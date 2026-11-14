"""Plan or launch the native Markdown GRPO loop after real source/data/model checks.

No installation, checkpoint download, seed admission or parameter update occurs
in preflight. A launched run is fresh; uncertain previous updates are not replayed.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from integrations.benchmark_tasks.contracts import digest, identifier, strict_json
from integrations.benchmark_tasks.store import save
from integrations.executors import ExecutorOptions
from integrations.experience import ExperienceStore
from integrations.orchestration.executor import DownstreamExecutor
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from evaluation.firewall import EvaluationFirewall
from training.algorithms import AlgorithmRegistry
from training.markdown_data import prepare_group, read_pairs
from training.markdown_contract import native_prompt
from training.skill_contract import TrainingRejected
from training.updates import checkpoint_identity

ROOT = Path(__file__).resolve().parents[1]


def load_config(path, *, algorithms=None):
    import math
    config = strict_json(Path(path).read_text())
    required = {"schema_version", "model_path", "pairs_file", "executor_credentials_file", "executor_options",
        "algorithm", "prompt_batch_size", "prompt_length", "response_length", "learning_rate", "total_steps",
        "visible_gpus", "agent_workers", "generator_concurrency_per_worker", "executor_concurrency_per_worker",
        "queue_capacity_per_worker", "sample_timeout", "executor_timeout"}
    if (not isinstance(config, dict) or not required <= set(config)
            or set(config) - required - {"verl_overrides", "skillos_roles_file"}
            or config["schema_version"] != "workagent-markdown-training-runtime-v1"):
        raise TrainingRejected("Invalid deployment-owned training configuration")
    # Deployment tuning must not replace sampling/data/admission/update adapters.
    allowed_overrides = {
        "actor_rollout_ref.model.override_config.attn_implementation",
        "actor_rollout_ref.model.use_remove_padding",
        "actor_rollout_ref.actor.fsdp_config.use_torch_compile",
        "actor_rollout_ref.actor.use_torch_compile",
        "actor_rollout_ref.actor.fsdp_config.model_dtype",
        "actor_rollout_ref.actor.fsdp_config.param_offload",
        "actor_rollout_ref.actor.fsdp_config.optimizer_offload",
        "actor_rollout_ref.rollout.gpu_memory_utilization",
        "actor_rollout_ref.rollout.enforce_eager",
        "actor_rollout_ref.rollout.max_num_batched_tokens",
        "actor_rollout_ref.rollout.max_num_seqs",
        "actor_rollout_ref.rollout.temperature",
        "actor_rollout_ref.rollout.top_p",
        "actor_rollout_ref.rollout.top_k",
    }
    overrides = config.get("verl_overrides", {})
    if not isinstance(overrides, dict) or set(overrides) - allowed_overrides:
        raise TrainingRejected("Unsupported deployment verl overrides")
    for key in ("prompt_batch_size", "prompt_length", "response_length", "total_steps", "agent_workers",
                "generator_concurrency_per_worker", "executor_concurrency_per_worker", "queue_capacity_per_worker"):
        if type(config[key]) is not int or config[key] < 1:
            raise TrainingRejected("Invalid positive configuration field: " + key)
    for key in ("learning_rate", "sample_timeout", "executor_timeout"):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]) or config[key] <= 0:
            raise TrainingRejected("Invalid positive numeric field: " + key)
    gpu_ids = config["visible_gpus"]
    if not isinstance(gpu_ids, list) or not gpu_ids or any(type(i) is not int or i < 0 for i in gpu_ids) or len(set(gpu_ids)) != len(gpu_ids):
        raise TrainingRejected("Explicit unique GPU indices are required")
    algorithm = (algorithms or AlgorithmRegistry()).resolve(**config["algorithm"])
    if algorithm.max_policy_lag != 0:
        raise TrainingRejected("This launcher uses an on-policy install barrier")
    if config["prompt_batch_size"] * algorithm.group_size % len(gpu_ids):
        raise TrainingRejected("Sample batch must divide across the selected GPU world size")
    if config["prompt_batch_size"] * algorithm.group_size % config["agent_workers"]:
        raise TrainingRejected("Sample batch must divide across agent workers")
    ExecutorOptions(**config["executor_options"])
    if config["executor_options"].get("skill_version") is not None:
        raise TrainingRejected("Training slots select their own candidate snapshot")
    for key in ("model_path", "pairs_file", "executor_credentials_file"):
        if not isinstance(config[key], str) or not config[key]:
            raise TrainingRejected("Missing runtime path: " + key)
        config[key] = str((ROOT / config[key]).resolve())
    if config.get("skillos_roles_file"):
        from integrations.skill_generator.roles import load_roles
        config["skillos_roles_file"] = str((ROOT / config["skillos_roles_file"]).resolve())
        roles = load_roles(config["skillos_roles_file"])
        if Path(config["model_path"]) != (ROOT / roles["roles"]["curator"]["model_path"]).resolve():
            raise TrainingRejected("GRPO model must be the configured SkillOS Curator checkpoint")
        config["skillos_roles_sha256"] = digest(roles)
        if config["response_length"] != roles["roles"]["curator"]["max_tokens_sequence"][0]:
            raise TrainingRejected("Curator completion budget differs from SkillOS configuration")
        if config["prompt_length"] != roles["roles"]["curator"]["max_prompt_tokens"]:
            raise TrainingRejected("Curator prompt budget differs from SkillOS configuration")
        for key, expected in (("temperature", 1), ("top_p", 1), ("top_k", -1)):
            if config.get("verl_overrides", {}).get("actor_rollout_ref.rollout." + key, expected) != expected:
                raise TrainingRejected("Curator sampling must retain identity decode processors")
    return config


def preflight(config):
    import hashlib
    import shutil
    checks = []
    def check(name, passed, reason):
        checks.append({"name": name, "passed": bool(passed), "reason": reason})
    for name in ("torch", "ray", "vllm", "transformers", "omegaconf", "tensordict"):
        try: available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError): available = False
        check("installed_" + name, available, "Module discovery only; imports/runtime must succeed on launch")
    gpu_indices = set()
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                gpu_indices = {int(line.strip()) for line in result.stdout.splitlines() if line.strip()}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    check("selected_gpu_devices", set(config["visible_gpus"]) <= gpu_indices,
          "Driver-visible selected devices; memory reservation and model fit are not inferred")
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text())["sources"]["verl"]
    checkout = ROOT / lock["checkout"]
    try:
        commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError): commit = None
    check("pinned_verl_commit", commit == lock["commit"], "Native adapter targets the project pin, without NeMo recipe")
    try:
        clean = subprocess.run(["git", "-C", str(checkout), "diff", "--quiet", "HEAD", "--"],
                               capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired): clean = False
    check("pinned_verl_worktree_clean", clean, "Tracked native training implementation must match the pinned commit")
    check("full_verl_checkout", all((checkout / name).is_file() for name in
        ("verl/trainer/main_ppo.py", "verl/trainer/config/ppo_trainer.yaml", "verl/checkpoint_engine/base.py")),
        "A sparse signature-only checkout cannot launch an optimizer")
    model = Path(config["model_path"])
    model_complete = model.is_dir() and any(p.is_file() for suffix in ("*.safetensors", "*.bin") for p in model.glob(suffix))
    index_path = model / "model.safetensors.index.json"
    if index_path.is_file():
        try:
            weight_files = set(json.loads(index_path.read_text())["weight_map"].values())
            model_complete = bool(weight_files) and all((model / name).is_file() for name in weight_files)
        except (ValueError, KeyError):
            model_complete = False
    if config.get("skillos_roles_file"):
        try:
            from integrations.skill_generator.roles import load_roles
            curator = load_roles(config["skillos_roles_file"])["roles"]["curator"]
            source = json.loads((model / "deployment_source.json").read_text())
            model_complete = (model_complete and source["revision"] == curator["model_revision"] and source["weights_lfs_verified"] is True
                and all((model / name).is_file() and (model / name).stat().st_size == item["bytes"] for name, item in source["files"].items()))
        except (OSError, ValueError, KeyError):
            model_complete = False
    check("trainable_checkpoint", model_complete, "Complete local weight files required; chunk directories and partial shards do not qualify")
    check("executor_credentials_file", Path(config["executor_credentials_file"]).is_file(), "Only path presence is reported")
    if config.get("skillos_roles_file"):
        from integrations.skill_generator.roles import SkillOSRoles, load_roles
        async def role_check():
            roles = SkillOSRoles(load_roles(config["skillos_roles_file"]))
            try:
                return await roles.preflight(include_curator=False)
            finally:
                await roles.close()
        readiness = asyncio.run(role_check())
        check("frozen_skillos_judges", readiness["ready"], json.dumps(readiness, ensure_ascii=False))
    try:
        guard = EvaluationFirewall.load(ROOT / "evaluation/toolathlon_verified.manifest.json", ROOT / "training/seed_registry.json")
        check("reviewed_training_seeds", bool(guard.allowed), "Existing independent task/family firewall remains mandatory")
        pairs = read_pairs(config["pairs_file"])
        edit = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"), SkillLibrary(ROOT / ".runtime/skill_library"))
        for pair in pairs:
            prepare_group(pair, editor=edit, policy_id="preflight", training=True, firewall=guard)
        check("reviewed_pairs_bound_to_bytes", len(pairs) >= config["prompt_batch_size"], "Seed prompts, snapshots and downstream tasks verified")
    except (OSError, ValueError, KeyError) as exc:
        check("reviewed_pairs_bound_to_bytes", False, type(exc).__name__ + ": " + str(exc)[:300])
    return {"schema_version": "workagent-markdown-training-preflight-v1", "ready_for_training": all(c["passed"] for c in checks),
            "checks": checks, "actual_parameter_updates": 0,
            "runtime_compatibility_verified": False, "verl_commit": commit}


def trainer_config(config, run, *, algorithms=None):
    algorithm = (algorithms or AlgorithmRegistry()).resolve(**config["algorithm"])
    result = {"defaults": ["ppo_trainer", "_self_"], "hydra": {"searchpath": ["pkg://verl.trainer.config"]},
        "data": {"train_files": config["pairs_file"], "val_files": config["pairs_file"],
            "custom_cls": {"path": str(ROOT / "training/markdown_dataset.py"), "name": "MarkdownSkillDataset"},
            "train_batch_size": config["prompt_batch_size"], "val_batch_size": config["prompt_batch_size"],
            "max_prompt_length": config["prompt_length"], "max_response_length": config["response_length"],
            "dataloader_num_workers": 0, "truncation": "error", "apply_chat_template_kwargs": {"enable_thinking": bool(config.get("skillos_roles_file"))}},
        "algorithm": {"use_kl_in_reward": False},
        "actor_rollout_ref": {"model": {"path": config["model_path"], "trust_remote_code": False},
            "actor": {"ppo_mini_batch_size": config["prompt_batch_size"], "ppo_micro_batch_size_per_gpu": 1,
                      "optim": {"lr": config["learning_rate"]}, "use_kl_loss": False},
            "rollout": {"name": "vllm", "mode": "async", "n": algorithm.group_size, "calculate_log_probs": True,
                "log_prob_micro_batch_size_per_gpu": 1,
                "prompt_length": config["prompt_length"], "response_length": config["response_length"],
                "tensor_model_parallel_size": 1, "checkpoint_manager_class": "training.markdown_verl.MarkdownCheckpointManager",
                "agent": {"num_workers": config["agent_workers"], "default_agent_loop": "markdown_skill",
                    "agent_loop_manager_class": "training.markdown_verl.MarkdownSkillAgentLoopManager",
                    "agent_loop_config_path": str(ROOT / "training/configs/markdown_agents.yaml")}}},
        "trainer": {"project_name": "workagent-skill-generator", "experiment_name": run.name,
            "n_gpus_per_node": len(config["visible_gpus"]), "nnodes": 1, "total_training_steps": config["total_steps"],
            "total_epochs": config["total_steps"], "val_before_train": False, "test_freq": -1,
            "critic_warmup": 0, "save_freq": 1, "resume_mode": "disable", "logger": ["console"],
            "default_local_dir": str(run / "checkpoints")}}
    for dotted, value in {**config.get("verl_overrides", {}), **algorithm.verl_overrides()}.items():
        parts, target = dotted.split("."), result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute:
        raise TrainingRejected("The two-position trainer is superseded by pinned SkillOS source204. "
                               "Use scripts/run_skillos_upstream.py for collection planning. "
                               "The source204 optimizer lifecycle migration has not been accepted; no optimizer launched.")
    identifier(args.run_id)
    config = load_config(args.config)
    readiness = preflight(config)
    print(json.dumps(readiness, ensure_ascii=False), flush=True)
    if not args.execute:
        return 0
    if not readiness["ready_for_training"]:
        print("Training resources/admission are incomplete; no optimizer was launched", file=sys.stderr)
        return 2
    import hashlib
    import yaml
    from transformers import AutoTokenizer
    from scripts.run_skill_pipeline import load_credentials
    load_credentials(Path(config["executor_credentials_file"]))
    pairs = read_pairs(config["pairs_file"])
    guard = EvaluationFirewall.load(ROOT / "evaluation/toolathlon_verified.manifest.json", ROOT / "training/seed_registry.json")
    edit = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"), SkillLibrary(ROOT / ".runtime/skill_library"))
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], trust_remote_code=False)
    for pair in pairs:
        group = prepare_group(pair, editor=edit, policy_id="prelaunch", training=True, firewall=guard)
        ids = tokenizer.apply_chat_template(native_prompt(group["request"]), tokenize=True, add_generation_prompt=True,
                                            enable_thinking=bool(config.get("skillos_roles_file")))
        if len(ids) > config["prompt_length"]:
            raise TrainingRejected("Reviewed prompt exceeds actual tokenizer budget")
    tasks = {p["downstream_task"]["uid"]: p["downstream_task"] for p in pairs}
    fixed = asyncio.run(DownstreamExecutor.create(project_root=ROOT,
        options=ExecutorOptions(**config["executor_options"]), tasks=list(tasks.values())))
    model_revision, model_files = checkpoint_identity(config["model_path"])
    run = ROOT / ".runtime/training" / args.run_id
    run.mkdir(parents=True, exist_ok=False)
    key_path = ROOT / ".runtime/private" / ("training-capture-" + args.run_id + ".key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(os.urandom(32))
    save(run / "runtime.json", config)
    save(run / "initial_checkpoint.json", {"sha256": model_revision, "files": model_files})
    import importlib.metadata
    source_files = []
    for name in ("training", "integrations/orchestration", "integrations/skill_generator", "integrations/executors"):
        source_files.extend((ROOT / name).rglob("*.py"))
    checkout = ROOT / json.loads((ROOT / "configs/mainline_sources.lock.json").read_text())["sources"]["verl"]["checkout"]
    source_files += [checkout / name for name in ("verl/trainer/ppo/core_algos.py", "verl/trainer/ppo/ray_trainer.py",
        "verl/experimental/agent_loop/agent_loop.py", "verl/checkpoint_engine/base.py", "verl/workers/engine_workers.py")]
    source_hashes = {}
    for source in source_files:
        raw = source.read_bytes()
        name = str(source.relative_to(ROOT))
        source_hashes[name] = hashlib.sha256(raw).hexdigest()
        target = run / "source_snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    save(run / "source_manifest.json", {"source_sha256": source_hashes})
    env = os.environ.copy()
    bindings = dict(WORKENV_PROJECT_ROOT=str(ROOT), WORKENV_MARKDOWN_RUNTIME_CONFIG=str(run / "runtime.json"),
        WORKENV_MARKDOWN_RUNTIME_SHA256=hashlib.sha256((run / "runtime.json").read_bytes()).hexdigest(),
        WORKENV_MARKDOWN_SOURCE_MANIFEST=str(run / "source_manifest.json"),
        WORKENV_MARKDOWN_SOURCE_MANIFEST_SHA256=hashlib.sha256((run / "source_manifest.json").read_bytes()).hexdigest(),
        WORKENV_MARKDOWN_RUN_DIRECTORY=str(run), WORKENV_TRAINING_RUN_ID=args.run_id,
        WORKENV_CAPTURE_KEY_FILE=str(key_path), WORKENV_GENERATOR_MODEL_PATH=config["model_path"],
        WORKENV_GENERATOR_MODEL_REVISION=model_revision, WORKENV_GENERATOR_TOKENIZER_REVISION=model_revision,
        WORKENV_FIXED_EXECUTOR_ID=fixed.executor_id,
        WORKENV_MARKDOWN_PAIRS=config["pairs_file"],
        WORKENV_MARKDOWN_PAIRS_SHA256=hashlib.sha256(Path(config["pairs_file"]).read_bytes()).hexdigest(),
        WORKENV_VERIFIED_MANIFEST=str(ROOT / "evaluation/toolathlon_verified.manifest.json"),
        WORKENV_TRAINING_REGISTRY=str(ROOT / "training/seed_registry.json"),
        WORKENV_TRAINING_REGISTRY_SHA256=hashlib.sha256((ROOT / "training/seed_registry.json").read_bytes()).hexdigest(),
        CUDA_VISIBLE_DEVICES=",".join(map(str, config["visible_gpus"])))
    env.update(bindings)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(checkout), env.get("PYTHONPATH", "")])
    native_config = trainer_config(config, run)
    # Ray may start workers from a pre-existing daemon environment. Forward only
    # nonsecret bindings; each task worker reads its own private credential file.
    native_config["ray_kwargs"] = {"ray_init": {"address": "local", "include_dashboard": False,
        "_temp_dir": str(run / "ray"), "runtime_env": {"env_vars": {
            **{key: value for key, value in bindings.items() if key.startswith("WORKENV_")},
            "PYTHONPATH": env["PYTHONPATH"]}}}}
    with (run / "native.yaml").open("x") as stream:
        yaml.safe_dump(native_config, stream, sort_keys=False)
    command = [sys.executable, "-m", "verl.trainer.main_ppo", "--config-path", str(run), "--config-name", "native"]
    receipt = {"schema_version": "workagent-native-markdown-training-run-v1", "run_id": args.run_id,
        "status": "launching", "command": command, "preflight": readiness, "initial_model_revision": model_revision,
        "fixed_executor_id": fixed.executor_id, "actual_parameter_updates": 0, "source_sha256": source_hashes,
        "python": sys.version, "runtime_versions": {name: importlib.metadata.version(name) for name in
            ("torch", "ray", "vllm", "transformers", "omegaconf", "tensordict")}}
    save(run / "receipt.json", receipt)
    process = None
    try:
        with (run / "native.log").open("xb") as stream:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            receipt["native_exit_code"] = process.wait()
        installed = json.loads((run / "installed_policy.json").read_text()) if (run / "installed_policy.json").exists() else {}
        receipt["actual_parameter_updates"] = installed.get("step", 0)
        receipt["status"] = "completed" if process.returncode == 0 and installed.get("step") == config["total_steps"] else "failed_or_unknown"
        receipt["installed_policy"] = installed
    except BaseException:
        receipt["status"] = "interrupted_update_state_unknown"
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        raise
    finally:
        save(run / "receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
