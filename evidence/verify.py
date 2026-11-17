"""Verify published manifests, byte hashes and the protocol reference closure."""

import json
from pathlib import Path

from .archive import Archive
from .build import FILES, FOLDER
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


def _at(value, pointer):
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isdecimal() or (len(key) > 1 and key.startswith("0")):
                raise ValueError(f"Invalid array index in {pointer}")
            value = value[int(key)]
        else:
            value = value[key]
    return value


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
                selected = _at(documents[key], location["pointer"])
                if line is not None:
                    selected = selected.splitlines()[line - 1]
            else:
                if key not in lines:
                    lines[key] = archive.path(key[1]).read_bytes().splitlines()
                selected = lines[key][line - 1]
            if line is not None:
                if type(line) is not int or line < 1:
                    raise ValueError("Line number must be positive")
                if location.get("line_pointer") is not None:
                    _at(json.loads(selected), location["line_pointer"])
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
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
        if run_id in seen or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError(f"Duplicate or invalid run ID: {run_id}")
        seen.add(run_id)
        run = Archive(index_archive.path(Path("runs") / run_id), "run")
        roots = {"batch": batch, "run": run}
        _verify_ref(entry["artifact_manifest"], roots)
        records = _read_run(run)
        artifacts = records["artifacts.json"]
        inventory = {(row["source_root"], row["path"]) for row in artifacts["files"]}
        for row in artifacts["files"]:
            _verify_ref(row, roots)
        for name, data in records.items():
            if name != "artifacts.json" and ("run", name) not in inventory:
                raise ValueError(f"Derived file is missing from {run_id} inventory: {name}")
            for row in data if isinstance(data, list) else (data,):
                if row.get("run_id") != run_id or row.get("schema_version") != SCHEMA:
                    raise ValueError(f"Run/schema identity mismatch in {run_id}/{name}")
        for ref in references(records):
            _verify_ref(ref, roots, inventory)
        _verify_locators(records, roots)
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
