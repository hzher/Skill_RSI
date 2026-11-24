"""Discovery, benchmarks, comparisons and the optional training handoff."""
from pathlib import Path

from . import command, config_arg


def _extract(args):
    from ..arguments.config import load_config
    from ..discovery.extract import extract
    return extract(args.snapshot, args.output, args.id, load_config(args.teacher))


def _materialize(args):
    from ..arguments.config import load_config
    from ..discovery.materialize import materialize
    return materialize(args.extraction, args.component, args.version, load_config(args.config), args.output)


def _benchmark(config):
    from ..arguments.config import load_config
    from ..benchmarks.registry import create_benchmark
    return create_benchmark(load_config(config), config.resolve().parent)


def _tasks(args):
    from ..experiments.run import benchmark_fingerprint
    benchmark = _benchmark(args.config)
    return {"fingerprint": benchmark_fingerprint(benchmark), "configuration": benchmark.configuration(),
            "tasks": [item.public() if hasattr(item, "public") else item for item in benchmark.tasks()]}


def _workbuddy(args):
    from ..benchmarks.workbuddy import export
    from ..evidence.io import read_json
    return export(args.dataset, read_json(args.selection), args.output, strip_prefix=args.strip_prefix)


def _freeze(args):
    from ..arguments.config import load_config
    from ..experiments.protocol import freeze
    from ..experiments.run import benchmark_fingerprint
    return freeze(args.output, load_config(args.config), benchmark_fingerprint(_benchmark(args.benchmark)),
                  args.snapshot, discovery=args.discovery, state_dir=args.state_dir)


def _run(args):
    from ..experiments.run import execute
    return execute(args.protocol, _benchmark(args.benchmark), args.output, state_dir=args.state_dir,
                   capability_project=args.capability_project)


def _report(args):
    from ..experiments.report import report
    return report(args.protocol, args.execution, args.output)


def _export(args):
    from ..exchange.training import export_training
    return export_training(args.snapshot, args.output, dataset_id=args.dataset_id, score_protocol=args.score_protocol)


def _verify_export(args):
    from ..exchange.training import verify_export, verify_receipt
    return verify_receipt(args.bundle, args.receipt) if args.receipt else verify_export(args.bundle)


def _output(parser):
    parser.add_argument("--output", required=True, type=Path)


def _report_arguments(parser):
    parser.add_argument("protocol", type=Path)
    parser.add_argument("execution", type=Path)
    _output(parser)


def register(routes):
    parser = command(routes, "extract", "Read approved development evidence and propose candidate strategies", _extract, aliases=("discover",))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--id", required=True)
    config_arg(parser, "--teacher", required=True)
    _output(parser)
    parser = command(routes, "materialize", "Package a reviewed discovery candidate", _materialize)
    parser.add_argument("extraction", type=Path)
    parser.add_argument("component")
    parser.add_argument("version")
    config_arg(parser, required=True)
    _output(parser)

    group = command(routes, "benchmark", "Inspect an evaluation adapter")
    sub = group.add_subparsers(required=True)
    config_arg(command(sub, "tasks", "List public task IDs and fingerprints", _tasks), required=True)
    parser = command(sub, "export-workbuddy", "Export selected public WorkBuddy task workspaces", _workbuddy)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("selection", type=Path)
    parser.add_argument("--strip-prefix", default="")
    _output(parser)

    group = command(routes, "experiment", "Freeze, execute and compare independent held-out conditions")
    sub = group.add_subparsers(required=True)
    parser = command(sub, "freeze", "Freeze an evaluation protocol", _freeze)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--discovery", type=Path)
    config_arg(parser, required=True)
    config_arg(parser, "--benchmark", required=True)
    _output(parser)
    parser = command(sub, "run", "Run the frozen host and benchmark assignments", _run)
    parser.add_argument("protocol", type=Path)
    parser.add_argument("--capability-project", type=Path, default=Path("."))
    config_arg(parser, "--benchmark", required=True)
    _output(parser)
    _report_arguments(command(sub, "report", "Compare complete native score sequences", _report))
    _report_arguments(command(routes, "validate", "Compare complete native score sequences", _report, aliases=("evaluate",)))
    parser = command(routes, "export", "Generate a purpose-limited training handoff", _export)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--score-protocol", required=True)
    _output(parser)
    parser = command(routes, "verify-export", "Verify the training handoff or receiver receipt", _verify_export)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--receipt", type=Path)
