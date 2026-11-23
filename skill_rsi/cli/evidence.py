"""Offline evidence and approval commands."""
from pathlib import Path

from . import command, config_arg


def _ingest(args):
    from ..arguments.config import load_config
    from ..pipelines.evidence import ingest

    if args.config:
        config = load_config(args.config)
        source = config["source"]
        base = args.config.resolve().parent
        args.path = args.path or base / source["path"]
        args.host = args.host or source.get("host")
        args.reader = args.reader or source.get("reader")
        args.format = args.format or source.get("format")
        args.mapping = args.mapping or source.get("mapping")
        if isinstance(args.mapping, str):
            args.mapping = base / args.mapping
        args.source_id = args.source_id or source.get("id")
        args.batch_id = args.batch_id or source.get("batch_id")
        args.project_id = args.project_id or source.get("project_id")
    else:
        source = {}
    if args.path is None:
        raise ValueError("a source path or config is required")
    options = load_config(args.options) if args.options else source.get("options")
    return ingest(args.path, output=args.output, host=args.host, format=args.format, options=options, reader=args.reader,
                  mapping=args.mapping, project_id=args.project_id, source_id=args.source_id,
                  batch_id=args.batch_id, store=args.store, follow=args.follow, seal=not args.unsealed)


def _verify(args):
    from ..evidence.verify import verify
    return verify(args.archive, batch_dir=args.batch_dir)


def _inspect(args):
    import json
    from ..evidence.io import checked_bytes, indexed_run, inside, read_json

    index = read_json(inside(args.archive, "batch_index.json"))
    if not args.run_id:
        return index
    root, refs, raw = indexed_run(args.archive, args.run_id, index)
    if args.view == "artifacts":
        return json.loads(raw)
    view = {"manifest": "manifest.json", "observations": "observations.json",
            "events": "events.jsonl", "requests": "requests/index.jsonl",
            "scores": "scores.jsonl", "episodes": "context_episodes.jsonl"}[args.view]
    if view not in refs:
        extensions = {ref["path"]: ref for ref in json.loads(raw).get("extensions", [])}
        if view not in extensions:
            return {"run_id": args.run_id, "status": "unknown", "reason": "view_unavailable"}
        refs[view] = extensions[view]
    contents = checked_bytes(root, view, refs[view])
    if view.endswith(".jsonl"):
        return [json.loads(line) for line in contents.splitlines() if line.strip()]
    return json.loads(contents)


def _journal(args):
    from ..evidence.journal import EventJournal
    return EventJournal(args.journal).publish(args.output, seal=not args.unsealed)


def _review(args):
    from ..arguments.config import load_config
    from ..review.snapshot import create_snapshot
    index, windows, refs = create_snapshot(args.archive, load_config(args.config), args.output,
                                           batch_dir=args.batch_dir, purpose=args.purpose)
    return {"snapshot": args.output, "index": index, "windows": len(windows), "source_events": len(refs)}


def _audit(args):
    from ..review.snapshot import load_snapshot
    index, windows, refs = load_snapshot(args.snapshot, purpose=args.purpose)
    return {"index": index, "windows": len(windows), "source_events": len(refs)}


def register(routes):
    parser = command(routes, "import", "Import selected standard or native historical data", _ingest, aliases=("build",))
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", choices=("dsh", "claude_code", "codex"))
    parser.add_argument("--format")
    parser.add_argument("--reader", help="name of an installed skill_rsi.sources data source")
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--source-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--store", type=Path)
    parser.add_argument("--follow", action="store_true", help="consume a complete incremental prefix and commit its cursor")
    parser.add_argument("--unsealed", action="store_true")
    config_arg(parser)
    config_arg(parser, "--options")
    parser = command(routes, "verify", "Verify the recorded source and evidence digests", _verify)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--batch-dir", type=Path)
    parser = command(routes, "inspect", "Read the archived run index, lineage or observation evidence", _inspect)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--view", choices=("manifest", "observations", "events", "requests", "scores",
                                          "episodes", "artifacts"), default="manifest")
    parser = command(routes, "archive-journal", "Publish captured live events as an evidence archive", _journal)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unsealed", action="store_true")

    parser = command(routes, "review", "Publish a purpose-limited evidence snapshot", _review)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--batch-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--purpose", choices=("discovery", "evaluation", "training"), default="discovery")
    config_arg(parser, required=True)
    parser = command(routes, "verify-snapshot", "Verify an approved snapshot without opening it to a model", _audit)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--purpose", choices=("discovery", "evaluation", "training"), required=True)
