"""Linux-only pidfd handle for deliberately faulting an attested diagnostic child.

Never fall back to signalling a bare PID: that would allow PID reuse to target
an unrelated process. The syscall numbers are from Linux x86_64 unistd_64.h.
"""
import ctypes
import errno
import os
import platform
import select
import signal
import sys

from workenv.core import ResourceError
from workenv.process_identity import process_identity

_PIDFD_SEND_SIGNAL = 424
_PIDFD_OPEN = 434


class LinuxAttestedChild:
    def __init__(self, identity):
        if sys.platform != "linux" or platform.machine() != "x86_64":
            raise ResourceError("Linux diagnostic pidfd requires x86_64")
        if not isinstance(identity, dict) or identity.get("status") != "alive" or not identity.get("creation_id"):
            raise ResourceError("Diagnostic child lacks a live creation identity")
        self.identity = identity
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        fd = self.libc.syscall(ctypes.c_long(_PIDFD_OPEN), ctypes.c_int(identity["pid"]), ctypes.c_uint(0))
        if fd < 0:
            error = ctypes.get_errno()
            raise ResourceError(f"Cannot open an attested pidfd: errno {error}")
        self.fd = fd
        if process_identity(identity["pid"]) != identity:
            self.close()
            raise ResourceError("Diagnostic pidfd belongs to another creation identity")

    def _exited(self, timeout_ms=0):
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        return bool(poller.poll(timeout_ms))

    def terminate_if_alive(self):
        if self.fd is None:
            raise ResourceError("Diagnostic pidfd has been closed")
        if self._exited():
            return False
        if process_identity(self.identity["pid"]) != self.identity:
            # The child can exit between the first poll and /proc inspection.
            # Only the already-open pidfd can prove that race harmless; an
            # unknown/reused PID alone never authorizes signalling or cleanup.
            # A multithreaded Node process may become a /proc zombie just
            # before its pidfd becomes readable. Await the same handle briefly;
            # neither a zombie status nor a reused/unknown PID permits a signal.
            if self._exited(1000):
                return False
            raise ResourceError("Diagnostic child identity changed before fault injection")
        result = self.libc.syscall(ctypes.c_long(_PIDFD_SEND_SIGNAL), ctypes.c_int(self.fd),
                                   ctypes.c_int(signal.SIGKILL), ctypes.c_void_p(), ctypes.c_uint(0))
        if result < 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH and self._exited():
                return False
            raise ResourceError(f"Attested diagnostic child signal failed: errno {error}")
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        if not poller.poll(10000):
            raise ResourceError("Attested diagnostic child exit was not observed")
        return True

    def close(self):
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None
