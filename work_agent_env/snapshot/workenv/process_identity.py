"""Read-only local process identity; unsupported queries remain unknown."""
import os
from pathlib import Path


def _linux_identity(pid):
    result = {"pid": pid, "status": "unknown", "creation_id": None, "source": "linux-proc-stat"}
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        result["status"] = "not_found"
        return result
    except OSError:
        return result
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        # The comm field is parenthesized and may itself contain spaces or ')'.
        if "(" not in raw or ")" not in raw: return result
        fields = raw[raw.rfind(")") + 2:].split()
        state, start_ticks = fields[0], int(fields[19])
        if not boot_id or len(state) != 1 or start_ticks <= 0:
            return result
        result["creation_id"] = f"linux-proc:{boot_id}:{start_ticks}"
        result["status"] = "exited" if state in ("Z", "X", "x") else "alive"
    except (OSError, ValueError, IndexError):
        pass
    return result


def process_identity(pid):
    if type(pid) is not int or not 1<=pid<2**32:
        raise ValueError("PID must be a positive 32-bit integer")
    result={"pid":pid,"status":"unknown","creation_id":None,"source":"unsupported_platform"}
    if os.name!="nt":
        return _linux_identity(pid) if os.path.exists("/proc/self/stat") else result
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    result["source"]="win32-process-times-and-wait"
    handle=kernel.OpenProcess(0x1000|0x00100000,False,pid)
    if not handle:
        error=ctypes.get_last_error()
        result.update(status="not_found" if error==87 else "unknown",query_error=error)
        return result
    try:
        values=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in values]):
            result["query_error"]=ctypes.get_last_error()
            return result
        created=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
        state=kernel.WaitForSingleObject(handle,0)
        if state not in (0,0x102) or created==0:
            result["query_error"]=ctypes.get_last_error()
            return result
        result.update(status="alive" if state==0x102 else "exited",creation_id="win32-filetime:"+str(created))
        return result
    finally:
        kernel.CloseHandle(handle)
