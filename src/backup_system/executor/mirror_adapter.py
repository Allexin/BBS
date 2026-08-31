"""Safe mirror reconciliation, journal recovery and verification."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_catalog import CatalogEntry, MirrorCatalog
from backup_system.executor.mirror_plan import (
    CapacityAssessment,
    MirrorPlan,
    PathKeyProvider,
    PlanAction,
    ScanResult,
    WindowsOrdinalPathKeys,
    assess_capacity,
    build_plan,
    scan_tree,
)
from backup_system.executor.mirror_win32 import MirrorFileOperations

CONTROL_DIRECTORY = ".backup-system"
TEMP_PREFIX = ".bbs-tmp-"


class MirrorVerificationError(RuntimeError):
    pass


class MirrorRepairNotAllowedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MirrorBackupResult:
    generation_id: UUID
    copied_files: int
    deleted_files: int
    skipped_reparse_points: int
    mtime_failures: int
    capacity: CapacityAssessment


@dataclass(frozen=True, slots=True)
class MirrorCheckResult:
    mode: str
    checked_files: int
    checked_bytes: int
    logical_bytes: int


class MirrorAdapter:
    def __init__(
        self,
        *,
        files: MirrorFileOperations,
        cancellation: CancellationToken,
        path_keys: PathKeyProvider | None = None,
    ) -> None:
        self._files = files
        self._cancellation = cancellation
        self._path_keys = path_keys or WindowsOrdinalPathKeys()

    def backup(
        self,
        *,
        source_root: Path,
        destination_root: Path,
        excludes: tuple[str, ...],
        job_id: str,
        marker_uuid: UUID,
        run_id: UUID,
        volume_free_bytes: int,
        allow_verification_gate: bool = False,
        force_copy_all: bool = False,
    ) -> MirrorBackupResult:
        self._cancellation.raise_if_requested()
        source = scan_tree(source_root, excludes=excludes, path_keys=self._path_keys)
        catalog_path = destination_root / CONTROL_DIRECTORY / "catalog.sqlite3"
        with MirrorCatalog(catalog_path, job_id=job_id, marker_uuid=marker_uuid) as catalog:
            if catalog.verification_gate_active() and not allow_verification_gate:
                raise MirrorVerificationError("mirror verification gate is active")
            try:
                self._recover(destination_root, catalog)
            except MirrorVerificationError:
                catalog.activate_verification_gate()
                raise
            destination = scan_tree(
                destination_root,
                path_keys=self._path_keys,
                reserved_root=CONTROL_DIRECTORY,
            )
            entries = catalog.entries()
            unchanged = (
                frozenset() if force_copy_all else _unchanged_keys(source, destination, entries)
            )
            plan = build_plan(source, destination, unchanged_path_keys=unchanged)
            capacity = assess_capacity(plan, volume_free_bytes=volume_free_bytes)
            _save_plan(destination_root, run_id, plan)
            copied, deleted, mtime_failures = self._apply_plan(
                source_root=source_root,
                destination_root=destination_root,
                plan=plan,
                catalog=catalog,
                entries=entries,
                generation_id=run_id,
            )
            catalog.commit_generation(run_id)
        return MirrorBackupResult(
            generation_id=run_id,
            copied_files=copied,
            deleted_files=deleted,
            skipped_reparse_points=source.skipped_reparse_points,
            mtime_failures=mtime_failures,
            capacity=capacity,
        )

    def check(
        self,
        *,
        destination_root: Path,
        job_id: str,
        marker_uuid: UUID,
        mode: str,
    ) -> MirrorCheckResult:
        if mode not in {"metadata", "subset", "full"}:
            raise ValueError(f"unsupported mirror check mode: {mode}")
        with MirrorCatalog(
            destination_root / CONTROL_DIRECTORY / "catalog.sqlite3",
            job_id=job_id,
            marker_uuid=marker_uuid,
        ) as catalog:
            try:
                self._recover(destination_root, catalog)
                destination = scan_tree(
                    destination_root,
                    path_keys=self._path_keys,
                    reserved_root=CONTROL_DIRECTORY,
                )
                entries = catalog.entries()
                present = {
                    key: value for key, value in entries.items() if value.desired_state == "present"
                }
                _verify_metadata(destination, present)
                selected = _select_for_hash(present, mode)
                checked_bytes = 0
                for entry in selected:
                    self._cancellation.raise_if_requested()
                    actual_hash = _hash_file(
                        destination_root / entry.relative_path, self._cancellation
                    )
                    if actual_hash != entry.sha256:
                        raise MirrorVerificationError(
                            f"mirror content mismatch: {entry.relative_path}"
                        )
                    if entry.content_generation is None:
                        raise MirrorVerificationError("catalog content generation is missing")
                    catalog.mark_verified(
                        entry.path_key,
                        content_generation=entry.content_generation,
                    )
                    checked_bytes += entry.size_bytes or 0
            except MirrorVerificationError:
                catalog.activate_verification_gate()
                raise
        return MirrorCheckResult(mode, len(selected), checked_bytes, destination.total_bytes)

    def repair(
        self,
        *,
        source_root: Path,
        destination_root: Path,
        excludes: tuple[str, ...],
        job_id: str,
        marker_uuid: UUID,
        run_id: UUID,
        volume_free_bytes: int,
    ) -> MirrorBackupResult:
        catalog_path = destination_root / CONTROL_DIRECTORY / "catalog.sqlite3"
        with MirrorCatalog(catalog_path, job_id=job_id, marker_uuid=marker_uuid) as catalog:
            if not catalog.verification_gate_active():
                raise MirrorRepairNotAllowedError(
                    "repair-mirror requires an active verification gate"
                )
        result = self.backup(
            source_root=source_root,
            destination_root=destination_root,
            excludes=excludes,
            job_id=job_id,
            marker_uuid=marker_uuid,
            run_id=run_id,
            volume_free_bytes=volume_free_bytes,
            allow_verification_gate=True,
            force_copy_all=True,
        )
        self.check(
            destination_root=destination_root,
            job_id=job_id,
            marker_uuid=marker_uuid,
            mode="full",
        )
        with MirrorCatalog(catalog_path, job_id=job_id, marker_uuid=marker_uuid) as catalog:
            catalog.clear_verification_gate()
        return result

    def _apply_plan(
        self,
        *,
        source_root: Path,
        destination_root: Path,
        plan: MirrorPlan,
        catalog: MirrorCatalog,
        entries: dict[str, CatalogEntry],
        generation_id: UUID,
    ) -> tuple[int, int, int]:
        copied = 0
        deleted = 0
        mtime_failures = 0
        delete_items = tuple(item for item in plan.files if item.action == PlanAction.DELETE)
        copy_items = tuple(item for item in plan.files if item.action != PlanAction.DELETE)
        for item in delete_items:
            self._cancellation.raise_if_requested()
            final = destination_root / item.relative_path
            entry = entries.get(item.path_key)
            if entry is None:
                catalog.accept_new_absent(
                    path_key=item.path_key,
                    relative_path=item.relative_path,
                    generation_id=generation_id,
                )
            else:
                catalog.accept_absent(entry, generation_id=generation_id)
            self._files.delete_file(final)
            catalog.remove_tombstone(item.path_key)
            deleted += 1
        for relative in plan.destination_directories:
            self._cancellation.raise_if_requested()
            self._files.remove_directory(destination_root / relative)
        for item in copy_items:
            self._cancellation.raise_if_requested()
            final = destination_root / item.relative_path
            source = source_root / item.relative_path
            temp_relative = str(
                Path(item.relative_path).parent
                / f"{TEMP_PREFIX}{generation_id.hex}-{secrets.token_hex(8)}"
            )
            temp = destination_root / temp_relative
            accepted = False
            try:
                self._files.copy_to_temp(
                    source,
                    temp,
                    expected_size=item.source_size_bytes or 0,
                    cancellation=self._cancellation,
                )
                digest = _hash_file(temp, self._cancellation)
                self._cancellation.raise_if_requested()
                catalog.accept_present(
                    path_key=item.path_key,
                    relative_path=item.relative_path,
                    size_bytes=item.source_size_bytes or 0,
                    source_mtime_ns=item.source_mtime_ns or 0,
                    sha256=digest,
                    temp_relative_path=temp_relative,
                    generation_id=generation_id,
                )
                accepted = True
                self._cancellation.raise_if_requested()
                self._files.publish(temp, final, replace_existing=final.exists())
                catalog.clear_temp(item.path_key)
            except BaseException:
                if not accepted:
                    _remove_owned_temp(temp)
                raise
            try:
                os.utime(final, ns=(item.source_mtime_ns or 0, item.source_mtime_ns or 0))
            except OSError:
                mtime_failures += 1
            copied += 1
        return copied, deleted, mtime_failures

    def _recover(self, destination_root: Path, catalog: MirrorCatalog) -> None:
        for entry in catalog.entries().values():
            self._cancellation.raise_if_requested()
            final = destination_root / entry.relative_path
            if entry.desired_state == "absent":
                if final.exists():
                    self._files.delete_file(final)
                catalog.remove_tombstone(entry.path_key)
                continue
            if entry.temp_relative_path is None:
                continue
            temp = destination_root / entry.temp_relative_path
            final_matches = _matches(final, entry, self._cancellation)
            temp_matches = _matches(temp, entry, self._cancellation)
            if final_matches:
                _remove_owned_temp(temp)
                catalog.clear_temp(entry.path_key)
            elif temp_matches:
                self._files.publish(temp, final, replace_existing=final.exists())
                catalog.clear_temp(entry.path_key)
            else:
                raise MirrorVerificationError(
                    f"unresolved mirror journal state: {entry.relative_path}"
                )


def _unchanged_keys(
    source: ScanResult,
    destination: ScanResult,
    entries: dict[str, CatalogEntry],
) -> frozenset[str]:
    unchanged: set[str] = set()
    for key, item in source.files.items():
        target = destination.files.get(key)
        entry = entries.get(key)
        if (
            target is not None
            and entry is not None
            and entry.desired_state == "present"
            and entry.temp_relative_path is None
            and entry.path_key == key
            and entry.size_bytes == item.size_bytes == target.size_bytes
            and entry.source_mtime_ns == item.mtime_ns
            and entry.sha256 is not None
        ):
            unchanged.add(key)
    return frozenset(unchanged)


def _verify_metadata(destination: ScanResult, entries: dict[str, CatalogEntry]) -> None:
    if set(destination.files) != set(entries):
        raise MirrorVerificationError("mirror path set differs from catalog")
    for key, item in destination.files.items():
        entry = entries[key]
        if entry.size_bytes != item.size_bytes:
            raise MirrorVerificationError(f"mirror size mismatch: {item.relative_path}")


def _select_for_hash(entries: dict[str, CatalogEntry], mode: str) -> tuple[CatalogEntry, ...]:
    if mode == "metadata":
        return ()
    ordered = sorted(entries.values(), key=lambda item: (item.verified_at or "", item.path_key))
    if mode == "full":
        return tuple(ordered)
    logical = sum(item.size_bytes or 0 for item in ordered)
    budget = max(1, (logical + 3) // 4)
    selected: list[CatalogEntry] = []
    total = 0
    for item in ordered:
        selected.append(item)
        total += item.size_bytes or 0
        if total >= budget:
            break
    return tuple(selected)


def _matches(path: Path, entry: CatalogEntry, cancellation: CancellationToken) -> bool:
    if not path.is_file() or entry.size_bytes is None or entry.sha256 is None:
        return False
    try:
        if path.stat().st_size != entry.size_bytes:
            return False
        return _hash_file(path, cancellation) == entry.sha256
    except OSError:
        return False


def _hash_file(path: Path, cancellation: CancellationToken) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                cancellation.raise_if_requested()
                digest.update(chunk)
    except OSError as error:
        raise MirrorVerificationError(f"cannot read mirror file: {path}") from error
    return digest.digest()


def _save_plan(destination_root: Path, run_id: UUID, plan: MirrorPlan) -> None:
    plans = destination_root / CONTROL_DIRECTORY / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    final = plans / f"{run_id}.json"
    temp = plans / f".{run_id}.tmp"
    payload = {
        "schema_version": 1,
        "run_id": str(run_id),
        "aggregates": {
            "current_mirror_size": plan.current_mirror_size,
            "planned_mirror_size": plan.planned_mirror_size,
            "largest_copy_size": plan.largest_copy_size,
            "required_peak_mirror_size": plan.required_peak_mirror_size,
        },
        "files": [asdict(item) for item in plan.files],
    }
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, final)


def _remove_owned_temp(path: Path) -> None:
    if path.name.startswith(TEMP_PREFIX):
        with suppress(FileNotFoundError):
            path.unlink()
