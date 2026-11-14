"""Exclusive owned lock records; release refuses replaced ownership bytes."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid

from workenv.core import ResourceError,contained,now
from workenv.process_identity import process_identity


@contextmanager
def owned_lock(path,root,kind):
    root=Path(root).resolve()
    path=Path(path).absolute()
    relative=path.relative_to(root).as_posix()
    path=contained(root,relative)
    record={"schema_version":"workenv-lock-v0.1","workspace_owner":str(root),"resource":relative,
            "kind":kind,"owner_token":uuid.uuid4().hex,"acquired_at":now(),"process":process_identity(os.getpid())}
    encoded=(json.dumps(record,ensure_ascii=False,sort_keys=True)+"\n").encode("utf-8")
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try:
        with os.fdopen(descriptor,"wb") as stream:
            stream.write(encoded)
        yield record
    finally:
        # Never remove a replacement lock or a file whose original bytes were lost.
        try:
            checked=contained(root,relative)
            matching=checked.is_file() and checked.stat().st_size==len(encoded) and checked.read_bytes()==encoded
        except (ValueError,OSError):
            matching=False
        if not matching:
            raise ResourceError("Lock ownership changed; replacement was preserved: "+relative)
        path.unlink()
