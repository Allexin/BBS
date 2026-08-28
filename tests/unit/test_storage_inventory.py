from backup_system.executor.storage_inventory import (
    DiskRecord,
    PartitionRecord,
    WindowsStorageInventory,
)


class FakeSource:
    def disks(self) -> tuple[DiskRecord, ...]:
        return (
            DiskRecord(0, "SYSTEM", 500, False, True, True),
            DiskRecord(7, " BACKUP ", 1000, True, False, False),
        )

    def partitions(self) -> tuple[PartitionRecord, ...]:
        return (
            PartitionRecord(7, "partition-a", ("\\\\?\\Volume{volume-a}\\", "D:\\")),
            PartitionRecord(7, "partition-b", ("\\\\?\\Volume{volume-b}\\",)),
            PartitionRecord(99, "missing-disk", ("\\\\?\\Volume{ignored}\\",)),
            PartitionRecord(0, "", ("\\\\?\\Volume{ignored}\\",)),
        )


def test_inventory_joins_structured_disk_partition_and_volume_identity() -> None:
    candidates = WindowsStorageInventory(FakeSource()).enumerate()
    assert [(item.disk_number, item.partition_guid, item.volume_guid) for item in candidates] == [
        (7, "partition-a", "volume-a"),
        (7, "partition-b", "volume-b"),
    ]
    assert all(item.physical_serial == " BACKUP " for item in candidates)
    assert all(item.offline for item in candidates)


def test_ambiguous_volume_access_paths_are_not_inventory_candidates() -> None:
    class Ambiguous(FakeSource):
        def partitions(self) -> tuple[PartitionRecord, ...]:
            return (
                PartitionRecord(
                    7,
                    "partition-a",
                    ("\\\\?\\Volume{volume-a}\\", "\\\\?\\Volume{volume-b}\\"),
                ),
            )

    assert WindowsStorageInventory(Ambiguous()).enumerate() == ()
