"""Low-level IVssBackupComponents backend with durable-start lifecycle support."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, Protocol
from uuid import UUID

from backup_system.executor.vss import VssSnapshot
from backup_system.executor.windows_vss import VssBackendError

HRESULT = ctypes.c_long
VSS_ASYNC_TIMEOUT_SECONDS = 120.0
VSS_ASYNC_POLL_SECONDS = 0.25
VSS_S_ASYNC_PENDING = 0x00042309
VSS_S_ASYNC_FINISHED = 0x0004230A
VSS_S_ASYNC_CANCELLED = 0x0004230B
HRESULT_WAIT_TIMEOUT = 0x800705B4
HRESULT_OPERATION_CANCELLED = 0x800704C7
VSS_BT_COPY = 5
VSS_CTX_CLIENT_ACCESSIBLE = 0x0000001D
VSS_OBJECT_SNAPSHOT_SET = 2
VSS_E_OBJECT_NOT_FOUND = 0x80042308
RPC_E_CHANGED_MODE = 0x80010106


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _SnapshotProperties(ctypes.Structure):
    _fields_ = [
        ("snapshot_id", _Guid),
        ("snapshot_set_id", _Guid),
        ("snapshots_count", wintypes.LONG),
        ("snapshot_device_object", wintypes.LPWSTR),
        ("original_volume_name", wintypes.LPWSTR),
        ("originating_machine", wintypes.LPWSTR),
        ("service_machine", wintypes.LPWSTR),
        ("exposed_name", wintypes.LPWSTR),
        ("exposed_path", wintypes.LPWSTR),
        ("provider_id", _Guid),
        ("snapshot_attributes", wintypes.LONG),
        ("creation_timestamp", ctypes.c_longlong),
        ("status", ctypes.c_int),
    ]


class NativeVssSession(Protocol):
    def start_snapshot_set(self) -> UUID: ...

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_name: str) -> VssSnapshot: ...

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None: ...

    def close(self) -> None: ...


class NativeVssFactory(Protocol):
    def create(self) -> NativeVssSession: ...


class NativeVssBackend:
    """PreparedVssBackend retaining one COM requestor across Start/Add/Do."""

    def __init__(
        self,
        factory: NativeVssFactory | None = None,
        *,
        cancellation_checkpoint: Callable[[], None] | None = None,
    ) -> None:
        if factory is not None and cancellation_checkpoint is not None:
            raise ValueError("custom VSS factory owns its cancellation integration")
        self._factory = factory or CtypesVssFactory(cancellation_checkpoint)
        self._active: tuple[UUID, NativeVssSession] | None = None

    def start_snapshot_set(self) -> UUID:
        if self._active is not None:
            raise VssBackendError("start while another set is active", 0x80042301)
        session = self._factory.create()
        try:
            snapshot_set_id = session.start_snapshot_set()
        except BaseException:
            session.close()
            raise
        self._active = (snapshot_set_id, session)
        return snapshot_set_id

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_guid: str) -> VssSnapshot:
        session = self._require_active(snapshot_set_id)
        return session.complete_snapshot_set(snapshot_set_id, _volume_name(volume_guid))

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        if self._active is not None:
            session = self._require_active(snapshot_set_id)
            try:
                session.delete_snapshot_set(snapshot_set_id)
            finally:
                session.close()
                self._active = None
            return
        session = self._factory.create()
        try:
            session.delete_snapshot_set(snapshot_set_id)
        finally:
            session.close()

    def _require_active(self, snapshot_set_id: UUID) -> NativeVssSession:
        if self._active is None or self._active[0] != snapshot_set_id:
            raise VssBackendError("snapshot set is not owned by active requestor", 0x80042308)
        return self._active[1]


class CtypesVssFactory:
    def __init__(self, cancellation_checkpoint: Callable[[], None] | None = None) -> None:
        if os.name != "nt":
            raise OSError("VSS is available only on Windows")
        self._vssapi = ctypes.WinDLL("vssapi", use_last_error=True)
        # WinDLL preserves the raw HRESULT. OleDLL raises before we can accept
        # RPC_E_CHANGED_MODE from a thread initialized by another COM consumer.
        self._ole32 = ctypes.WinDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole32.CoInitializeEx.restype = HRESULT
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._create = self._vssapi.CreateVssBackupComponentsInternal
        self._create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self._create.restype = HRESULT
        self._free_properties = self._vssapi.VssFreeSnapshotPropertiesInternal
        self._free_properties.argtypes = [ctypes.POINTER(_SnapshotProperties)]
        self._free_properties.restype = None
        self._cancellation_checkpoint = cancellation_checkpoint or (lambda: None)

    def create(self) -> NativeVssSession:
        result = self._ole32.CoInitializeEx(None, 0)
        uninitialize = _com_uninitializer(result, self._ole32.CoUninitialize)
        pointer = ctypes.c_void_p()
        try:
            _check("CreateVssBackupComponents", self._create(ctypes.byref(pointer)))
        except BaseException:
            if uninitialize is not None:
                uninitialize()
            raise
        if not pointer.value:
            if uninitialize is not None:
                uninitialize()
            raise VssBackendError("CreateVssBackupComponents returned null", 0x80004003)
        return CtypesVssSession(
            pointer,
            self._free_properties,
            uninitialize,
            self._cancellation_checkpoint,
        )


def _com_uninitializer(result: int, uninitialize: Any) -> Any | None:
    unsigned_result = _unsigned(result)
    if _failed(result) and unsigned_result != RPC_E_CHANGED_MODE:
        _raise_hresult("CoInitializeEx", result)
    return uninitialize if unsigned_result in {0, 1} else None


class CtypesVssSession:
    def __init__(
        self,
        pointer: ctypes.c_void_p,
        free_properties: Any,
        uninitialize: Any | None,
        cancellation_checkpoint: Callable[[], None],
    ) -> None:
        self._pointer = pointer
        self._free_properties = free_properties
        self._uninitialize = uninitialize
        self._cancellation_checkpoint = cancellation_checkpoint
        self._closed = False
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def start_snapshot_set(self) -> UUID:
        identifier = _Guid()
        self._invoke(36, "StartSnapshotSet", ctypes.POINTER(_Guid))(
            self._pointer, ctypes.byref(identifier)
        )
        return _uuid(identifier)

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_name: str) -> VssSnapshot:
        snapshot_id = _Guid()
        provider = _Guid()
        self._invoke(
            37,
            "AddToSnapshotSet",
            wintypes.LPCWSTR,
            _Guid,
            ctypes.POINTER(_Guid),
        )(self._pointer, volume_name, provider, ctypes.byref(snapshot_id))
        asynchronous = ctypes.c_void_p()
        self._invoke(38, "DoSnapshotSet", ctypes.POINTER(ctypes.c_void_p))(
            self._pointer, ctypes.byref(asynchronous)
        )
        _wait_async(asynchronous, "DoSnapshotSet", self._cancellation_checkpoint)
        properties = _SnapshotProperties()
        properties_loaded = False
        try:
            self._invoke(42, "GetSnapshotProperties", _Guid, ctypes.POINTER(_SnapshotProperties))(
                self._pointer, snapshot_id, ctypes.byref(properties)
            )
            properties_loaded = True
            actual_set_id = _uuid(properties.snapshot_set_id)
            if actual_set_id != snapshot_set_id:
                raise VssBackendError("verify snapshot set", 0x8004230F)
            original = str(properties.original_volume_name or "")
            if _normalize_volume(original) != _normalize_volume(volume_name):
                raise VssBackendError("verify source volume", 0x8004230F)
            device = str(properties.snapshot_device_object or "")
            if not device.startswith(r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy"):
                raise VssBackendError("verify shadow device", 0x8004230F)
            return VssSnapshot(actual_set_id, _uuid(snapshot_id), volume_name, device + "\\")
        finally:
            if properties_loaded:
                self._free_properties(ctypes.byref(properties))

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        deleted = wintypes.LONG()
        nondeleted = _Guid()
        delete = _raw_method(
            self._pointer,
            39,
            HRESULT,
            _Guid,
            ctypes.c_int,
            wintypes.BOOL,
            ctypes.POINTER(wintypes.LONG),
            ctypes.POINTER(_Guid),
        )
        result = delete(
            self._pointer,
            _guid(snapshot_set_id),
            VSS_OBJECT_SNAPSHOT_SET,
            True,
            ctypes.byref(deleted),
            ctypes.byref(nondeleted),
        )
        if _unsigned(result) != VSS_E_OBJECT_NOT_FOUND:
            _check("DeleteSnapshots", result)

    def close(self) -> None:
        if not self._closed:
            _raw_method(self._pointer, 2, ctypes.c_ulong)(self._pointer)
            if self._uninitialize is not None:
                self._uninitialize()
            self._closed = True

    def _initialize(self) -> None:
        self._invoke(5, "InitializeForBackup", ctypes.c_void_p)(self._pointer, None)
        self._invoke(
            6,
            "SetBackupState",
            wintypes.BOOLEAN,
            wintypes.BOOLEAN,
            ctypes.c_int,
            wintypes.BOOLEAN,
        )(self._pointer, False, False, VSS_BT_COPY, False)
        self._invoke(35, "SetContext", wintypes.LONG)(self._pointer, VSS_CTX_CLIENT_ACCESSIBLE)

    def _invoke(self, index: int, operation: str, *arguments: Any) -> Any:
        function = _raw_method(self._pointer, index, HRESULT, *arguments)

        def checked(*values: object) -> None:
            _check(operation, function(*values))

        return checked


def _raw_method(pointer: ctypes.c_void_p, index: int, result: Any, *arguments: Any) -> Any:
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    prototype = ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)
    return prototype(address)


def _wait_async(
    pointer: ctypes.c_void_p,
    operation: str,
    cancellation_checkpoint: Callable[[], None],
) -> None:
    if not pointer.value:
        raise VssBackendError(f"{operation} returned null async", 0x80004003)
    try:
        query = _raw_method(
            pointer, 5, HRESULT, ctypes.POINTER(HRESULT), ctypes.POINTER(wintypes.LONG)
        )
        cancel_method = _raw_method(pointer, 3, HRESULT)

        def query_status() -> int:
            status = HRESULT()
            _check(f"{operation}.QueryStatus", query(pointer, ctypes.byref(status), None))
            return status.value

        def cancel() -> None:
            _check(f"{operation}.Cancel", cancel_method(pointer))

        _poll_async_status(
            operation=operation,
            query_status=query_status,
            cancel=cancel,
            cancellation_checkpoint=cancellation_checkpoint,
        )
    finally:
        _raw_method(pointer, 2, ctypes.c_ulong)(pointer)


def _poll_async_status(
    *,
    operation: str,
    query_status: Callable[[], int],
    cancel: Callable[[], None],
    cancellation_checkpoint: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + VSS_ASYNC_TIMEOUT_SECONDS
    while True:
        status = query_status()
        if _unsigned(status) != VSS_S_ASYNC_PENDING:
            _require_async_finished(status, operation)
            return
        try:
            cancellation_checkpoint()
        except BaseException:
            cancel()
            raise
        remaining = deadline - monotonic()
        if remaining <= 0:
            cancel()
            raise VssBackendError(f"{operation} timed out", HRESULT_WAIT_TIMEOUT)
        sleep(min(VSS_ASYNC_POLL_SECONDS, remaining))


def _require_async_finished(status: int, operation: str) -> None:
    unsigned = _unsigned(status)
    if unsigned == VSS_S_ASYNC_FINISHED:
        return
    if unsigned == VSS_S_ASYNC_PENDING:
        raise VssBackendError(f"{operation} timed out", HRESULT_WAIT_TIMEOUT)
    if unsigned == VSS_S_ASYNC_CANCELLED:
        raise VssBackendError(f"{operation} was cancelled", HRESULT_OPERATION_CANCELLED)
    _check(operation, status)


def _guid(value: UUID) -> _Guid:
    return _Guid.from_buffer_copy(value.bytes_le)


def _uuid(value: _Guid) -> UUID:
    return UUID(bytes_le=bytes(value))


def _volume_name(value: str) -> str:
    stripped = value.strip().rstrip("\\")
    if stripped.casefold().startswith(r"\\?\volume{") and stripped.endswith("}"):
        identifier = UUID(stripped[len(r"\\?\Volume{") : -1])
    else:
        identifier = UUID(stripped.strip("{}"))
    return f"\\\\?\\Volume{{{identifier}}}\\"


def _normalize_volume(value: str) -> str:
    return value.strip().rstrip("\\").casefold()


def _unsigned(value: int) -> int:
    return value & 0xFFFFFFFF


def _failed(value: int) -> bool:
    return bool(_unsigned(value) & 0x80000000)


def _check(operation: str, value: int) -> None:
    if _failed(value):
        _raise_hresult(operation, value)


def _raise_hresult(operation: str, value: int) -> None:
    raise VssBackendError(operation, _unsigned(value))
