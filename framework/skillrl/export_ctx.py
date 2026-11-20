"""Seal native Codex CL-Bench attempts with the team's SkillRSI-CTX-v1 publisher."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
from tempfile import TemporaryDirectory

from .codex_chatgpt import digest, match_outcome


def read(path):
    return json.loads(path.read_bytes())


def select(data, keys):
    return {key: data[key] for key in keys if key in data}


def exception_class(receipt):
    name = receipt.get("type") if isinstance(receipt, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise ValueError("Invalid failure receipt exception type")
    return name


def view(case):
    """Project private collector data into two public summaries, without modifying the collection."""
    case = Path(case)
    record = read(case / "result.json")
    identity = record.get("identity")
    if not isinstance(identity, dict) or not all(k in identity for k in ("task_id", "attempt_id", "instance_id")):
        raise ValueError(f"Collector result has no verifiable task and attempt identity: {case.name}")
    objective_path = case / "native-objective.json"
    objective = read(objective_path) if objective_path.exists() else {}
    receipt_hash = digest(objective_path) if objective_path.exists() else None
    manifest_hash = record.get("artifact_manifest_sha256")
    failure_summary = {}
    if type(record.get("score_binding_version")) is int and record["score_binding_version"] == 1:
        link = read(case / "scoring-link.json")
        if record.get("score_mapping") == "native_evaluator_failed":
            failure_path = case / "scoring-failure.json"
            if (objective_path.exists() or record.get("scoring_status") != "unavailable"
                    or "native_outcome_instance_id" not in record or record["native_outcome_instance_id"] is not None
                    or "score" not in record or record["score"] is not None or not failure_path.is_file()
                    or link.get("evaluation_failure_sha256") != digest(failure_path)):
                raise ValueError("Unverified native evaluator failure")
            outcome, mapping = {}, "native_evaluator_failed"
            failure_summary = {"evaluation_failure_sha256": link["evaluation_failure_sha256"],
                               "evaluation_failure_type": exception_class(read(failure_path))}
        else:
            if not receipt_hash or (case / "scoring-failure.json").exists() or link.get("evaluation_failure_sha256") is not None:
                raise ValueError("Missing or conflicting native outcome receipt")
            outcome, mapping = match_outcome(objective, identity)
        if (read(case / "identity.json") != identity or link["identity"] != identity
                or link["mapping"] != mapping or link["query_instance_id"] != identity["instance_id"]
                or link["outcome_instance_id"] != outcome.get("instance_id")
                or record.get("score_mapping", mapping) != mapping
                or record.get("native_outcome_instance_id", outcome.get("instance_id")) != outcome.get("instance_id")
                or link["verifier_receipt_sha256"] != receipt_hash
                or link["artifact_manifest_sha256"] != manifest_hash
                or digest(case / "artifact-manifest.json") != manifest_hash):
            raise ValueError("Scoring receipt is not bound to this attempt and artifact snapshot")
    else:
        if "score_binding_version" in record:
            raise ValueError("Unsupported score binding version")
        binding_keys = ("score_mapping", "native_outcome_instance_id", "artifact_manifest_sha256")
        if (any(key in record for key in binding_keys) or (case / "scoring-link.json").exists() or (case / "scoring-failure.json").exists()
                or (case / "artifact-manifest.json").exists()):
            raise ValueError("Missing score binding version on a bound collector result")
        if ((case / "identity.json").exists() and read(case / "identity.json") != identity
                or objective and (not (case / "identity.json").exists() or record.get("objective") != objective)):
            raise ValueError("Legacy identity or original outcome does not match the collector record")
        # Historical unbound records require an exact ID and the original outcome captured by the collector.
        outcomes = objective.get("instance_outcomes", [])
        outcome = outcomes[0] if (len(outcomes) == 1 and identity.get("instance_id") is not None
                                  and outcomes[0].get("instance_id") == identity["instance_id"]) else {}
        mapping = "legacy_exact_id" if outcome else "unmatched"
    success = outcome.get("success")
    expected = int(success) if type(success) is bool else None
    if type(record.get("score")) is not type(expected) or record.get("score") != expected:
        raise ValueError("Shared score disagrees with the original verifier")
    reward = outcome.get("reward")
    public_identity = select(identity, ("task_id", "attempt_id", "instance_id", "canonical_index",
                                        "family", "schedule", "selected_index"))
    score = {**public_identity, "score": expected, "native_success": success if type(success) is bool else None,
             "native_reward": reward if type(reward) in (int, float) and math.isfinite(reward) else None,
             "native_outcome_instance_id": outcome.get("instance_id"), "score_mapping": mapping,
             "scoring_status": record.get("scoring_status"), "artifact_manifest_sha256": manifest_hash,
             "verifier_receipt_sha256": receipt_hash, "private_verifier_details_included": False, **failure_summary}
    public = select(record, ("family", "index", "complete", "execution_complete", "resource_closed",
                             "task_done", "task_actions", "initialization_terminal", "started_at", "ended_at",
                             "wall_seconds", "native_thread_ids", "artifact_snapshot_before_evaluation",
                             "usage_delta", "usage_delta_scope", "execution_mode", "resource_close_scope"))
    public["identity"] = public_identity
    public["native"] = select(record.get("native") or {}, ("usage", "complete", "exit_code", "timeout"))
    public["configuration"] = select(record["configuration"], ("model", "auth_mode", "reasoning_effort",
                                          "framework_commit", "framework_base_commit", "benchmark_commit", "protocol_commit"))
    return public, score


def artifact_name(name):
    relative = PurePosixPath(name)
    if (not relative.parts or relative.as_posix() != name or relative.is_absolute()
            or PureWindowsPath(name).drive or "\\" in name or ":" in name
            or relative.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pkl", ".parquet", ".pyc"}
            or any(p.startswith(".") or p.lower() in {"home", "credentials", "auth.json", "settings.local.json"}
                   for p in relative.parts)):
        raise ValueError(f"Invalid task artifact path: {name!r}")
    return name


def pre_identity_failure(source, case, record):
    """Only the Fresh collector's sealed initialization failure may have no native identity."""
    case_name = case.relative_to(source.root).as_posix()
    required = {"intent.json", "result.json", "failure.json", "close-receipt.json"}
    if {item.name for item in case.iterdir()} != required:
        raise ValueError(f"Unverified pre-identity failure or unexpected execution evidence: {case_name}")
    intent = source.json(case / "intent.json")
    failure = source.json(case / "failure.json")
    close = source.json(case / "close-receipt.json")
    expected = {"backend": "codex", "auth_mode": "chatgpt", "protocol": "native-mcp-v1",
                "complete": False, "score": None, "scoring_status": "unavailable"}
    execution_fields = {"identity", "objective", "native", "artifact_manifest_sha256", "native_thread_ids"}
    unstarted = (select(record, expected) == expected and record["complete"] is False
                 and record.get("execution_mode", "independent") == "independent"
                 and record.get("execution_complete", False) is False and record.get("task_actions", 0) == 0
                 and not execution_fields.intersection(record))
    intent_matches = (isinstance(intent, dict) and {"family", "index", "protocol", "backend", "auth_mode"} <= intent.keys()
                      and all(record.get(key) == val for key, val in intent.items()))
    failure_matches = isinstance(failure, dict) and record.get("failure") == failure
    closed = (isinstance(close, dict) and type(close.get("complete")) is bool
              and close["complete"] is record.get("resource_closed")
              and any(step.get("name") == "benchmark_thread" for step in close.get("steps", [])))
    if not (unstarted and intent_matches and failure_matches and closed):
        raise ValueError(f"Unverified pre-identity initialization failure: {case_name}")
    hashes = {name: source.ref(case / name)["sha256"] for name in sorted(required)}
    source.assert_unchanged()
    return {"case": case_name, "reason": "initialization_failed_before_instance_identity",
            "exception_type": exception_class(failure), "resource_closed": close["complete"], "receipt_sha256": hashes}


def prepare(source, case, staged, artifacts):
    """Only selected sources enter the staging archive that the protocol publisher can seal."""
    from evidence.archive import write

    for name in ("result.json", "identity.json", "native-objective.json", "scoring-link.json", "scoring-failure.json", "artifact-manifest.json"):
        path = source.path(case / name)
        if path.exists():
            source.ref(path)
    public, score = view(case)
    attempt = public["identity"]["attempt_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", attempt):
        raise ValueError(f"Invalid attempt id: {attempt!r}")
    dest = staged / "cases" / attempt
    manifest = source.json(case / "artifact-manifest.json") if (case / "artifact-manifest.json").exists() else {}
    def copy(name, expected):
        src = source.path(case / name)
        actual = source.ref(src)
        if expected is not None and (actual["sha256"], actual["bytes"]) != (expected["sha256"], expected["bytes"]):
            raise ValueError(f"Captured artifact differs from manifest: {name}")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        if digest(target) != actual["sha256"]:
            raise ValueError(f"Source changed during copy: {name}")

    for name in ("initial-task.json", "actor/prompt.txt", "actor/native-cli.jsonl", "actor/final.txt",
                 "broker.jsonl", "final-task.json"):
        if name in manifest or source.path(case / name).is_file():
            if score["artifact_manifest_sha256"] and name not in manifest:
                raise ValueError(f"Unbound collector source: {name}")
            copy(name, manifest.get(name))
    versions = []
    capture_file = source.path(case / "workspace-capture.json")
    if artifacts and capture_file.exists():
        capture = source.json(capture_file)
        if not manifest.get("workspace-capture.json") or digest(capture_file) != manifest["workspace-capture.json"]["sha256"]:
            raise ValueError("Workspace inventory is not bound to the captured artifact manifest")
        for name in sorted(artifacts):
            artifact = "workspace-final/" + name
            saved = capture.get("files", {}).get(name)
            if saved is not None:
                copy(artifact, saved)
                versions.append(name)
            elif source.path(case / artifact).exists():
                raise ValueError(f"Task output missing from workspace inventory: {name}")
    continuity_path = source.path(case / "continuity.json")
    mode = public.get("execution_mode", "stateful" if continuity_path.exists() else "independent")
    if mode not in {"stateful", "independent", "fresh"}:
        raise ValueError(f"Unknown execution mode: {mode}")
    if continuity_path.exists() != (mode == "stateful"):
        raise ValueError("Continuity evidence and execution mode disagree")
    if mode == "stateful":
        info = source.json(continuity_path)
        if (score["artifact_manifest_sha256"] and "continuity.json" not in manifest
                or "continuity.json" in manifest and digest(continuity_path) != manifest["continuity.json"]["sha256"]):
            raise ValueError("Continuity record differs from captured manifest")
        ident = public["identity"]
        if any(info[k] != ident[k] for k in ("attempt_id", "family", "canonical_index")):
            raise ValueError("Continuity record identifies a different attempt")
        threads = public.get("native_thread_ids") or []
        if threads and threads != [info["thread_id"]]:
            raise ValueError("Continuity does not match native thread")
        continuity = select(info, ("attempt_id", "family", "canonical_index", "thread_id", "resumed_thread_id",
                                   "previous_attempt_id", "session_started_here", "initialization_terminal"))
        continuity["source_sha256"] = digest(continuity_path)
        family = ident["family"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", family):
            raise ValueError("Invalid task family")
        family_root = case.parents[1] / "families" / family
        for name in ("final-close-receipt.json", "close-receipt.json"):
            path = source.path(family_root / name)
            if path.exists():
                closure = source.json(path)
                continuity["family_close"] = {**select(closure, ("closed", "complete", "scope")),
                                              "source_sha256": digest(path)}
                break
        public["continuity"] = continuity
    public["execution_mode"] = mode
    source.assert_unchanged()
    write(dest / "views/run.json", public)
    write(dest / "views/score.json", score)
    return dest, public, score, versions


def read_run(archive, case, public, score, versions):
    from evidence.schema import NativeRun, event, identifier

    ident, config = public["identity"], public["configuration"]
    threads = public.get("native_thread_ids") or []
    thread = threads[0] if len(threads) == 1 else None
    scope = {"instance_id": ident["instance_id"], "instance_index": ident["canonical_index"],
             "native_session_id": thread, "thread_id": thread, "runtime_id": ident["attempt_id"]}
    ref = archive.ref
    metadata = ref(case / "views/run.json", layer="collector_run")
    grade = ref(case / "views/score.json", layer="native_score_summary")
    stateful = public["execution_mode"] == "stateful"
    continuity = public.get("continuity") or {}
    health = {"collector": {"recording_closed": True}, "resources": {"closed": public.get("resource_closed")}}
    if stateful:
        health["resources"].update(scope="instance", family_close=continuity.get("family_close"))
    run = NativeRun(identity={
        "run_id": ident["attempt_id"], "task_name": ident["task_id"], "job_id": ident["task_id"],
        "job_attempt_id": ident["attempt_id"], "task_family": ident.get("family", public["family"]),
        "phase": "stateful" if stateful else "independent", "status": "completed" if public["complete"] else "incomplete",
        "previous_attempt_id": continuity.get("previous_attempt_id"), "created_at": public.get("started_at"),
        "ended_at": public.get("ended_at"), "model_id": config["model"], "auth_mode": config.get("auth_mode"),
        "clbench_commit": config.get("benchmark_commit"), "framework_base_commit": config.get("framework_base_commit"),
        "framework_commit": config.get("framework_commit"), "protocol_commit": config.get("protocol_commit"),
        "reader_name": "skillrl.export_ctx", "reader_sha256": digest(Path(__file__)),
        "reset_policy": "official_family_sequence" if stateful else "official_reset_baseline_instance",
        "memory_scope": "native_codex_thread_within_family" if stateful else "independent_native_session",
        "collector_ref": metadata, "runtime_health": health,
        "capture_capabilities": {"source": "native_codex_cli_and_benchmark_broker",
                                 "actual_context": "unavailable", "physical_attempts": "unavailable",
                                 "authoritative_tool_order": "broker.sequence", "cross_stream_event_order": "not_asserted"}})
    run.sources.extend([metadata, grade])
    query = case / "initial-task.json"
    if query.exists():
        query_ref = ref(query, layer="benchmark_query")
        run.identity["initial_state_refs"] = [query_ref]
        run.events.append(event(query_ref, "environment_query", 0, actor_type="harness", **scope))
    cli = case / "actor/native-cli.jsonl"
    native_ids, tool_refs = [], []
    if cli.exists():
        for row, raw, line in archive.jsonl(cli):
            item = row.get("item") or {}
            kind = "model_message" if item.get("type") == "agent_message" else "native_event"
            entry = event(raw, kind, line, actor_type="agent" if kind == "model_message" else "harness",
                          **scope, native_type=row.get("type"), native_item_type=item.get("type"),
                          native_item_id=item.get("id"), order_scope="native_stream", native_seq=line,
                          content=item.get("text") if kind == "model_message" else None)
            run.events.append(entry)
            native_ids.append(entry["event_id"])
    journal = case / "broker.jsonl"
    pending = {}
    last_sequence = 0
    if journal.exists():
        for row, raw, line in archive.jsonl(journal):
            intent = row["type"] == "tool_intent"
            if row["type"] not in ("tool_intent", "tool_result", "tool_error"):
                raise ValueError("Unknown broker record")
            key = row["sequence"]
            fields = {"call_id": row["operation_id"], "operation_id": row["operation_id"], "tool_name": row["tool"],
                      "arguments": row["arguments"], "dispatch_sequence": key, "timestamp": row["timestamp"],
                      "order_scope": "serialized_benchmark_broker", "representation": "rpc",
                      "cached_receipt": row["cached_receipt"], "mcp_server": "benchmark"}
            result = row.get("result")
            if result is not None:
                fields.update(result=result, rendered_result_ref={**raw, "locator": {"type": "line", "line": line, "line_pointer": "/result"}})
            fields["error"] = row.get("error") or bool((result or {}).get("isError"))
            entry = event(raw, "tool_call" if intent else "tool_return", line, **scope, **fields,
                          actor_type="agent" if intent else "harness")
            run.events.append(entry)
            tool_refs.append(raw)
            if intent:
                if type(key) is not int or key <= last_sequence:
                    raise ValueError("Duplicate or out-of-order broker dispatch")
                last_sequence = key
                pending[key] = entry
                continue
            before = pending.pop(key, None)
            if before is None or any(before[k] != entry[k] for k in ("call_id", "tool_name", "arguments", "cached_receipt")):
                raise ValueError("Broker return does not match intent")
            if row["tool"] == "act" and result and not fields["error"] and not row["cached_receipt"]:
                value = json.loads(result["content"][0]["text"])
                if "action" in value:
                    run.events.append(event(raw, "environment_action", line, **scope, actor_type="agent",
                        action=value["action"], observation=value.get("observation"), dispatch_sequence=key,
                        submission_status="benchmark_recorded", mapping_status="exact", call_id=row["operation_id"],
                        source_event_ids=[before["event_id"], entry["event_id"]], timestamp=row["timestamp"]))
        if pending:
            health["collector"].update(recording_closed=False, open_calls=[e["call_id"] for e in pending.values()])
        run.sources.append(ref(journal, layer="benchmark_tool_journal"))
    if cli.exists():
        prompt = case / "actor/prompt.txt"
        usage_key = "usage_delta" if stateful else "native/usage"
        usage = public.get("usage_delta") if stateful else public["native"].get("usage")
        def usage_ref(pointer):
            return ref(case / "views/run.json", {"type": "json_pointer", "pointer": "/" + pointer}, "native_usage")
        request = {**scope, "logical_call_id": ident["attempt_id"] + "-cli", "attempt_id": None,
                   "turn_id": 1, "native_step_id": 0, "purpose": "main", "call_granularity": "cli_invocation",
                   "mapping_status": "exact", "request_evidence_kind": "native_invocation_prompt",
                   "request_ref": ref(prompt, layer="native_invocation_prompt") if prompt.exists() else None,
                   "response_ref": ref(cli, layer="native_cli_session_output"), "actual_context_ref": None,
                   "source_event_ids": native_ids, "tool_refs": tool_refs,
                   "usage_refs": [usage_ref(usage_key)] if isinstance(usage, dict) else [],
                   "usage_scope": public.get("usage_delta_scope") if stateful else "independent_cli_invocation",
                   "missing_reasons": ["effective_model_input_unavailable", "physical_provider_requests_unobserved"]}
        if stateful and isinstance(public["native"].get("usage"), dict):
            request["thread_cumulative_usage_refs"] = [usage_ref("native/usage")]
        run.requests.append(request)
    for name in ("actor/final.txt", "final-task.json"):
        if (case / name).exists():
            run.sources.append(ref(case / name, layer="native_artifact"))
    for name in versions:
        content = ref(case / "workspace-final" / name, layer="task_output")
        artifact_id = identifier(ident["attempt_id"], name)
        run.versions.append({"artifact_id": artifact_id, "artifact_role": "task_output", "path": name,
                             "version_id": identifier(artifact_id, content["sha256"]), "parent_version_id": None,
                             "content_ref": content, "content_hash": content["sha256"], "source_event_ids": [],
                             "observation_boundary": "collector_snapshot", "existence_status": "present"})
    run.scores.append({**scope, "task_id": ident["task_id"], "job_attempt_id": ident["attempt_id"],
                       "metric_name": "reward", "raw_score": score["native_reward"], "native_success": score["native_success"],
                       "shared_score": score["score"], "grading_status": "valid" if score["score"] is not None else "unscorable",
                       "scorer_version": config.get("benchmark_commit"), "details_ref": grade,
                       "artifact_manifest_sha256": score["artifact_manifest_sha256"],
                       "private_verifier_receipt_sha256": score["verifier_receipt_sha256"],
                       **select(score, ("evaluation_failure_sha256", "evaluation_failure_type"))})
    run.diagnostics.extend(archive.diagnostics)
    return run


def export(root, collections, output, protocol_root, *, artifacts=()):
    """Publish one immutable batch. Repeating an identical export verifies the existing directory."""
    root, output = Path(root).resolve(strict=True), Path(output).resolve()
    protocol_root = Path(protocol_root).resolve(strict=True)
    if not (protocol_root / "evidence/build.py").is_file():
        raise ValueError("Expected a SkillRSI-CTX checkout containing evidence/build.py")
    sys.path.insert(0, str(protocol_root))
    try:
        from evidence import schema
        from evidence.archive import Archive, write
        from evidence.build import _publish
        from evidence.verify import verify
    finally:
        sys.path.remove(str(protocol_root))
    if not Path(schema.__file__).resolve().is_relative_to(protocol_root) or schema.SCHEMA != "SkillRSI-CTX-v1":
        raise ValueError("Loaded protocol differs from requested SkillRSI-CTX-v1 checkout")
    protocol_sources = {f"evidence/{name}.py": digest(protocol_root / "evidence" / f"{name}.py")
                        for name in ("archive", "build", "schema", "trajectory", "verify")}
    artifacts = {artifact_name(name) for name in artifacts}
    cases = []
    for collection in collections:
        directory = Archive(root).path(Path(collection).absolute()).joinpath("cases")
        if not directory.is_dir():
            raise ValueError(f"Missing collection cases directory: {directory}")
        if output.is_relative_to(directory) or directory.is_relative_to(output):
            raise ValueError("Export would overlap the source collection")
        results = sorted(directory.glob("*/result.json"))
        if not results:
            raise ValueError(f"Collection has no sealed results: {directory.parent}")
        cases.extend(result.parent for result in results)
    if not cases:
        raise ValueError("Expected at least one collection")
    if len(set(cases)) != len(cases):
        raise ValueError("Duplicate attempt source directory")
    if output.exists() and (not output.is_dir() or (any(output.iterdir()) and not (output / "batch_index.json").is_file())):
        raise ValueError("Destination is nonempty and is not a CTX archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".codex-ctx-", dir=output.parent) as tmp:
        batch = Path(tmp) / "batch"
        (batch / "runs").mkdir(parents=True)
        runs, attempts, failures = [], set(), []
        for case in sorted(cases):
            source = Archive(root)
            record = source.json(case / "result.json")
            if "identity" not in record:
                failures.append(pre_identity_failure(source, case, record))
                continue
            with TemporaryDirectory(dir=tmp) as staging:
                staged = Archive(staging)
                prepared = prepare(source, case, Path(staging), artifacts)
                run = read_run(staged, *prepared)
                run.identity["publisher_source_sha256"] = protocol_sources
                attempt = run.identity["job_attempt_id"]
                if attempt in attempts:
                    raise ValueError(f"Duplicate attempt: {attempt}")
                attempts.add(attempt)
                runs.append(_publish(staged, batch, "codex", run, True))
                staged.assert_unchanged()
        coverage = {"sealed_case_count": len(cases), "trajectory_count": len(runs),
                    "pre_identity_failure_count": len(failures), "pre_identity_failures": failures}
        index = {"schema_version": schema.SCHEMA, "generator_version": schema.GENERATOR, "batch_id": root.name,
                 "batch_local": False, "harness_imports": {"codex": {"runs": runs, "sources_sealed": True,
                       "coverage": coverage, "counts_by_status": dict(Counter(r["status"] for r in runs))}}, "runs": runs}
        write(batch / "batch_index.json", index)
        result = verify(batch)
        try:
            batch.rename(output)
        except OSError:
            # A previous or concurrently completed export can be reused only if every indexed run matches.
            if not (output / "batch_index.json").is_file() or read(output / "batch_index.json") != index:
                raise ValueError("Destination already contains different evidence; choose a new output") from None
            result = verify(output)
        return {**result, "archive": str(output), "pre_identity_failure_count": len(failures)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--collection", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol-root", type=Path, required=True)
    parser.add_argument("--artifact", action="append", default=[], metavar="RELATIVE_PATH",
                        help="Include this workspace-final output when present in a captured inventory")
    args = parser.parse_args()
    print(json.dumps(export(args.root, args.collection, args.output, args.protocol_root, artifacts=args.artifact)))


if __name__ == "__main__":
    main()
