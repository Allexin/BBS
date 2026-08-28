import json
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.restore_request import (
    RestoreRequest,
    RestoreRequestError,
    load_restore_request,
)

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_restore_request_accepts_only_safe_selection_and_absolute_target() -> None:
    request = RestoreRequest(
        request_id=REQUEST_ID,
        job_id="data",
        version="abcdef",
        path=r"Photos\2020",
        target=r"D:\Restores",
    )
    assert request.path == r"Photos\2020"


@pytest.mark.parametrize("selection", ["", r"..\secret", r"C:\data", "*.txt"])
def test_restore_request_rejects_unsafe_selection(selection: str) -> None:
    with pytest.raises(ValueError):
        RestoreRequest(
            request_id=REQUEST_ID,
            job_id="data",
            version="latest",
            path=selection,
            target=r"D:\Restores",
        )


def test_request_file_must_match_job(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": str(REQUEST_ID),
                "job_id": "other",
                "version": "abcdef",
                "path": ".",
                "target": r"D:\Restores",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RestoreRequestError, match="does not match"):
        load_restore_request(path, expected_job_id="data")
