"""Map selected files to source events and checkpoint them after durable writes."""

from collections.abc import Mapping
from pathlib import Path

from ..evidence.archive import safe_path
from ..evidence.io import file_lock, read_json
from ..evidence.journal import EventJournal
from ..evidence.schema import identifier
from .local import LocalFileSource

_FORMATS = {".json", ".jsonl", ".ndjson"}


def _field(record, address):
    if not isinstance(address, str) or not address:
        raise ValueError("Mapping fields must name a source field")
    if address in record:
        return record[address]
    value = record
    for part in address.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _mapped(row, mapping, project, source, batch):
    selectors = mapping.get("fields", {})
    if selectors:
        values = {key: _field(row, address) for key, address in selectors.items()}
    else:
        values = dict(row)
    result_project = values.get("project_id") or project
    if project is not None and result_project != project:
        raise ValueError(f"Source event belongs to {result_project!r}, outside project {project!r}")
    if not isinstance(result_project, str) or not result_project:
        raise ValueError("A project_id setting or mapped field is required")
    if values.get("source_id") not in {None, source}:
        raise ValueError("Source record belongs to another source_id")
    kind = values.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError("A type field is required")
    types = mapping.get("types")
    if types and kind not in types:
        values["mapping_status"] = "unmapped_type"
    mapped_kind = (types or {}).get(kind, kind)
    native_run_id = values.get("run_id")
    return {
        **values, "project_id": result_project, "source_id": source,
        "run_id": str(native_run_id or identifier(result_project, source, batch)),
        "run_id_derived": native_run_id is None, "native_type": values.get("native_type") or kind,
        "type": mapped_kind, "mapping_version": str(mapping.get("version", "1")),
    }


def ingest(source, journal, *, source_id, batch_id, mapping=None, project_id=None, stream_name="records"):
    """Consume any registered reader using the same durable source checkpoint contract."""
    if not isinstance(source_id, str) or not source_id or not isinstance(batch_id, str) or not batch_id:
        raise ValueError("The source and batch must have stable IDs")
    mapping = mapping or {}
    stream = identifier("stream", project_id, source_id, batch_id, stream_name)
    counts = {"added": 0, "duplicates": 0, "skipped": 0}
    with file_lock(journal.root / ".locks" / (stream + ".lock")):
        cursor = journal.cursor(stream)
        for row, origin, next_cursor in source.read(cursor):
            if row is None:
                journal.skip(origin, next_cursor, stream=stream)
                if next_cursor.get("skip_reason") != "blank":
                    counts["skipped"] += 1
                continue
            event = _mapped(row, mapping, project_id, source_id, batch_id)
            result = journal.append(
                {**event, "source_stream": stream}, origin=origin, cursor=next_cursor,
                stream=stream, sequence_is_identity=mapping.get("sequence_is_identity") is True,
            )
            counts["duplicates" if result["duplicate"] else "added"] += 1
    return counts


def _selected(path):
    if not path.is_dir():
        return [(path.name, path)]
    selected = []
    for entry in sorted(path.rglob("*")):
        if entry.suffix not in _FORMATS or not entry.is_file():
            continue
        try:
            candidate = safe_path(path, entry)
        except ValueError:
            continue
        selected.append((entry.relative_to(path).as_posix(), candidate))
    return selected


def import_source(path, *, output, mapping=None, project_id=None, source_id=None,
                  batch_id=None, store=None, seal=True, follow=False):
    path = Path(path).resolve(strict=True)
    output = Path(output).resolve()
    if path.is_dir() and output.is_relative_to(path):
        raise ValueError("Keep imported evidence output outside the selected source directory")
    source_id = source_id or path.name
    batch_id = batch_id or path.name
    config = read_json(mapping) if isinstance(mapping, (str, Path)) else mapping
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("Field mapping must be an object")
    for key in ("fields", "types"):
        if config and key in config and not isinstance(config[key], Mapping):
            raise ValueError(f"Mapping {key} must be an object")
    results = []
    selected = _selected(path)
    if not selected:
        raise ValueError(f"No supported source files in {path}")
    journal = EventJournal(store or output / ".source")
    if path.is_dir() and journal.root.is_relative_to(path):
        raise ValueError("Keep the source journal outside the selected source directory")
    for name, item in selected:
        results.append(ingest(
            LocalFileSource(item, name=name, follow=follow), journal,
            source_id=source_id, batch_id=batch_id, mapping=config, project_id=project_id, stream_name=name,
        ))
    result = journal.publish(output, seal=seal)
    return {**result, "journal": str(journal.root), **{
        key: sum(item[key] for item in results) for key in ("added", "duplicates", "skipped")
    }}
