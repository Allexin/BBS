"""Idempotent application of accepted spool commands to local manager state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backup_system.common.commands import CancelCurrentCommand, QueueRemoveCommand, RunCommand
from backup_system.manager.operations import (
    EnqueueDisposition,
    OperationsRepository,
    RemoveDisposition,
)
from backup_system.manager.spool import CommandSpool


class CommandDisposition(StrEnum):
    ENQUEUED = "enqueued"
    DEDUPLICATED = "deduplicated"
    COALESCED = "coalesced"
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    NOT_QUEUED = "not_queued"
    CANCEL_REQUESTED = "cancel_requested"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProcessedCommand:
    command_id: UUID
    disposition: CommandDisposition
    operation_id: UUID | None = None


class CommandProcessor:
    def __init__(
        self,
        spool: CommandSpool,
        operations: OperationsRepository,
        *,
        cancel_current: Callable[[], None],
        default_operations: dict[str, str] | None = None,
    ) -> None:
        self._spool = spool
        self._operations = operations
        self._cancel_current = cancel_current
        self._default_operations = default_operations or {}

    def process_accepted(self) -> tuple[ProcessedCommand, ...]:
        processed: list[ProcessedCommand] = []
        for command in self._spool.load_accepted():
            try:
                outcome = self._process(command)
            except (RuntimeError, ValueError, sqlite3.Error):
                self._spool.mark_rejected(command.command_id)
                processed.append(ProcessedCommand(command.command_id, CommandDisposition.REJECTED))
                continue
            self._spool.mark_completed(command.command_id)
            self._spool.write_result(
                command.command_id,
                disposition=outcome.disposition,
                operation_id=outcome.operation_id,
            )
            processed.append(outcome)
        return tuple(processed)

    def _process(
        self, command: RunCommand | QueueRemoveCommand | CancelCurrentCommand
    ) -> ProcessedCommand:
        if isinstance(command, RunCommand):
            operation = command.operation or self._default_operations.get(command.job_id)
            if operation is None:
                raise ValueError("job has no configured default operation")
            request: dict[str, object] | None = None
            if operation == "restore":
                assert command.version is not None
                assert command.path is not None
                assert command.target is not None
                request = {
                    "schema_version": 1,
                    "request_id": str(command.command_id),
                    "job_id": command.job_id,
                    "version": command.version,
                    "path": command.path,
                    "target": command.target,
                }
            enqueue_result = self._operations.enqueue(
                deduplication_key=f"command:{command.command_id}",
                job_id=command.job_id,
                kind="resolve-restore" if operation == "restore" else operation,
                mode=command.mode,
                request=request,
                trigger_source="manual",
                queued_at=command.created_at,
            )
            disposition = {
                EnqueueDisposition.CREATED: CommandDisposition.ENQUEUED,
                EnqueueDisposition.DEDUPLICATED: CommandDisposition.DEDUPLICATED,
                EnqueueDisposition.COALESCED: CommandDisposition.COALESCED,
            }[enqueue_result.disposition]
            outcome = ProcessedCommand(command.command_id, disposition, enqueue_result.operation_id)
        elif isinstance(command, QueueRemoveCommand):
            remove_result = self._operations.remove_queued(command.operation_id)
            disposition = {
                RemoveDisposition.REMOVED: CommandDisposition.REMOVED,
                RemoveDisposition.NOT_FOUND: CommandDisposition.NOT_FOUND,
                RemoveDisposition.NOT_QUEUED: CommandDisposition.NOT_QUEUED,
            }[remove_result]
            outcome = ProcessedCommand(command.command_id, disposition, command.operation_id)
        elif isinstance(command, CancelCurrentCommand):
            self._cancel_current()
            outcome = ProcessedCommand(command.command_id, CommandDisposition.CANCEL_REQUESTED)
        else:
            raise AssertionError("unreachable command type")
        return outcome
