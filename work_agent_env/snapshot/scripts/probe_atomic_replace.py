"""Controlled Windows deny-delete handle probe; no task or model execution."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workenv.core import save,read_json


@contextmanager
def deny_delete(path,seconds=.1):
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    close=kernel.CloseHandle
    close.argtypes=[wintypes.HANDLE]
    close.restype=wintypes.BOOL
    handle=create(str(path),0x80000000,3,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    release=threading.Event()
    def worker():
        release.wait(seconds)
        close(handle)
    thread=threading.Thread(target=worker)
    thread.start()
    try:yield
    finally:
        release.set()
        thread.join(timeout=2)
        if thread.is_alive():raise RuntimeError("Probe handle thread did not close")


def main():
    if os.name!="nt":
        print(json.dumps({"supported":False,"reason":"Windows-only handle probe"}))
        return 0
    with tempfile.TemporaryDirectory() as temporary:
        path=Path(temporary)/"record.json"
        save(path,{"version":1})
        start=time.monotonic()
        with deny_delete(path):
            try:
                save(path,{"version":2})
                completed=True
                error=None
            except PermissionError as exc:
                completed=False
                error={"kind":type(exc).__name__,"winerror":exc.winerror}
        result={"fixture":"owned temporary file held without FILE_SHARE_DELETE for .1 seconds",
                "replacement_completed":completed,"error":error,"elapsed_seconds":time.monotonic()-start,
                "saved_version":read_json(path)["version"],"uncommitted_temporaries":len(list(path.parent.glob("*.tmp"))),
                "real_model_calls":0,"training_updates":0}
        print(json.dumps(result))
    return 0


if __name__=="__main__":raise SystemExit(main())
