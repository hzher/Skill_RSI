"""Replay real recorded Cohort actions and verify native-session restoration without inference."""
from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sources/continual-learning-bench-pr26-lf"
sys.path.insert(0, str(SOURCE))
from native_resume import NativeResume
from src.tasks.cohort_studies.task import CohortStudiesTask


def main():
    run_id = "collection_20261112T184222+0800_8624dcbc"
    source = ROOT / "runs/collections" / run_id
    plan = json.loads((source / "control/bridge-plan.json").read_text(encoding="utf-8-sig"))
    plan["resume_from_run"] = run_id
    os.chdir(SOURCE)
    with tempfile.TemporaryDirectory(prefix="rsi-resume-check-") as temporary:
        base = Path(temporary)
        (base / run_id).symlink_to(source, target_is_directory=True)
        archive = base / "check"
        (archive / "control").mkdir(parents=True)
        replay = NativeResume(plan, archive)
        task = CohortStudiesTask(**plan["effective_config"]["task"]["params"])
        query = task.reset()
        for _ in replay.turns:
            result = task.step(replay.replay_response(query))
            replay.verify_observation(result.observation)
            query = result.next_query
        assert replay.replay_response(query) is None
        actual = [x for x in task.get_instance_outcomes()]
        expected = replay.trace["instance_outcomes"]
        assert len(actual) == len(expected) == 6
        for current, previous in zip(actual, expected):
            assert current.instance_id == previous["instance_id"]
            assert current.instance_index == previous["instance_index"]
            assert abs(current.reward - previous["reward"]) < 1e-10
        workspace = base / "workspace"
        workspace.mkdir()
        system = SimpleNamespace(_tmp_dir=str(workspace), _last_cumulative_token_usage_by_conversation={})
        replay.restore_thread(system)
        assert system._conversation_id == replay.session_id
        assert len(list(workspace.rglob("*.jsonl"))) == 1
        assert not list(workspace.rglob("auth.json"))
        # A changed public query must fail before using any saved action.
        other = base / "tamper"
        (other / "control").mkdir(parents=True)
        check = NativeResume(plan, other)
        query = task.reset()
        query.prompt += " changed"
        try:
            check.replay_response(query)
            raise AssertionError("Changed query was accepted")
        except RuntimeError as exc:
            assert "Public query differs" in str(exc)
        report = {"passed": True, "source_run": run_id, "replayed_interactions": replay.position,
                  "identical_environment_observations": replay.position, "identical_original_outcomes": len(actual),
                  "native_thread_restored": True, "auth_files_copied": False, "changed_query_rejected": True,
                  "model_calls": 0, "helper_sha256": hashlib.sha256((ROOT / "scripts/native_resume.py").read_bytes()).hexdigest()}
    (ROOT / "reports/native-resume-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
