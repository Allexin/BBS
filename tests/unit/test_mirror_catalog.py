from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.mirror_catalog import MirrorCatalog, MirrorCatalogError

MARKER = UUID("22222222-2222-4222-8222-222222222222")
GENERATION = UUID("11111111-1111-4111-8111-111111111111")


def test_catalog_durably_tracks_present_temp_and_tombstone(tmp_path: Path) -> None:
    path = tmp_path / ".backup-system" / "catalog.sqlite3"
    with MirrorCatalog(path, job_id="job-1", marker_uuid=MARKER) as catalog:
        catalog.accept_present(
            path_key="dir\\file.bin",
            relative_path="Dir\\File.bin",
            size_bytes=3,
            source_mtime_ns=42,
            sha256=b"x" * 32,
            temp_relative_path="Dir\\.bbs-tmp-run-token",
            generation_id=GENERATION,
        )

    with MirrorCatalog(path, job_id="job-1", marker_uuid=MARKER) as catalog:
        entry = catalog.entries()["dir\\file.bin"]
        assert entry.temp_relative_path == "Dir\\.bbs-tmp-run-token"
        catalog.clear_temp(entry.path_key)
        catalog.accept_absent(entry, generation_id=GENERATION)

    with MirrorCatalog(path, job_id="job-1", marker_uuid=MARKER) as catalog:
        entry = catalog.entries()["dir\\file.bin"]
        assert entry.desired_state == "absent"
        catalog.remove_tombstone(entry.path_key)
        assert catalog.entries() == {}


def test_catalog_rejects_wrong_job_identity_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with MirrorCatalog(path, job_id="job-1", marker_uuid=MARKER):
        pass

    with (
        pytest.raises(MirrorCatalogError, match="identity"),
        MirrorCatalog(path, job_id="job-2", marker_uuid=MARKER),
    ):
        pass

    path.write_bytes(b"not sqlite")
    with (
        pytest.raises(MirrorCatalogError, match="open"),
        MirrorCatalog(path, job_id="job-1", marker_uuid=MARKER),
    ):
        pass
