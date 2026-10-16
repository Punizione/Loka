"""Best-effort Windows commit limit for Loka's own process.

Importing this module does not create a job or change process limits. Launchers
opt in only in their ``__main__`` guards. Child tools silently break away from
this job; any limits imposed by an enclosing job remain Windows' responsibility.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import threading
from typing import Callable


DEFAULT_PROCESS_MEMORY_LIMIT = 4 * 1024 ** 3
_PROCESS_MEMORY = 0x100
_SILENT_BREAKAWAY_OK = 0x1000
_EXTENDED_LIMIT_INFORMATION = 9
_job_handle = None  # Keep the successful handle open for the process lifetime.
_installed_result = None
_install_lock = threading.Lock()


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount',
    )]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _BasicLimitInformation),
        ('IoInfo', _IOCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class ProcessMemoryLimitResult:
    enabled: bool
    limit_bytes: int
    reason: str


def _load_kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _report(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except (OSError, UnicodeError, MemoryError):
        pass  # A missing console must not prevent launching the GUI.


def install_process_memory_limit(
    limit_bytes: int = DEFAULT_PROCESS_MEMORY_LIMIT,
) -> ProcessMemoryLimitResult:
    """Limit committed bytes, or return/log a reason and continue unchanged.

    The first successful installation is retained, including a smaller limit
    supplied by an embedding launcher. No enclosing job is queried or modified.
    """
    global _job_handle, _installed_result
    with _install_lock:
        if _installed_result is not None:
            return _installed_result
        if sys.platform != 'win32':
            result = ProcessMemoryLimitResult(False, limit_bytes, 'Windows job objects are unavailable on this platform')
            _report('Loka: memory limit unavailable: ' + result.reason)
            return result

        kernel = None
        handle = None
        operation = 'load kernel32'
        try:
            kernel = _load_kernel32()
            operation = 'CreateJobObjectW'
            handle = kernel.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _PROCESS_MEMORY | _SILENT_BREAKAWAY_OK
            info.ProcessMemoryLimit = limit_bytes
            operation = 'SetInformationJobObject'
            if not kernel.SetInformationJobObject(
                handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            operation = 'AssignProcessToJobObject'
            if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except (OSError, AttributeError) as error:
            if handle and kernel is not None:
                kernel.CloseHandle(handle)
            result = ProcessMemoryLimitResult(False, limit_bytes, f'{operation}: {error}')
            _report('Loka: memory limit unavailable: ' + result.reason)
            return result

        _job_handle = handle
        _installed_result = ProcessMemoryLimitResult(True, limit_bytes, 'Windows per-process committed-memory job limit enabled')
        return _installed_result


def exception_text(error: BaseException) -> str:
    """Give allocation failure a useful message without claiming its cause."""
    if not isinstance(error, MemoryError):
        return str(error)
    message = '内存不足，无法继续处理；请减少输入规模后重试。'
    if _installed_result is not None and _installed_result.enabled:
        limit = _installed_result.limit_bytes
        if limit >= 1024 ** 3 and limit % (1024 ** 2) == 0:
            label = f'{limit / 1024 ** 3:g} GiB'
        elif limit >= 1024 ** 2 and limit % (1024 ** 2) == 0:
            label = f'{limit // 1024 ** 2} MiB'
        else:
            label = f'{limit} 字节'
        message += f'Loka 提交内存上限为 {label}。'
    return message


def run_with_process_memory_limit(main: Callable[[], int]) -> int:
    """Entry-point guard; regular exported main() calls remain unrestricted."""
    install_process_memory_limit()
    try:
        return main()
    except MemoryError as error:
        _report('Loka: MemoryError: ' + exception_text(error))
        return 1
