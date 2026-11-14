"""Owned regular source files and bounded bundles with byte-exact manifests.

Checks reduce accidental inclusion; this is not isolation from hostile writers.
"""
import hashlib
import json
from pathlib import Path
import stat
import uuid
import zipfile

from workenv.atomic_io import replace_atomic
from workenv.core import contained,no_links,now
from workenv.handoff import MAX_ARCHIVE_BYTES,MAX_CONTENT_BYTES,validate_member
from workenv.leases import owned_lock

SOURCE_DIRS=("workenv","adapters","scripts","configs","docs","tasks","tests","reports")
MAINLINE_DIRS=("integrations","training","evaluation","responses_api_agents","resources_servers")
MAX_MEMBERS=1999  # Plus the manifest: extractor allows at most 2000.


def _root(root):
    root=Path(root).absolute()
    no_links(root,root)
    return root.resolve()


def _check_path(root,path):
    no_links(path,root)
    observed=path.lstat()
    if getattr(observed,"st_file_attributes",0)&0x400:
        raise ValueError("Reparse source entries are forbidden")
    if stat.S_ISREG(observed.st_mode) and observed.st_nlink!=1:
        raise ValueError("Hard-linked or unverified source files are forbidden")
    return observed


def source_files(root,*,include_mainline=False):
    root=_root(root)
    paths=[]
    for name in ("README.md",".gitignore"):
        path=contained(root,name)
        if not stat.S_ISREG(_check_path(root,path).st_mode):raise ValueError("Missing regular source entry")
        paths.append(path)
    pending=[contained(root,name) for name in SOURCE_DIRS+(MAINLINE_DIRS if include_mainline else ())]
    visited=0
    while pending:
        directory=pending.pop()
        if not stat.S_ISDIR(_check_path(root,directory).st_mode):raise ValueError("Source directory is not regular")
        for path in directory.iterdir():
            visited+=1
            if visited>8192:raise ValueError("Source inventory exceeds entry budget")
            mode=_check_path(root,path).st_mode
            if stat.S_ISDIR(mode):
                if path.name!="__pycache__" and not path.name.startswith("."):pending.append(path)
            elif stat.S_ISREG(mode):
                if path.suffix in (".pyc",".pyo") or path.name.startswith(".") and path.name!=".env.example":continue
                paths.append(path)
            else:raise ValueError("Only regular source entries are supported")
            if len(paths)>MAX_MEMBERS:raise ValueError("Source member count exceeds bundle budget")
    return sorted(paths)


def evidence_files(root,names):
    root=_root(root)
    paths=[]
    for name in names:
        validate_member(name)
        if "/" in name:raise ValueError("Selected evidence names must be simple filenames")
        path=contained(root,"evidence/"+name)
        if path.exists():
            if not stat.S_ISREG(_check_path(root,path).st_mode):raise ValueError("Selected evidence is not regular")
            paths.append(path)
    batches=contained(root,"evidence/batches")
    if batches.exists():
        _check_path(root,batches)
        for index,directory in enumerate(batches.iterdir()):
            if index>=MAX_MEMBERS:raise ValueError("Batch inventory exceeds budget")
            mode=_check_path(root,directory).st_mode
            if not stat.S_ISDIR(mode):continue
            path=contained(root,(directory.relative_to(root)/"batch.json").as_posix())
            if path.exists():
                if not stat.S_ISREG(_check_path(root,path).st_mode):raise ValueError("Batch metadata is not regular")
                paths.append(path)
    return paths


def read_owned(root,path):
    before=_check_path(root,path)
    if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_CONTENT_BYTES:raise ValueError("Source file exceeds supported regular-file budget")
    # The read is bounded even if a trusted writer grows the file after stat.
    with path.open("rb") as stream:data=stream.read(MAX_CONTENT_BYTES+1)
    after=_check_path(root,path)
    if len(data)>MAX_CONTENT_BYTES or len(data)!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        raise ValueError("Source changed while being read")
    return data


def snapshot(root,evidence_names=None,*,include_mainline=False):
    root=_root(root)
    files={}
    folded=set()
    total=0
    extras=evidence_files(root,evidence_names) if evidence_names is not None else []
    for path in sorted(set(source_files(root,include_mainline=include_mainline)+extras)):
        name=path.relative_to(root).as_posix()
        validate_member(name)
        if name.casefold() in folded:raise ValueError("Case-aliased source names")
        folded.add(name.casefold())
        data=read_owned(root,path)
        total+=len(data)
        if total>MAX_CONTENT_BYTES or len(files)>=MAX_MEMBERS:raise ValueError("Bundle content or member budget exceeded")
        files[name]=data
    return files


def build_bundle(root,evidence_names=(),destination="evidence/handoff/local_review_bundle.zip",*,include_mainline=False):
    root=_root(root)
    validate_member(destination)
    output=contained(root,destination)
    if not destination.replace("\\","/").startswith("evidence/handoff/") or output.suffix!=".zip":
        raise ValueError("Source bundles must be evidence/handoff/*.zip")
    output.parent.mkdir(parents=True,exist_ok=True)
    with owned_lock(output.parent/".package.lock",root,"source_bundle"):
        files=snapshot(root,evidence_names,include_mainline=include_mainline)
        hashes={name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
        manifest={"format":"office-environment-review-v1","built_at":now(),"files":hashes,
                  "scope":"source, synthetic fixtures, selected summaries and diagnostic examples",
                  "excluded":["runtime binaries","venv","upstream checkouts","raw command logs","full run/private state","real .env"],
                  "replication_claim":"none until verified; same-host reproduction is not independent review or clean-machine replication"}
        encoded=(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
        if len(encoded)+sum(map(len,files.values()))>MAX_CONTENT_BYTES:raise ValueError("Manifest exceeds content budget")
        temporary=output.with_name(".bundle_"+uuid.uuid4().hex+".tmp")
        checksum_temp=output.with_name(".checksum_"+uuid.uuid4().hex+".tmp")
        try:
            with zipfile.ZipFile(temporary,"x",compression=zipfile.ZIP_DEFLATED) as archive:
                for name,data in files.items():archive.writestr(name,data)
                archive.writestr("BUNDLE_MANIFEST.json",encoded)
            if temporary.stat().st_size>MAX_ARCHIVE_BYTES:raise ValueError("Compressed bundle exceeds budget")
            current=snapshot(root,evidence_names,include_mainline=include_mainline)
            if {name:hashlib.sha256(data).hexdigest() for name,data in current.items()}!=hashes:
                raise ValueError("Source inventory changed while packaging")
            checksum=hashlib.sha256(temporary.read_bytes()).hexdigest()
            checksum_temp.write_text(checksum+"\n",encoding="ascii")
            replace_atomic(temporary,output)
            replace_atomic(checksum_temp,output.with_suffix(".zip.sha256"))
            return {"built_at":now(),"bundle":output.relative_to(root).as_posix(),"sha256":checksum,"files":len(files),"bytes":output.stat().st_size}
        finally:
            for path in (temporary,checksum_temp):
                try:path.unlink(missing_ok=True)
                except OSError:pass
