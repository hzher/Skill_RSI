"""Benchmark configuration and locally installed extension entry points."""
from importlib.metadata import entry_points
from pathlib import Path

from .local import CommandEvaluator, JSONExactEvaluator, ManifestBenchmark, WorkBuddyBenchmark


def create_benchmark(config: dict, base: Path):
    kind = config.get("kind", "manifest")
    if kind == "clbench":
        from .clbench import CLBench
        return CLBench(config, base)
    if kind in {"pastbench", "past-bench"}:
        from .pastbench import PASTBench
        return PASTBench(config, base)
    if kind not in {"manifest", "workbuddy"}:
        entries = tuple(entry_points(group="skill_rsi.benchmarks", name=kind))
        if len(entries) != 1:
            raise ValueError(f"unknown or ambiguous benchmark: {kind}")
        return entries[0].load()(config, base)

    evaluator = None
    if definition := config.get("evaluator"):
        match definition["kind"]:
            case "json_exact":
                evaluator = JSONExactEvaluator(base / definition["expected_dir"],
                                               definition.get("output", "output/result.json"))
            case "command":
                argv = definition["argv"]
                if not isinstance(argv, list):
                    raise ValueError("verifier argv must be an array")
                argv = [str((base / arg).resolve()) if (base / arg).is_file() else arg for arg in argv]
                evaluator = CommandEvaluator(argv, timeout=definition.get("timeout", 900))
            case other:
                raise ValueError(f"unknown evaluator: {other}")

    options = {key: config[key] for key in ("image", "network", "user", "security_profile") if key in config}
    cls = ManifestBenchmark
    if kind == "workbuddy":
        cls = WorkBuddyBenchmark
        options["images"] = config.get("images")
    return cls(base / config["manifest"], evaluator=evaluator, **options)
