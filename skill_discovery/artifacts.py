import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile


SCHEMA = "SkillRSI-SkillDiscovery-v1"
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def read_json(path):
    return json.loads(path.read_bytes())


def write_bytes(path, data, *, replace=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
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
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid relative path: {relative!r}")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes its root: {relative!r}")
    return target


def indexed_run(archive, run_id, index=None):
    index = index if index is not None else read_json(inside(archive, "batch_index.json"))
    matches = [row for row in index["runs"] if row["run_id"] == run_id]
    if len(matches) != 1:
        raise ValueError(f"expected one indexed run for {run_id}")
    root = inside(archive, f"runs/{identifier(run_id)}")
    artifacts = checked_bytes(root, "artifacts.json", matches[0]["artifact_manifest"])
    manifest = json.loads(artifacts)
    if manifest.get("run_id") != run_id:
        raise ValueError("artifact manifest does not match its indexed run")
    refs = {ref["path"]: ref for ref in manifest["files"] if ref["source_root"] == "run"}
    return root, refs, artifacts


def checked_bytes(root, name, ref):
    if ref["path"] != name or ref["source_root"] != "run":
        raise ValueError(f"invalid indexed file reference for {name}")
    contents = inside(root, name).read_bytes()
    if len(contents) != ref["bytes"] or digest(contents) != ref["sha256"]:
        raise ValueError(f"hash mismatch for indexed file {name}")
    return contents


def _pointer(value, address):
    if not isinstance(address, str) or (address and not address.startswith("/")):
        raise ValueError("invalid JSON pointer")
    for key in address.split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
    return value


def _row(contents, index):
    if type(index) is not int or index < 1:
        raise ValueError("invalid JSONL line")
    return json.loads(contents.splitlines()[index - 1])


def read_ref(batch, run, ref):
    root_type = ref["source_root"]
    root = {"batch": batch, "run": run}[root_type]
    path = inside(root, ref["path"])
    data = path.read_bytes()
    if len(data) != ref["bytes"] or digest(data) != ref["sha256"]:
        raise ValueError("native source hash differs from the frozen reference")
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    locator = ref["locator"]
    kind = locator["type"]
    if kind == "line":
        data = _row(data, locator["line"])
    elif kind == "json_pointer":
        data = _pointer(json.loads(data), locator["pointer"])
        if "line" in locator:
            data = _row(data, locator["line"])
    elif kind == "file":
        data = [json.loads(line) for line in data.splitlines() if line.strip()] if path.suffix == ".jsonl" else json.loads(data)
    else:
        raise ValueError("unsupported source locator")
    return _pointer(data, locator.get("line_pointer", ""))
