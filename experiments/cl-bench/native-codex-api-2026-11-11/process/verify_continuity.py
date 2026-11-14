"""Verify actual Codex session continuity and CLI-version evidence, without inference."""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def verify(run_id):
    run = ROOT / "runs/collections" / run_id
    index = json.loads((run / "audit/collection-index.json").read_text(encoding="utf-8-sig"))
    groups = {"baseline": [], "run": []}
    for entry in index["episodes"]:
        data = json.loads((run / entry["path"]).read_text(encoding="utf-8-sig"))
        if not data["interactions"]:
            raise AssertionError("Missing Codebase interaction evidence")
        phase = "baseline" if entry["phase"] == "baseline" else "run"
        metadata = data["interactions"][0]["response"]["metadata"]
        sessions = {turn["response"]["metadata"]["conversation_id"] for turn in data["interactions"]}
        assert len(sessions) == 1, "Conversation unexpectedly changed inside an issue"
        groups[phase].append({"instance_id": entry["instance_id"], "instance_index": entry["instance_index"],
                              "session_id": metadata["conversation_id"], "continuity": metadata["continuity"],
                              "native_memory_file_count": len(metadata.get("memory_files", {}))})
    for rows in groups.values():
        rows.sort(key=lambda row: row["instance_index"])
        assert all(row["native_memory_file_count"] == 0 for row in rows)
    fresh = groups["baseline"]
    stateful = groups["run"]
    assert all(row["continuity"].get("conversation_id_before") is None and row["continuity"].get("resumed_this_turn") is False for row in fresh)
    assert len({row["session_id"] for row in fresh}) == len(fresh)
    if stateful:
        session = stateful[0]["session_id"]
        assert stateful[0]["continuity"].get("conversation_id_before") is None
        assert all(row["session_id"] == session for row in stateful)
        assert all(row["continuity"].get("conversation_id_before") == session and row["continuity"].get("resumed_this_turn") is True for row in stateful[1:])
        assert session not in {row["session_id"] for row in fresh}
    versions = set()
    requested_models = set()
    for request in (run / "control/native-calls").glob("*.request.json"):
        command = json.loads(request.read_text(encoding="utf-8"))["native_command"]
        if command == "codex --version":
            output = request.with_name(request.name.replace(".request.json", ".events.jsonl"))
            if output.exists():
                match = re.search(r"codex-cli\s+([0-9.]+)", output.read_text(encoding="utf-8"))
                if match:
                    versions.add(match.group(1))
        match = re.search(r"--model\s+(\S+)", command)
        if match:
            requested_models.add(match.group(1))
    assert versions == {"0.167.0"} and requested_models == {"gpt-6.1-sol"}
    report = {"run_id": run_id, "fresh_instances_checked": len(fresh), "stateful_instances_checked": len(stateful),
              "fresh_uses_distinct_sessions": True, "fresh_has_no_inherited_conversation": True,
              "stateful_uses_one_session_across_issues": bool(stateful), "fresh_and_stateful_sessions_disjoint": bool(stateful),
              "native_memory_files_empty": True, "actual_cli_versions_from_version_output": sorted(versions),
              "models_from_native_command_arguments": sorted(requested_models), "actual_upstream_model_independently_verified": False,
              "full_pair_verification_complete": len(fresh) == len(stateful) == 19,
              "model_calls_by_check": 0, "fresh_evidence": fresh, "stateful_evidence": stateful}
    (ROOT / "reports/continuity-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    report = verify(parser.parse_args().run)
    print(json.dumps({key:value for key,value in report.items() if not key.endswith("_evidence")}))
