"""Typed ingestion of executor events that update manager-owned durable state."""

from __future__ import annotations

from backup_system.common.events import KnownExecutorEvent, SmartObserved
from backup_system.manager.smart_history import SmartComparison, SmartHistoryRepository


class ExecutorEventIngestor:
    def __init__(self, smart_history: SmartHistoryRepository) -> None:
        self._smart_history = smart_history

    def ingest(self, event: KnownExecutorEvent) -> SmartComparison | None:
        if not isinstance(event, SmartObserved):
            return None
        if event.collection_success and event.identity_key is None:
            raise ValueError("successful SMART event must contain an identity key")
        return self._smart_history.record(
            disk_id=event.disk_id,
            public_disk_id=event.disk_id,
            identity_key=event.identity_key or "unavailable",
            role="monitored",
            observed_at=event.timestamp,
            operational_state="unknown",
            smart_health=event.health,
            metrics=event.metrics,
            collection_success=event.collection_success,
        )
