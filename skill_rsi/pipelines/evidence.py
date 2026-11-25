"""Assemble native, file and installed custom evidence readers."""
from importlib.metadata import entry_points
from pathlib import Path


def ingest(path, *, output, host=None, format=None, options=None, reader=None, **settings):
    if host and reader:
        raise ValueError("select either a native host reader or a custom data source")
    if host:
        from ..evidence.build import build
        return build(Path(path), host, output=output, format=format, options=options,
                     seal=settings.get("seal", True))
    if reader:
        return _custom_source(reader, path, output, options or {}, settings)
    from ..sources.ingest import import_source
    return import_source(path, output=output, **settings)


def _custom_source(name, path, output, options, settings):
    from ..evidence.io import read_json
    from ..evidence.journal import EventJournal
    from ..sources.ingest import ingest as consume

    extensions = tuple(entry_points(group="skill_rsi.sources", name=name))
    if len(extensions) != 1:
        raise ValueError(f"unknown or ambiguous data source: {name}")
    config = settings.get("mapping")
    if isinstance(config, (str, Path)):
        config = read_json(config)
    if config is not None and not isinstance(config, dict):
        raise ValueError("custom source mapping must be an object")
    journal = EventJournal(settings.get("store") or output / ".source")
    source = extensions[0].load()(path=path, options=options, follow=settings.get("follow", False))
    counts = consume(source, journal, source_id=settings.get("source_id") or name,
                     batch_id=settings.get("batch_id") or path.name, mapping=config,
                     project_id=settings.get("project_id"), stream_name=path.name)
    publication = journal.publish(output, seal=settings.get("seal", True))
    return {**publication, **counts, "journal": str(journal.root)}
