from uuid import UUID

import pytest

from backup_system.common.ids import new_run_id, parse_uuid4


def test_uuid4_round_trip() -> None:
    identifier = new_run_id()
    assert parse_uuid4(str(identifier)) == identifier


def test_non_uuid4_is_rejected() -> None:
    with pytest.raises(ValueError, match="UUID4"):
        parse_uuid4(str(UUID("00000000-0000-1000-8000-000000000000")))
