"""Read hash-bound source references and publish record files atomically."""

# Adapted from Skill_RSI_hzh/skill_discovery/artifacts.py (MIT).
import gzip
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .archive import safe_path
from .schema import SCHEMA as EVIDENCE_SCHEMA, encode

SCHEMA = "SkillRSI-SkillDiscovery-v1"
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_bytes(path, data, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            if replace:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, encode(value) + b"\n")


def inside(root, relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Invalid relative path: {relative!r}")
    return safe_path(Path(root).absolute(), path)


@contextmanager
def file_lock(path):
    """Keep concurrent publishers on one lock file, including on Windows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def indexed_run(archive, run_id, index=None):
    index = index if index is not None else read_json(inside(archive, "batch_index.json"))
    if index.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("Unsupported evidence index")
    matches = [row for row in index["runs"] if row["run_id"] == run_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one indexed run for {run_id}")
    root = inside(archive, f"runs/{identifier(run_id)}")
    artifacts = checked_bytes(root, "artifacts.json", matches[0]["artifact_manifest"])
    manifest = json.loads(artifacts)
    if manifest.get("run_id") != run_id or manifest.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("Artifact manifest does not match its indexed run")
    rows = [ref for ref in manifest["files"] if ref["source_root"] == "run"]
    refs = {ref["path"]: ref for ref in rows}
    if len(refs) != len(rows):
        raise ValueError(f"Duplicate indexed artifact for {run_id}")
    return root, refs, artifacts


def checked_bytes(root, name, ref):
    if ref["path"] != name or ref["source_root"] != "run":
        raise ValueError(f"Invalid indexed file reference for {name}")
    return source_bytes(root, ref)


def source_bytes(root, ref):
    if root is None:
        raise ValueError(f"Missing authorized source root for {ref['path']}")
    contents = inside(root, ref["path"]).read_bytes()
    if len(contents) != ref["bytes"] or digest(contents) != ref["sha256"]:
        raise ValueError(f"Hash or size mismatch for {ref['path']}")
    return contents


def at_pointer(value, address):
    if not isinstance(address, str) or (address and not address.startswith("/")):
        raise ValueError("Invalid JSON pointer")
    for raw_token in address.split("/")[1:]:
        if re.search(r"~(?![01])", raw_token):
            raise ValueError("Invalid JSON pointer escape")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError(f"Invalid array index in {address}")
            value = value[int(token)]
        else:
            value = value[token]
    return value


def physical_line(contents, index):
    if type(index) is not int or index < 1:
        raise ValueError("Invalid source line number")
    lines = contents.split(b"\n" if isinstance(contents, bytes) else "\n")
    if index == len(lines) and not lines[-1]:
        raise IndexError("Source line is beyond end of file")
    return lines[index - 1]


def read_ref(batch, run, ref):
    root = {"batch": batch, "run": run}[ref["source_root"]]
    data = source_bytes(root, ref)
    path = Path(ref["path"])
    if path.suffix == ".gz":
        data = gzip.decompress(data)
        path = path.with_suffix("")
    location = ref["locator"]
    kind = location["type"]
    if kind == "line":
        value = json.loads(physical_line(data, location["line"]))
    elif kind == "json_pointer":
        value = at_pointer(json.loads(data), location["pointer"])
        if "line" in location:
            value = json.loads(physical_line(value, location["line"]))
    elif kind == "file":
        if path.suffix in {".jsonl", ".ndjson"}:
            value = [json.loads(line) for line in data.split(b"\n") if line.strip()]
        else:
            value = json.loads(data)
    else:
        raise ValueError("Unsupported source locator")
    return at_pointer(value, location.get("line_pointer", ""))
