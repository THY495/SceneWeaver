"""P0 process containment: call only inside a dedicated disposable host process."""

import ctypes as c
from ctypes import wintypes as w


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_longlong),
        ("job_time", c.c_longlong),
        ("flags", w.DWORD),
        ("min_working_set", c.c_size_t),
        ("max_working_set", c.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", c.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", c.c_ulonglong * 6),
        ("process_memory", c.c_size_t),
        ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t),
        ("peak_job_memory", c.c_size_t),
    ]


kernel = c.WinDLL("kernel32", use_last_error=True)
kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
kernel.CreateJobObjectW.restype = w.HANDLE
kernel.GetCurrentProcess.restype = w.HANDLE
kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
kernel.CloseHandle.argtypes = [w.HANDLE]


def contain_current_host():
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise c.WinError(c.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)):
        error = c.get_last_error()
        kernel.CloseHandle(handle)
        raise c.WinError(error)
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = c.get_last_error()
        kernel.CloseHandle(handle)
        raise c.WinError(error)
    # Intentionally keep the non-inheritable handle open until host exit. Closing
    # it earlier would terminate this host as well as its contained descendants.
    return handle


def process_ids(handle):
    class PidList(c.Structure):
        _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("pids", c.c_size_t * 256)]

    result = PidList()
    if not kernel.QueryInformationJobObject(handle, 3, c.byref(result), c.sizeof(result), None):
        raise c.WinError(c.get_last_error())
    return list(result.pids[: result.count])
