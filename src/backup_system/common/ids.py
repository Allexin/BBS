"""UUID4 identifiers used by versioned contracts."""

from typing import NewType
from uuid import RFC_4122, UUID, uuid4

OperationId = NewType("OperationId", UUID)
RunId = NewType("RunId", UUID)
EventId = NewType("EventId", UUID)
CommandId = NewType("CommandId", UUID)


def new_operation_id() -> OperationId:
    return OperationId(uuid4())


def new_run_id() -> RunId:
    return RunId(uuid4())


def new_event_id() -> EventId:
    return EventId(uuid4())


def new_command_id() -> CommandId:
    return CommandId(uuid4())


def parse_uuid4(value: str) -> UUID:
    parsed = UUID(value)
    if parsed.version != 4 or parsed.variant != RFC_4122:
        raise ValueError("identifier must be an RFC 4122 UUID4")
    return parsed
