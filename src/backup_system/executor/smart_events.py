"""Privacy-safe SMART observations on the executor JSON Lines boundary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from backup_system.common.events import SmartObserved
from backup_system.executor.smart_preflight import SmartPreflightObservation


def build_smart_events(
    observations: Sequence[SmartPreflightObservation], *, timestamp: datetime
) -> tuple[SmartObserved, ...]:
    return tuple(
        SmartObserved(
            event="smart_observed",
            timestamp=timestamp,
            disk_id=observation.disk_id,
            collection_success=observation.collection_success,
            health=observation.health,
            identity_key=observation.identity_key,
            metrics=observation.metrics,
            reason=observation.reason,
            manufacturer=observation.manufacturer,
            model=observation.model,
            media_type=observation.media_type,
            bus_type=observation.bus_type,
            capacity_bytes=observation.capacity_bytes,
            mount_points=observation.mount_points,
        )
        for observation in observations
    )
