"""Index native collection evidence, preserving failed and interrupted attempts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def long_path(path):
    path = path.absolute()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


def audit(run):
    plan = read(run / "control/bridge-plan.json")
    manifest = read(run / "manifest.json")
    task = plan["effective_config"]["task"]["name"]
    # Prefer final traces. Live snapshots can say completed with zero outcomes;
    # actual completed instance outcomes determine coverage.
    base = long_path(run / "native/results" / task)
    runtime_base = None
    if manifest["execution_status"] == "running":
        from collection_progress import runtime_path
        runtime_base = runtime_path(plan["linux_runtime"])
        base = runtime_base / "results" / task
    candidates = list((base / "traces").glob("*/*.json"))
    if not candidates:
        candidates = list((base / "live").glob("*/*.json"))
    selected = {}
    for path in candidates:
        if path.name == "manifest.json":
            continue
        try:
            trace = read(path)
        except (OSError, ValueError):
            continue
        if "execution" not in trace:
            continue
        phase = trace.get("phase", path.stem)
        if phase not in selected or path.stat().st_mtime > selected[phase][0].stat().st_mtime:
            selected[phase] = (path, trace)
    episodes = []
    sessions = set()
    usage_by_phase = {}
    replayed_outcomes = []
    episode_kinds = {}
    submissions = []
    if (run / "control/submission-map.jsonl").exists():
        submissions = [json.loads(line) for line in (run / "control/submission-map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for phase, (path, trace) in selected.items():
        usage_by_phase[phase] = trace.get("execution", {}).get("usage")
        outcomes = trace.get("instance_outcomes") or trace["execution"].get("instance_outcomes") or []
        interactions = trace.get("interactions") or []
        for turn in interactions:
            metadata = turn.get("response", {}).get("metadata") or {}
            sessions.update(metadata.get("codex_conversation_session_ids") or [])
            if metadata.get("conversation_id"):
                sessions.add(metadata["conversation_id"])
        for n, outcome in enumerate(outcomes):
            instance_id = outcome["instance_id"]
            index = outcome.get("instance_index", n)
            # Native Sales query IDs include the schedule prefix while its
            # outcome IDs omit it. The canonical instance_index joins them;
            # keep both original IDs instead of rewriting native evidence.
            turns = [item for item in interactions if item.get("query", {}).get("instance_index") == index]
            if not turns:
                # Some baseline collectors remap indexes in the aggregate only.
                for child in trace.get("instance_traces", []):
                    child_outcomes = child.get("instance_outcomes") or child.get("execution", {}).get("instance_outcomes") or []
                    if any(x.get("instance_id") == instance_id and x.get("instance_index", index) == index for x in child_outcomes):
                        turns = child.get("interactions") or []
                        break
            if turns and all((t.get("response", {}).get("metadata") or {}).get("rsi_replayed") for t in turns):
                replayed_outcomes.append({"phase": phase, "instance_id": instance_id, "instance_index": index,
                                          "source_run": plan.get("resume_from_run"), "counted_as_new_trajectory": False})
                continue
            evidence_kind = "model_interaction" if turns else "missing_interaction_evidence"
            environment_evidence = None
            if not turns and task == "exploitable_poker":
                history = (trace.get("result") or {}).get("metrics", {}).get("hand_history", [])
                hand = next((h for h in history if h["instance_id"] == instance_id), None)
                if (hand and hand["actions"] and all(a["player"] == "opponent" for a in hand["actions"])
                        and hand["actions"][-1]["action"] == "FOLD"):
                    evidence_kind = "environment_auto_resolution"
                    marker = f"Hand {hand['hand_num']} complete:"
                    environment_evidence = {"reason": "opponent_folded_before_model_turn", "native_hand_history": hand,
                                            "supporting_observation_steps": [t["step_number"] for t in interactions if marker in t.get("observation", {}).get("content", "")]}
            episode_kinds[evidence_kind] = episode_kinds.get(evidence_kind, 0) + 1
            key = f"{phase}_{index:04d}_{hashlib.sha256(instance_id.encode()).hexdigest()[:10]}"
            episode = {"run_id": run.name, "task": task, "phase": phase,
                       "instance_id": instance_id, "instance_index": index,
                       "grading_status": "native_outcome_present", "native_outcome": outcome,
                       "completed_interactions": len(turns), "interactions": turns,
                       "evidence_kind": evidence_kind, "environment_evidence": environment_evidence,
                       "native_query_instance_ids": sorted({item["query"].get("instance_id") for item in turns if item["query"].get("instance_id") is not None}),
                       "interaction_join_key": "native canonical instance_index",
                       "visible_initial_prompt_sha256": hashlib.sha256(turns[0]["query"]["prompt"].encode()).hexdigest() if turns else None,
                       "source_trace": ("native/" + path.relative_to(runtime_base).as_posix()) if runtime_base else path.relative_to(long_path(run)).as_posix(),
                       "source_trace_is_live": runtime_base is not None,
                       "source_sha": plan["benchmark_sha"], "resource_limits": plan.get("resource_limits"),
                       "resume_from_run": plan.get("resume_from_run"),
                       "replayed_prefix_interactions": sum(bool((t.get("response", {}).get("metadata") or {}).get("rsi_replayed")) for t in turns),
                       "cost_note": "Native cost_usd is a framework estimate, not actual gateway billing"}
            if task == "codebase_adaptation":
                # Join full captures to the original grader's patch prefix,
                # without guessing a phase from capture order.
                metric_traces = [trace] + trace.get("instance_traces", [])
                submitted = [item for native in metric_traces
                             for item in ((native.get("result") or {}).get("metrics") or {}).get("interaction_trace", [])
                             if item.get("issue_id") == instance_id and "model_patch" in item]
                prefixes = {item["model_patch"] for item in submitted}
                matches = [item for item in submissions if item["instance_id"] == instance_id
                           and (run / item["path"]).read_text(encoding="utf-8")[:5000] in prefixes]
                episode["full_submission_evidence"] = {
                    "status": ("matched_native_patch_prefix" if len(matches) == 1 else
                               "multiple_captures_match_native_patch_prefix" if matches else
                               "no_submit_before_native_timeout" if outcome.get("metadata", {}).get("timed_out") else
                               "pending_native_result_or_no_matching_capture"),
                    "matching_captures": matches,
                    "native_patch_prefix_sha256": sorted(hashlib.sha256(value.encode()).hexdigest() for value in prefixes),
                    "note": "Full files are preserved separately; native model_patch is capped at 5000 characters. Ambiguous identical captures remain explicitly listed."}
            target = run / "audit/episodes" / (key + ".json")
            write(target, episode)
            episodes.append({k: episode[k] for k in ["task", "phase", "instance_id", "instance_index", "grading_status", "completed_interactions", "visible_initial_prompt_sha256", "evidence_kind"]}
                            | {"path": target.relative_to(run).as_posix(), "reward": outcome.get("reward"), "success": outcome.get("success")})
    report = {"run_id": run.name, "task": task, "updated_at": datetime.now(timezone.utc).isoformat(),
              "execution_status": manifest["execution_status"], "expected_instances_per_sequence": plan["instance_count_per_sequence"],
              "expected_trajectories": plan["instance_count_per_sequence"] * (2 if plan["include_baseline"] else 1),
              "graded_trajectories": len(episodes), "unique_graded": len({e["instance_id"] for e in episodes}),
              "episodes": episodes, "captured_full_submissions": submissions,
              "evidence_kind_counts": episode_kinds,
              "native_session_ids": sorted(sessions), "native_usage_by_phase": usage_by_phase,
              "resume_from_run": plan.get("resume_from_run"), "replayed_outcomes_not_counted_as_new": replayed_outcomes,
              "cost_note": "Framework estimates are not actual gateway charges; usage of interrupted calls may only exist in native event sidecars",
              "ungraded_submission_instance_ids": sorted({s["instance_id"] for s in submissions} - {e["instance_id"] for e in episodes}),
              "note": "A native outcome is evidence of completed grading; process exit code is not a task score. Partial submissions remain ungraded.",
              "model_calls_by_auditor": 0}
    write(run / "audit/collection-index.json", report)
    if manifest["execution_status"] != "running":
        manifest["grading_status"] = "native_outcomes_indexed_with_partial_coverage" if report["graded_trajectories"] + len(replayed_outcomes) < report["expected_trajectories"] else "native_outcomes_indexed"
        manifest["native_outcome_summary"] = {k: report[k] for k in ["graded_trajectories", "unique_graded", "ungraded_submission_instance_ids"]}
        manifest["evidence"]["collection_index"] = "audit/collection-index.json"
        manifest["session_ids"] = report["native_session_ids"]
        manifest["usage"] = {"per_phase": usage_by_phase, "cost_note": report["cost_note"]}
        manifest["missing_evidence"] = (["Sequence is incomplete; ungraded and unattempted instances have no task score"]
                                        if report["graded_trajectories"] + len(replayed_outcomes) < report["expected_trajectories"] else [])
        write(run / "manifest.json", manifest)
        # Derived indexes are added after sealing; update their checksum inventory.
        from rsi import checksum_tree
        write(run / "checksums.json", checksum_tree(run))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    args = parser.parse_args()
    runs = [ROOT / "runs/collections" / args.run] if args.run else sorted((ROOT / "runs/collections").glob("*"))
    for run in runs:
        if not (run / "control/bridge-plan.json").is_file():
            continue
        result = audit(run)
        print(json.dumps({k: result[k] for k in ["run_id", "graded_trajectories", "unique_graded", "ungraded_submission_instance_ids"]}))


if __name__ == "__main__":
    main()
