"""Centralized Win32 file primitives for the production mirror data path."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_plan import MirrorOutOfSpaceError

ERROR_DISK_FULL = 112
ERROR_HANDLE_DISK_FULL = 39
COPY_FILE_FAIL_IF_EXISTS = 0x00000001
COPYFILE2_PROGRESS_CONTINUE = 0
COPYFILE2_PROGRESS_CANCEL = 1
MOVEFILE_REPLACE_EXISTING = 0x00000001
MOVEFILE_WRITE_THROUGH = 0x00000008
REPLACEFILE_WRITE_THROUGH = 0x00000001
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_COPY_PROGRESS = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class MirrorIoError(RuntimeError):
    pass


class MirrorFileOperations(Protocol):
    def copy_to_temp(
        self,
        source: Path,
        temp: Path,
        *,
        expected_size: int,
        cancellation: CancellationToken,
    ) -> None: ...

    def publish(self, temp: Path, final: Path, *, replace_existing: bool) -> None: ...

    def delete_file(self, path: Path) -> None: ...

    def remove_directory(self, path: Path) -> None: ...


class _CopyFile2ExtendedParameters(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwCopyFlags", wintypes.DWORD),
        ("pfCancel", ctypes.POINTER(wintypes.BOOL)),
        ("pProgressRoutine", ctypes.c_void_p),
        ("pvCallbackContext", ctypes.c_void_p),
    ]


class WindowsMirrorFileOperations:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 mirror file operations require Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def copy_to_temp(
        self,
        source: Path,
        temp: Path,
        *,
        expected_size: int,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.raise_if_requested()
        temp.parent.mkdir(parents=True, exist_ok=True)
        cancelled = wintypes.BOOL(False)

        @_COPY_PROGRESS
        def progress(message: ctypes.c_void_p, context: ctypes.c_void_p) -> int:
            del message, context
            return (
                COPYFILE2_PROGRESS_CANCEL
                if cancellation.requested
                else COPYFILE2_PROGRESS_CONTINUE
            )

        parameters = _CopyFile2ExtendedParameters(
            ctypes.sizeof(_CopyFile2ExtendedParameters),
            COPY_FILE_FAIL_IF_EXISTS,
            ctypes.pointer(cancelled),
            ctypes.cast(progress, ctypes.c_void_p),
            None,
        )
        result = self._kernel32.CopyFile2(str(source), str(temp), ctypes.byref(parameters))
        if result < 0:
            self._raise_hresult("CopyFile2", result)
        cancellation.raise_if_requested()
        self._flush_file(temp)
        try:
            actual_size = temp.stat().st_size
        except OSError as error:
            raise MirrorIoError(f"cannot read copied file size: {temp}") from error
        if actual_size != expected_size:
            raise MirrorIoError(
                f"copied file size mismatch: expected {expected_size}, observed {actual_size}"
            )

    def publish(self, temp: Path, final: Path, *, replace_existing: bool) -> None:
        if replace_existing:
            if not self._kernel32.ReplaceFileW(
                str(final), str(temp), None, REPLACEFILE_WRITE_THROUGH, None, None
            ):
                self._raise_last_error("ReplaceFileW")
        elif not self._kernel32.MoveFileExW(
            str(temp), str(final), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        ):
            self._raise_last_error("MoveFileExW")

    def delete_file(self, path: Path) -> None:
        if not self._kernel32.DeleteFileW(str(path)):
            self._raise_last_error("DeleteFileW")

    def remove_directory(self, path: Path) -> None:
        if not self._kernel32.RemoveDirectoryW(str(path)):
            self._raise_last_error("RemoveDirectoryW")

    def _flush_file(self, path: Path) -> None:
        handle = self._kernel32.CreateFileW(
            str(path),
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateFileW")
        try:
            if not self._kernel32.FlushFileBuffers(handle):
                self._raise_last_error("FlushFileBuffers")
        finally:
            self._kernel32.CloseHandle(handle)

    def _raise_last_error(self, operation: str) -> None:
        code = ctypes.get_last_error()
        self._raise_code(operation, code)

    def _raise_hresult(self, operation: str, result: int) -> None:
        code = result & 0xFFFF
        self._raise_code(operation, code)

    @staticmethod
    def _raise_code(operation: str, code: int) -> None:
        if code in {ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL}:
            raise MirrorOutOfSpaceError(f"{operation} failed with Win32 error {code}")
        raise MirrorIoError(f"{operation} failed with Win32 error {code}")

    def _configure_signatures(self) -> None:
        self._kernel32.CopyFile2.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(_CopyFile2ExtendedParameters),
        ]
        self._kernel32.CopyFile2.restype = ctypes.c_long
        self._kernel32.ReplaceFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._kernel32.ReplaceFileW.restype = wintypes.BOOL
        self._kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.MoveFileExW.restype = wintypes.BOOL
        self._kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.DeleteFileW.restype = wintypes.BOOL
        self._kernel32.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.RemoveDirectoryW.restype = wintypes.BOOL
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
