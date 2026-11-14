"""Validate a privately built original MCP component before diagnostic launch."""
import hashlib
import json
from pathlib import Path

from workenv.core import contained, no_links
from workenv.process_identity import process_identity


def recover_component_lock(root, base):
    root, base = Path(root).resolve(), Path(base).resolve()
    if not base.is_relative_to(root / ".runtime/howtocook-build"): raise ValueError("Foreign component build root")
    path = base / ".component.lock"
    no_links(path, root)
    if not path.exists(): return None
    if path.stat().st_size > 8192: raise ValueError("Unexpected component lock size")
    raw = path.read_bytes()
    record = json.loads(raw)
    if (record.get("schema_version") != "workenv-lock-v0.1" or record.get("kind") != "howtocook_component"
        or record.get("workspace_owner") != str(root) or record.get("resource") != path.relative_to(root).as_posix()):
        raise ValueError("Component lock ownership differs")
    identity = process_identity(record["process"]["pid"])
    if identity["status"] not in ("not_found", "exited"):
        raise ValueError("Component lock owner is alive or unknown; keep its lock")
    archive = base / ("recovered-lock-" + hashlib.sha256(raw).hexdigest() + ".json")
    no_links(archive, root)
    if archive.exists():
        if archive.read_bytes() != raw: raise ValueError("Component lock archive was replaced")
    else:
        with archive.open("xb") as stream: stream.write(raw)
    if path.read_bytes() != raw: raise ValueError("Component lock changed during recovery")
    path.unlink()
    return archive.relative_to(root).as_posix()


def verify_component(root, receipt):
    root = Path(root).resolve()
    mode = (receipt.get("compilation_mode"), receipt.get("typecheck_completed"))
    if (receipt.get("schema_version") != "howtocook-offline-build-v1" or receipt.get("passed") is not True
        or mode not in (("full_typecheck", True), ("bounded_emit_only", False))):
        raise ValueError("Original recipe MCP build has not passed")
    base = contained(root, receipt["build_ref"])
    if not base.is_relative_to(root / ".runtime/howtocook-build"):
        raise ValueError("Original recipe MCP escaped its owned build root")
    no_links(base, root)
    hashes = receipt.get("source_sha256", {})
    if not hashes or len(hashes) > 16: raise ValueError("Missing original MCP build input bindings")
    for name, expected in hashes.items():
        if name.split("/", 1)[0] not in ("scripts", "training", "configs"): raise ValueError("Unsupported build source reference")
        path = contained(root, name)
        no_links(path, root)
        if path.stat().st_size > 2 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Original MCP build inputs changed")
    compiled = receipt.get("compiled_sha256", {})
    if not 1 <= len(compiled) <= 64 or any(not name.startswith("build/") for name in compiled):
        raise ValueError("Unexpected compiled MCP member inventory")
    actual = set()
    total = 0
    for path in (base / "build").rglob("*"):
        no_links(path, root)
        if not path.is_file(): continue
        name = path.relative_to(base).as_posix()
        total += path.stat().st_size
        if total > 8 * 1024 * 1024 or len(actual) >= 64 or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Compiled MCP source exceeds budget")
        if compiled.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Compiled original MCP source was replaced")
        actual.add(name)
    if set(compiled) != actual or "build/index.js" not in actual:
        raise ValueError("Compiled original MCP file inventory changed")
    return base


NETWORK_GUARD = '''import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import {syncBuiltinESMExports} from 'node:module';
const path=process.env.WORKENV_RECIPE_GUARD;
let calls=0;
function save(){fs.writeFileSync(path,JSON.stringify({pid:process.pid,instrumented_network_calls:calls}));}
function deny(){calls++;save();throw new Error('Original recipe MCP diagnostic forbids network requests');}
globalThis.fetch=deny;
http.request=deny;http.get=deny;https.request=deny;https.get=deny;
syncBuiltinESMExports();save();
'''
