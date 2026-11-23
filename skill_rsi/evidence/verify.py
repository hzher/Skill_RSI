# Adapted from Skill_RSI_hzh/evidence/verify.py (MIT).
"""Verify published manifests, byte hashes and the protocol reference closure."""

import gzip
import json
from pathlib import Path

from .archive import Archive
from .build import FILES, FOLDER, OBSERVATIONS_FILE
from .io import at_pointer, identifier, physical_line
from ..contracts.events import DIAGNOSTICS_SCHEMA
from .schema import SCHEMA, references


def _verify_ref(ref, roots, inventory=None):
    scope = ref["source_root"]
    if scope not in roots or roots[scope] is None:
        raise ValueError(f"Source {scope!r} is needed to verify {ref['path']}")
    raw = roots[scope].ref(ref["path"])
    if (raw["sha256"], raw["bytes"]) != (ref["sha256"], ref["bytes"]):
        raise ValueError(f"Evidence hash or size mismatch: {ref['path']}")
    if inventory is not None and (scope, ref["path"]) not in inventory:
        raise ValueError(f"Reference missing from artifact inventory: {ref['path']}")
    locator = ref["locator"]
    kind = locator.get("type")
    valid = kind == "file" or (
        kind == "line" and type(locator.get("line")) is int and locator["line"] > 0
    ) or (
        kind == "json_pointer" and isinstance(locator.get("pointer"), str)
        and (not locator["pointer"] or locator["pointer"].startswith("/"))
    )
    if not valid:
        raise ValueError(f"Invalid locator for {ref['path']}: {locator}")


def _verify_locators(records, roots):
    documents = {}
    lines = {}
    checked = set()
    for ref in references(records):
        location = ref["locator"]
        kind = location["type"]
        if kind == "file":
            continue
        key = ref["source_root"], ref["path"]
        selector = key, location.get("pointer"), location.get("line"), location.get("line_pointer")
        if selector in checked:
            continue
        checked.add(selector)
        archive = roots[key[0]]
        try:
            line = location.get("line")
            if kind == "json_pointer":
                if key not in documents:
                    documents[key] = archive.json(key[1])
                selected = at_pointer(documents[key], location["pointer"])
                if line is not None:
                    selected = physical_line(selected, line)
            else:
                if key not in lines:
                    path = archive.path(key[1])
                    data = path.read_bytes()
                    lines[key] = (gzip.decompress(data) if path.suffix == ".gz" else data).split(b"\n")
                if type(line) is not int or line < 1 or (line == len(lines[key]) and not lines[key][-1]):
                    raise IndexError("Invalid source line number")
                selected = lines[key][line - 1]
            if line is not None:
                if type(line) is not int or line < 1:
                    raise ValueError("Line number must be positive")
                if location.get("line_pointer") is not None:
                    at_pointer(json.loads(selected), location["line_pointer"])
        except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError(f"Unresolved evidence locator in {ref['path']}: {location}") from exc


def _read_run(archive):
    records = {}
    for name in (*FILES, "artifacts.json"):
        if name.endswith(".jsonl"):
            records[name] = [row for row, _, _ in archive.jsonl(name)]
        else:
            records[name] = archive.json(name)
    if archive.diagnostics:
        raise ValueError(f"Malformed derived records in {archive.root}")
    return records


def verify(output, *, batch_dir=None):
    output = Path(output).resolve(strict=True)
    index_archive = Archive(output)
    index = index_archive.json("batch_index.json")
    if index.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported evidence index")
    if batch_dir is None and index.get("batch_local") and output.parent.name == FOLDER.parent.name:
        batch_dir = output.parent.parent
    batch = Archive(batch_dir) if batch_dir is not None else None
    seen = set()
    for entry in index["runs"]:
        run_id = entry["run_id"]
        if identifier(run_id) in seen:
            raise ValueError(f"Duplicate run ID: {run_id}")
        seen.add(run_id)
        run = Archive(index_archive.path(Path("runs") / run_id), "run")
        roots = {"batch": batch, "run": run}
        manifest_ref = entry["artifact_manifest"]
        if (manifest_ref["source_root"], manifest_ref["path"]) != ("run", "artifacts.json"):
            raise ValueError(f"Run artifact manifest has wrong location: {run_id}")
        _verify_ref(manifest_ref, roots)
        records = _read_run(run)
        artifacts = records["artifacts.json"]
        inventory = {(row["source_root"], row["path"]) for row in artifacts["files"]}
        if len(inventory) != len(artifacts["files"]):
            raise ValueError(f"Duplicate file in {run_id} artifact inventory")
        for row in artifacts["files"]:
            _verify_ref(row, roots)
        for name, data in records.items():
            if name != "artifacts.json" and ("run", name) not in inventory:
                raise ValueError(f"Derived file is missing from {run_id} inventory: {name}")
            for row in data if isinstance(data, list) else (data,):
                if row.get("run_id") != run_id or row.get("schema_version") != SCHEMA:
                    raise ValueError(f"Run/schema identity mismatch in {run_id}/{name}")
        extensions = []
        for extension in artifacts.get("extensions", ()):
            if (extension["source_root"], extension["path"]) != ("run", OBSERVATIONS_FILE):
                raise ValueError(f"Unsupported run extension for {run_id}")
            observed = run.json(extension["path"])
            if (observed.get("schema_version"), observed.get("run_id"), observed.get("source_fingerprint")) != (
                DIAGNOSTICS_SCHEMA, run_id, records["manifest.json"].get("source_fingerprint")
            ):
                raise ValueError(f"Observation schema or run identity mismatch: {run_id}")
            extensions.append(observed)
        complete = [records, extensions]
        for ref in references(complete):
            _verify_ref(ref, roots, inventory)
        _verify_locators(complete, roots)
        events = records["events.jsonl"]
        event_ids = {row["event_id"] for row in events}
        if len(event_ids) != len(events):
            raise ValueError(f"Duplicate event IDs in {run_id}")
        for row in (*events, *records["requests/index.jsonl"], *records["context_episodes.jsonl"],
                    *artifacts["versions"]):
            if not set(row.get("source_event_ids") or ()) <= event_ids:
                raise ValueError(f"Unknown source event reference in {run_id}")
        run.assert_unchanged()
    for entry in index.get("harness_imports", {}).values():
        for ref in references(entry.get("coverage", {})):
            if batch is not None:
                _verify_ref(ref, {"batch": batch})
    if batch:
        batch.assert_unchanged()
    index_archive.assert_unchanged()
    return {"archive": str(output), "run_count": len(seen), "integrity": "verified"}
