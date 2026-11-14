"""Actual Docker reward groups plus CPU contracts and pinned native-source checks.

Diagnostics do not import stubbed torch/verl, sample an API, admit data, allocate
GPUs or report fake optimizer updates. Native runtime/GPU acceptance is separate.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import save
from training.markdown_launch import load_config, preflight, trainer_config


def native_source_checks():
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text())["sources"]["verl"]
    checkout = ROOT / lock["checkout"]
    commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if commit != lock["commit"]:
        raise ValueError("Native source checkout differs from project pin")
    expected = {
        "verl/experimental/agent_loop/agent_loop.py": {
            "AgentLoopBase": {"run": {"sampling_params"}},
            "AgentLoopManager": {"generate_sequences": {"prompts"}},
            "AsyncLLMServerManager": {"generate": {"prompt_ids", "sampling_params"}}},
        "verl/checkpoint_engine/base.py": {"CheckpointEngineManager": {"update_weights": {"global_steps"}}}}
    checks, hashes = [], {}
    for name, classes in expected.items():
        raw = (checkout / name).read_bytes()
        pinned = subprocess.check_output(["git", "-C", str(checkout), "show", "HEAD:" + name])
        if raw != pinned:
            raise ValueError("Pinned native source was edited")
        hashes[name] = hashlib.sha256(raw).hexdigest()
        tree = ast.parse(raw)
        for class_name, methods in classes.items():
            node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
            for method, arguments in methods.items():
                function = next(item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method)
                actual = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
                if not arguments <= actual:
                    raise ValueError("Native method signature changed")
                checks.append(class_name + "." + method)
    trainer = ast.parse((checkout / "verl/trainer/ppo/ray_trainer.py").read_text())
    cls = next(node for node in trainer.body if isinstance(node, ast.ClassDef) and node.name == "RayPPOTrainer")
    fit = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "fit")
    calls = [node for node in ast.walk(fit) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    actor = next(node.lineno for node in calls if node.func.attr == "_update_actor")
    checkpoint = next(node.lineno for node in calls if node.func.attr == "_save_checkpoint" and node.lineno > actor)
    install = next(node.lineno for node in calls if node.func.attr == "update_weights" and node.lineno > checkpoint)
    if not actor < checkpoint < install:
        raise ValueError("Native actor/checkpoint/install ordering changed")
    checks.append("actor_update_before_checkpoint_before_weight_install")
    return {"verl_commit": commit, "checks": checks, "native_file_sha256": hashes,
            "scope": "pinned AST/signatures/order only; no native runtime import or GPU optimization"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source_checks = native_source_checks()
    config = load_config(ROOT / "configs/markdown_training.example.json")
    readiness = preflight(config)
    save(args.output / "training_preflight.json", readiness)
    save(args.output / "native_config_projection.json", trainer_config(config, args.output / "not-launched"))
    sources = []
    for name in ("integrations/orchestration", "training/rewards"):
        sources.extend((ROOT / name).glob("*.py"))
    sources += [ROOT / name for name in ("training/algorithms.py", "training/markdown_contract.py", "training/markdown_data.py",
        "training/markdown_dataset.py", "training/markdown_verl.py", "training/markdown_launch.py", "training/updates.py",
        "integrations/executors/api.py", "scripts/prepare_skill_training.py", "scripts/verify_async_training.py", "tests/test_async_skill_training.py")]
    for source in sources:
        compile(source.read_text(), str(source), "exec")
    command = [sys.executable, "-m", "unittest", "tests.test_async_skill_training", "tests.test_generic_runtime",
        "tests.test_experience_skills", "tests.test_skill_generator_contract", "tests.test_rollout_handoff",
        "tests.test_generator_runtime", "tests.test_skill_registry_and_usage", "tests.test_task_packages_verifiers"]
    with (args.output / "tests.log").open("x") as stream:
        result = subprocess.run(command, cwd=ROOT, env={**os.environ, "RUN_CONTAINER_CHECKS": "1",
            "WORKENV_DIAGNOSTIC_EXPORT": str(args.output.resolve() / "docker-group")}, stdout=stream, stderr=subprocess.STDOUT)
    log = (args.output / "tests.log").read_text()
    if result.returncode or "skipped=" in log:
        print(log[-12000:])
        raise RuntimeError("Async training acceptance failed or container tests were skipped")
    batch = json.loads((args.output / "docker-group/batch.json").read_text())
    assert batch["training_admitted"] is False and batch["mode"] == "diagnostic"
    assert [row["reward"] for row in batch["rows"]] == [0, 1, 0, 1]
    receipt = {"schema_version": "workagent-async-skill-training-acceptance-v1", "passed": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "project_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "tests_passed": int(re.search(r"Ran (\d+) tests", log).group(1)), "container_checks_skipped": False,
        "native_source_checks": source_checks, "real_docker_group_rewards": [0, 1, 0, 1],
        "generator_sampling": "scripted_diagnostic_fixture_only", "native_runtime_import_verified": False,
        "ready_for_training": readiness["ready_for_training"],
        "real_generator_training_rollouts": 0, "actual_parameter_updates": 0,
        "training_admitted": False, "new_paid_model_requests": 0,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}}
    save(args.output / "receipt.json", receipt)
    print(json.dumps({key: value for key, value in receipt.items() if key not in ("source_sha256", "native_source_checks")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
