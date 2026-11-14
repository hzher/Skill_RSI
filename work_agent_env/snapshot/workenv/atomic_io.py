"""Bounded retries around atomic replacement, without relaxing permissions."""
import math
import os
from pathlib import Path
import time


def _retryable(error):
    return os.name=="nt" and isinstance(error,PermissionError) and getattr(error,"winerror",None) in (5,32,33)


def replace_atomic(source,destination,*,retry_seconds=.5):
    """Retry only the rename, at most .5 s by default; never rerun a mutation.

    Windows handles without delete sharing can briefly prevent replacement.
    A persistent ACL/read-only denial still raises the original error. There
    is no unlink/truncate fallback, permission change or inter-writer lock.
    """
    if type(retry_seconds) not in (int,float) or not math.isfinite(retry_seconds) or not 0<=retry_seconds<=1:
        raise ValueError("Replacement retry budget must be in 0..1 seconds")
    source,destination=Path(source),Path(destination)
    deadline=time.monotonic()+retry_seconds
    delay=.005
    for attempt in range(12):
        try:
            source.replace(destination)
            return
        except OSError as exc:
            remaining=deadline-time.monotonic()
            if not _retryable(exc) or remaining<=0 or attempt==11:raise
            time.sleep(min(delay,remaining))
            delay=min(delay*2,.08)
