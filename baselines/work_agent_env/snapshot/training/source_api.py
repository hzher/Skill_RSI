"""Inspect pinned upstream API source without importing training frameworks."""
import ast
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess

from workenv.core import contained
from workenv.handoff import validate_member

ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_BYTES = 2 * 1024 * 1024
DEFAULT = object()
SOURCE_INPUTS = ("training/source_api.py", "training/verl_agent_loop.py", "training/generator_dataset.py", "configs/mainline_sources.lock.json")
UPSTREAM_INPUTS = (("verl_recipe", "nemo_gym/agent_loop.py"), ("verl_recipe", "nemo_gym/dataset.py"),
                   ("verl_recipe", "nemo_gym/REQUIRED_VERL.txt"), ("verl", "verl/experimental/agent_loop/agent_loop.py"),
                   ("verl", "verl/trainer/ppo/ray_trainer.py"))


def parse_source(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds its supported byte budget")
    return ast.parse(raw.decode("utf-8-sig"))


def definition(tree, symbol):
    scope = tree
    for part in symbol.split("."):
        matches = [node for node in scope.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == part]
        if len(matches) != 1: raise ValueError("Missing or ambiguous source definition: " + symbol)
        scope = matches[0]
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("Expected a callable definition: " + symbol)
    return scope


def callable_signature(node):
    args, parameters = node.args, []
    positional = [*args.posonlyargs, *args.args]
    required = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(args.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD
        parameters.append(inspect.Parameter(arg.arg, kind, default=inspect.Parameter.empty if index < required else DEFAULT))
    if args.vararg: parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                           default=inspect.Parameter.empty if default is None else DEFAULT))
    if args.kwarg: parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def check_call(raw, symbol, positional, keywords=(), *, asynchronous=False):
    node = definition(parse_source(raw), symbol)
    if isinstance(node, ast.AsyncFunctionDef) != asynchronous:
        raise ValueError("Callable async boundary changed: " + symbol)
    signature = callable_signature(node)
    try: signature.bind(*([None] * positional), **dict.fromkeys(keywords))
    except TypeError as exc: raise ValueError("Call no longer binds to " + symbol + ": " + str(exc)) from exc
    return {"symbol": symbol, "line": node.lineno, "asynchronous": asynchronous,
            "parameters": [{"name": p.name, "kind": p.kind.name, "required": p.default is inspect.Parameter.empty
                            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)}
                           for p in signature.parameters.values()],
            "checked_positional_arguments": positional, "checked_keyword_arguments": list(keywords)}


def check_postprocess_alias(raw):
    tree = parse_source(raw)
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)
               and node.module == "verl.experimental.agent_loop.agent_loop"
               for name in node.names if name.name == "AgentLoopWorker" and name.asname == "_AgentLoopWorker"]
    aliases = [node for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_postprocess" for t in node.targets)]
    if (len(imports) != 1 or len(aliases) != 1 or not isinstance(aliases[0].value, ast.Attribute)
        or aliases[0].value.attr != "_postprocess" or not isinstance(aliases[0].value.value, ast.Name)
        or aliases[0].value.value.id != "_AgentLoopWorker"):
        raise ValueError("Recipe postprocess alias differs from the inspected core worker method")
    return {"symbol": "_postprocess", "target": "AgentLoopWorker._postprocess", "line": aliases[0].lineno}


def git(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], timeout=30, stderr=subprocess.PIPE)


def pinned_blob(root, source, member):
    validate_member(member)
    commit = source["commit"]
    if not re.fullmatch("[0-9a-f]{40}", commit): raise ValueError("Invalid pinned source commit")
    checkout = contained(root, source["checkout"])
    if git(checkout, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("Upstream checkout no longer matches its source pin")
    entry = git(checkout, "ls-tree", "-z", commit, "--", member).rstrip(b"\0")
    header, path = entry.decode().split("\t", 1)
    mode, kind, blob = header.split()
    if path != member or mode != "100644" or kind != "blob" or not re.fullmatch("[0-9a-f]{40}", blob):
        raise ValueError("Pinned source must be one regular Git blob")
    if int(git(checkout, "cat-file", "-s", blob)) > MAX_SOURCE_BYTES:
        raise ValueError("Pinned source blob exceeds its byte budget")
    raw = git(checkout, "cat-file", "blob", blob)
    if len(raw) > MAX_SOURCE_BYTES: raise ValueError("Pinned source read exceeds its byte budget")
    return raw, {"checkout": source["checkout"], "commit": commit, "path": member,
                 "git_blob": blob, "sha256": hashlib.sha256(raw).hexdigest()}


def inspect_sources(root=ROOT):
    lock = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    inputs, raw = [], {}
    for name, member in UPSTREAM_INPUTS:
        raw[member], metadata = pinned_blob(root, lock["sources"][name], member)
        inputs.append(metadata)
    checks = []
    def check(name, operation):
        try: checks.append({"name": name, "passed": True, "detail": operation()})
        except (ValueError, SyntaxError) as exc: checks.append({"name": name, "passed": False, "error": str(exc)[:500]})
    specs = (
        ("nemo_gym/agent_loop.py", "NeMoGymAgentLoopManager._async_generate_sequences", 2, (), True),
        ("nemo_gym/agent_loop.py", "NeMoGymAgentLoopManager.create", 1,
         ("config", "worker_group", "rollout_resource_pool", "reward_loop_worker_handles", "teacher_model_manager"), True),
        ("nemo_gym/agent_loop.py", "_build_nemo_gym_examples", 2, ("validate",), False),
        ("nemo_gym/agent_loop.py", "_nemo_gym_result_to_verl", 0, ("result", "tokenizer", "prompt_length", "response_length"), False),
        ("nemo_gym/agent_loop.py", "_compute_rollout_metrics", 1, (), False),
        ("nemo_gym/dataset.py", "NeMoGymJSONLDataset.__init__", 3, ("processor", "config"), False),
        ("verl/experimental/agent_loop/agent_loop.py", "AgentLoopWorker._postprocess", 2, ("input_non_tensor_batch", "validate"), False),
    )
    for member, symbol, positional, keywords, asynchronous in specs:
        check(symbol, lambda m=member, s=symbol, p=positional, k=keywords, a=asynchronous:
              check_call(raw[m], s, p, k, asynchronous=a))
    check("recipe_postprocess_alias", lambda: check_postprocess_alias(raw["nemo_gym/agent_loop.py"]))
    def trainer_hook():
        tree = parse_source(raw["verl/trainer/ppo/ray_trainer.py"])
        if not any(isinstance(n, ast.Constant) and n.value == "agent_loop_manager_class" for n in ast.walk(tree)):
            raise ValueError("Pinned trainer does not reference the custom manager configuration key")
        return {"configuration_key_referenced": "agent_loop_manager_class", "runtime_resolution_verified": False}
    check("core_custom_manager_hook", trainer_hook)
    declaration = dict(line.split("=", 1) for line in raw["nemo_gym/REQUIRED_VERL.txt"].decode().splitlines() if "=" in line)
    declared = declaration["RECIPE_SUBMODULE_COMMIT"]
    if not re.fullmatch("[0-9a-f]{40}", declared): raise ValueError("Invalid declared recipe commit")
    check("required_core_pin", lambda: declaration_matches(declaration["VERL_COMMIT"], lock["sources"]["verl"]["commit"]))
    recipe_checkout = contained(root, lock["sources"]["verl_recipe"]["checkout"])
    declared_has_recipe = bool(git(recipe_checkout, "ls-tree", declared, "--", "nemo_gym"))
    return {"schema_version": "pinned-training-source-api-v1", "inspection_completed": True,
            "static_api_checks_passed": all(c["passed"] for c in checks), "checks": checks, "git_inputs": inputs,
            "selected_recipe_commit": lock["sources"]["verl_recipe"]["commit"], "declared_recipe_commit": declared,
            "declared_recipe_contains_nemo_gym": declared_has_recipe, "frameworks_imported": False,
            "runtime_compatibility_verified": False, "ready_for_training": False,
            "scope": "AST signatures, alias identity and hook-key presence only; no imports, tensors, optimizer or GPU execution",
            "real_model_calls": 0, "training_updates": 0,
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_INPUTS}}


def report_current(root, report):
    try:
        hashes = report["source_sha256"]
        lock = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
        inputs = report.get("git_inputs")
        if not isinstance(inputs, list) or len(inputs) != len(UPSTREAM_INPUTS): return False
        for recorded, (name, member) in zip(inputs, UPSTREAM_INPUTS):
            raw, current = pinned_blob(root, lock["sources"][name], member)
            if recorded != current or hashlib.sha256(raw).hexdigest() != recorded["sha256"]: return False
        return (report.get("schema_version") == "pinned-training-source-api-v1" and report.get("inspection_completed") is True
                and report.get("static_api_checks_passed") is True and bool(report.get("checks"))
                and all(c.get("passed") is True for c in report["checks"]) and set(hashes) == set(SOURCE_INPUTS)
                and all(hashlib.sha256((root / name).read_bytes()).hexdigest() == hashes[name] for name in SOURCE_INPUTS))
    except (KeyError, TypeError, OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired): return False


def declaration_matches(actual, expected):
    if actual != expected: raise ValueError("Recipe declaration does not match the selected core pin")
    return {"verl_commit": actual}


def main():
    report = inspect_sources()
    target = ROOT / "evidence/mainline/training_source_api.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("inspection_completed", "static_api_checks_passed", "declared_recipe_contains_nemo_gym", "runtime_compatibility_verified", "ready_for_training")}))
    return 0 if report["static_api_checks_passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
