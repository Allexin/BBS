"""Durable per-mirror SQLite hash catalog."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID

SCHEMA_VERSION = "1"
HASH_ALGORITHM = "sha256"


class MirrorCatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    path_key: str
    relative_path: str
    desired_state: str
    size_bytes: int | None
    source_mtime_ns: int | None
    sha256: bytes | None
    content_generation: str | None
    verified_at: str | None
    temp_relative_path: str | None
    generation_id: str


class MirrorCatalog:
    def __init__(self, path: Path, *, job_id: str, marker_uuid: UUID) -> None:
        self._path = path
        self._job_id = job_id
        self._marker_uuid = str(marker_uuid)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> MirrorCatalog:
        existed = self._path.exists()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self._path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            self._connection = connection
            if existed:
                self._validate()
            else:
                self._initialize()
            return self
        except (OSError, sqlite3.Error, MirrorCatalogError) as error:
            self._close_after_error()
            if isinstance(error, MirrorCatalogError):
                raise
            raise MirrorCatalogError("cannot open mirror catalog") from error

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        connection = self._require_connection()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
        except sqlite3.Error as error:
            raise MirrorCatalogError("cannot checkpoint or close mirror catalog") from error
        finally:
            self._connection = None

    def entries(self) -> dict[str, CatalogEntry]:
        rows = self._require_connection().execute(
            """
            SELECT path_key, relative_path, desired_state, size_bytes, source_mtime_ns,
                   sha256, content_generation, verified_at, temp_relative_path, generation_id
            FROM mirror_entries
            """
        )
        return {str(row["path_key"]): _entry(row) for row in rows}

    def accept_present(
        self,
        *,
        path_key: str,
        relative_path: str,
        size_bytes: int,
        source_mtime_ns: int,
        sha256: bytes,
        temp_relative_path: str,
        generation_id: UUID,
    ) -> None:
        if len(sha256) != 32:
            raise MirrorCatalogError("SHA-256 digest must contain 32 bytes")
        now = _now()
        with self._require_connection():
            self._require_connection().execute(
                """
                INSERT INTO mirror_entries (
                    path_key, relative_path, desired_state, size_bytes, source_mtime_ns,
                    sha256, content_generation, verified_at, temp_relative_path,
                    generation_id, updated_at
                ) VALUES (?, ?, 'present', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path_key) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    desired_state='present',
                    size_bytes=excluded.size_bytes,
                    source_mtime_ns=excluded.source_mtime_ns,
                    sha256=excluded.sha256,
                    content_generation=excluded.content_generation,
                    verified_at=excluded.verified_at,
                    temp_relative_path=excluded.temp_relative_path,
                    generation_id=excluded.generation_id,
                    updated_at=excluded.updated_at
                """,
                (
                    path_key,
                    relative_path,
                    size_bytes,
                    source_mtime_ns,
                    sha256,
                    str(generation_id),
                    now,
                    temp_relative_path,
                    str(generation_id),
                    now,
                ),
            )

    def accept_absent(self, entry: CatalogEntry, *, generation_id: UUID) -> None:
        with self._require_connection():
            self._require_connection().execute(
                """
                UPDATE mirror_entries SET desired_state='absent', temp_relative_path=NULL,
                    generation_id=?, updated_at=? WHERE path_key=?
                """,
                (str(generation_id), _now(), entry.path_key),
            )

    def accept_new_absent(
        self,
        *,
        path_key: str,
        relative_path: str,
        generation_id: UUID,
    ) -> None:
        with self._require_connection():
            self._require_connection().execute(
                """
                INSERT INTO mirror_entries (
                    path_key, relative_path, desired_state, size_bytes, source_mtime_ns,
                    sha256, content_generation, verified_at, temp_relative_path,
                    generation_id, updated_at
                ) VALUES (?, ?, 'absent', NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(path_key) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    desired_state='absent',
                    temp_relative_path=NULL,
                    generation_id=excluded.generation_id,
                    updated_at=excluded.updated_at
                """,
                (path_key, relative_path, str(generation_id), _now()),
            )

    def clear_temp(self, path_key: str) -> None:
        with self._require_connection():
            self._require_connection().execute(
                "UPDATE mirror_entries SET temp_relative_path=NULL, updated_at=? WHERE path_key=?",
                (_now(), path_key),
            )

    def remove_tombstone(self, path_key: str) -> None:
        with self._require_connection():
            self._require_connection().execute(
                "DELETE FROM mirror_entries WHERE path_key=? AND desired_state='absent'",
                (path_key,),
            )

    def mark_verified(self, path_key: str, *, content_generation: str) -> None:
        with self._require_connection():
            cursor = self._require_connection().execute(
                """
                UPDATE mirror_entries SET verified_at=?, updated_at=?
                WHERE path_key=? AND desired_state='present' AND content_generation=?
                """,
                (_now(), _now(), path_key, content_generation),
            )
            if cursor.rowcount != 1:
                raise MirrorCatalogError("catalog entry changed during verification")

    def commit_generation(self, generation_id: UUID) -> None:
        with self._require_connection():
            self._set_meta("generation_id", str(generation_id))

    def verification_gate_active(self) -> bool:
        row = (
            self._require_connection()
            .execute("SELECT value FROM catalog_meta WHERE key='verification_gate'")
            .fetchone()
        )
        return row is not None and str(row[0]) == "active"

    def activate_verification_gate(self) -> None:
        with self._require_connection():
            self._set_meta("verification_gate", "active")

    def clear_verification_gate(self) -> None:
        with self._require_connection():
            self._set_meta("verification_gate", "clear")

    def _initialize(self) -> None:
        connection = self._require_connection()
        with connection:
            connection.executescript(
                """
                CREATE TABLE catalog_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE mirror_entries (
                    path_key TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL,
                    desired_state TEXT NOT NULL CHECK (desired_state IN ('present', 'absent')),
                    size_bytes INTEGER,
                    source_mtime_ns INTEGER,
                    sha256 BLOB,
                    content_generation TEXT,
                    verified_at TEXT,
                    temp_relative_path TEXT,
                    generation_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                      (desired_state = 'present' AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)
                      OR desired_state = 'absent'
                    )
                );
                """
            )
            self._set_meta("schema_version", SCHEMA_VERSION)
            self._set_meta("hash_algorithm", HASH_ALGORITHM)
            self._set_meta("job_id", self._job_id)
            self._set_meta("marker_uuid", self._marker_uuid)
            self._set_meta("generation_id", "")
            self._set_meta("verification_gate", "clear")

    def _validate(self) -> None:
        connection = self._require_connection()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MirrorCatalogError("mirror catalog integrity check failed")
        try:
            meta = dict(connection.execute("SELECT key, value FROM catalog_meta"))
        except sqlite3.Error as error:
            raise MirrorCatalogError("mirror catalog schema is invalid") from error
        expected = {
            "schema_version": SCHEMA_VERSION,
            "hash_algorithm": HASH_ALGORITHM,
            "job_id": self._job_id,
            "marker_uuid": self._marker_uuid,
        }
        if any(meta.get(key) != value for key, value in expected.items()):
            raise MirrorCatalogError("mirror catalog identity or version mismatch")
        try:
            self.entries()
        except sqlite3.Error as error:
            raise MirrorCatalogError("mirror catalog entries are invalid") from error

    def _set_meta(self, key: str, value: str) -> None:
        self._require_connection().execute(
            """
            INSERT INTO catalog_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MirrorCatalogError("mirror catalog is not open")
        return self._connection

    def _close_after_error(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _entry(row: sqlite3.Row) -> CatalogEntry:
    digest = row["sha256"]
    return CatalogEntry(
        path_key=str(row["path_key"]),
        relative_path=str(row["relative_path"]),
        desired_state=str(row["desired_state"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        source_mtime_ns=(None if row["source_mtime_ns"] is None else int(row["source_mtime_ns"])),
        sha256=None if digest is None else bytes(digest),
        content_generation=(
            None if row["content_generation"] is None else str(row["content_generation"])
        ),
        verified_at=None if row["verified_at"] is None else str(row["verified_at"]),
        temp_relative_path=(
            None if row["temp_relative_path"] is None else str(row["temp_relative_path"])
        ),
        generation_id=str(row["generation_id"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
