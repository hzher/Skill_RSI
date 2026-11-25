"""Adapted from Skill_RSI_hzh/skill_discovery/extract.py (MIT)."""

from pathlib import Path

from ..evidence.io import digest, encode, identifier, write_bytes, write_json
from ..review.projection import SECRET
from ..review.snapshot import load_snapshot
from .implementation import configuration
from .teacher import PROMPT_VERSION, Teacher, TeacherError


SCHEMA = "skill-rsi.discovery.v1"

SECTIONS = {
    "trigger": "Triggers", "preconditions": "Preconditions",
    "required_capabilities": "Required capabilities", "parameters": "Parameters",
    "sop_steps": "Procedure", "verification_steps": "Verification",
    "failure_recovery": "Failure recovery", "applicability": "Applicability",
    "exclusions": "Exclusions", "cost_tradeoffs": "Costs and tradeoffs",
}


def _citation(value):
    if isinstance(value, dict) and isinstance(value.get("run_id"), str) and isinstance(value.get("event_id"), str):
        return value["run_id"], value["event_id"]
    return None


def _review(candidate, events, review):
    if not isinstance(review, str) or review not in {"supported", "insufficient", "rejected"}:
        return "needs_more_evidence", "missing_generalization_review"
    if review == "rejected":
        return "rejected", "generalization_review_rejected"
    for field in ("skill_id", "version"):
        try:
            identifier(candidate.get(field))
        except ValueError:
            return "rejected", f"invalid_{field}"
    for field in ("title", "objective", "expected_mechanism"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            return "rejected", f"invalid_{field}"
    for field in SECTIONS:
        values = candidate.get(field)
        may_be_empty = field in ("required_capabilities", "parameters")
        if (not isinstance(values, list) or (not values and not may_be_empty)
                or not all(isinstance(item, str) and item.strip() for item in values)):
            return "rejected", f"invalid_{field}"
    if candidate.get("evidence_origin") not in ("observed", "synthesized", "hypothesis"):
        return "rejected", "invalid_evidence_origin"
    for field in ("source_refs", "counterexample_refs"):
        values = candidate.get(field)
        if not isinstance(values, list) or any(_citation(item) not in events for item in values):
            return "rejected", f"unread_or_invalid_{field}"
    if review == "insufficient" or not candidate["source_refs"]:
        return "needs_more_evidence", "insufficient_support"
    try:
        configuration(candidate)
    except ValueError:
        return "rejected", "invalid_declarative_implementation"
    rendered = _render(candidate)
    if SECRET.search(rendered):
        return "rejected", "credential_pattern_in_skill"
    if any(ref["run_id"] in rendered or ref["event_id"] in rendered
           for ref in candidate["source_refs"] if len(ref["event_id"]) >= 12):
        return "rejected", "concrete_evidence_identity_in_skill"
    return "candidate", "candidate_ready_for_independent_evaluation"


def _render(candidate):
    title = " ".join(candidate["title"].split())
    objective = " ".join(candidate["objective"].split())
    lines = ["---", f"name: {candidate['skill_id']}", f"description: {encode(objective).decode()}",
             "---", "", f"# {title}", "", objective]
    for field, heading in SECTIONS.items():
        if candidate[field]:
            lines += ["", f"## {heading}", ""]
            lines += ["- " + " ".join(item.split()) for item in candidate[field]]
    return "\n".join(lines) + "\n"


def _compare(teacher, analyses):
    if not analyses:
        raise TeacherError("no_window_read")
    while True:
        groups = [[]]
        for analysis in analyses:
            if not teacher.fits("compare", groups[-1] + [analysis]):
                groups.append([])
            if not teacher.fits("compare", [analysis]):
                raise TeacherError("analysis_exceeds_prompt_budget")
            groups[-1].append(analysis)
        groups = [group for group in groups if group]
        if len(groups) > 1 and len(groups) == len(analyses):
            raise TeacherError("comparison_requires_larger_prompt_budget")
        analyses = [teacher.ask("compare", group) for group in groups]
        if len(analyses) == 1:
            return analyses[0]


def extract(snapshot: Path, output: Path, extraction_id: str, teacher_config: dict):
    index, windows, events = load_snapshot(snapshot, purpose="discovery")
    root = output / identifier(extraction_id)
    teacher = Teacher(root, teacher_config)
    index = {**index, "reading_coverage": [], "unread_refs": [window["window_id"] for window in windows]}
    index.setdefault("missing_reasons", [])
    root.mkdir(parents=True, exist_ok=False)
    index["extraction_id"] = extraction_id
    manifest = {
        "schema_version": SCHEMA, "extraction_id": extraction_id, "part": "part2",
        "extraction_status": "partial", "validation_status": "not_tested",
        "extractor_model_id": teacher.config["model"], "executor_model_ids": index.get("executor_model_ids", []),
        "extractor_config": teacher.config, "prompt_version": PROMPT_VERSION,
        "strength_basis": teacher.config["strength_basis"], "strength_evidence": "pending",
        "failures": [],
    }
    write_json(root / "input_index.json", index)
    write_json(root / "manifest.json", manifest)
    outcomes = []
    try:
        read_events = set()
        analyses = []
        for window in windows:
            window_id = window["window_id"]
            try:
                content = {key: window[key] for key in (
                    "window_id", "run_id", "event_ids", "revision", "visibility", "structure", "records"
                ) if key in window}
                if "structure" in content:
                    content.pop("records", None)
                analysis = teacher.ask("read", content, [window_id])
            except TeacherError as exc:
                manifest["failures"].append({"window_id": window_id, "reason": str(exc)})
                if str(exc) in {"call_budget_exhausted", "teacher_credential_unavailable"}:
                    break
                continue
            read_events.update((window["run_id"], event) for event in window.get("event_ids", ()))
            analyses.append({"window_id": window_id, "analysis": analysis})
            index["reading_coverage"].append({"window_id": window_id, "call_id": teacher.calls[-1]["call_id"]})
            index["unread_refs"].remove(window_id)
        analysis = _compare(teacher, analyses)
        proposal = teacher.ask("propose", analysis)
        proposed = proposal.get("candidates")
        if not isinstance(proposed, list):
            raise TeacherError("invalid_candidate_collection")
        keys = set()
        for number, original in enumerate(proposed, 1):
            if not isinstance(original, dict):
                outcomes.append({"candidate_id": f"{extraction_id}-{number}", "candidate_status": "rejected",
                                 "validation_status": "not_tested", "reason": "candidate_must_be_object"})
                continue
            try:
                generalization = teacher.ask("generalize", original)
            except TeacherError as exc:
                generalization = {"candidate": original, "review": "insufficient", "reason": str(exc)}
                manifest["failures"].append({"candidate_number": number, "reason": str(exc)})
            candidate = generalization.get("candidate")
            if not isinstance(candidate, dict):
                candidate = original
                generalization["review"] = "insufficient"
            keys_to_keep = set(SECTIONS) | {"skill_id", "version", "parent_version", "title", "objective",
                                            "evidence_origin", "expected_mechanism", "source_refs",
                                            "counterexample_refs", "behavior_tags", "hypothesis", "implementation"}
            candidate = {key: value for key, value in candidate.items() if key in keys_to_keep}
            status, reason = _review(candidate, events & read_events, generalization.get("review"))
            if (candidate.get("skill_id"), candidate.get("version")) != (original.get("skill_id"), original.get("version")):
                status, reason = "rejected", "candidate_identity_changed_during_generalization"
            if status == "candidate":
                key = candidate["skill_id"], candidate["version"]
                if key in keys:
                    status, reason = "rejected", "duplicate_candidate_identity"
                else:
                    keys.add(key)
                    target = root / "skills" / key[0] / key[1] / "SKILL.md"
                    content = _render(candidate).encode()
                    write_bytes(target, content)
                    candidate.update({"skill_path": target.relative_to(root).as_posix(), "skill_sha256": digest(content)})
                    if implementation := configuration(candidate):
                        candidate["configuration_sha256"] = digest(encode(implementation))
                        write_json(target.with_name("implementation.json"), implementation)
            outcomes.append(candidate | {
                "candidate_id": f"{extraction_id}-{number}", "candidate_status": status,
                "extraction_id": extraction_id, "validation_status": "not_tested",
                "reason": reason, "generalization_review": generalization.get("reason"),
            })
        manifest["zero_candidate_reason"] = None if proposed else proposal.get("gaps") or "teacher_proposed_no_candidates"
        manifest["comparison_gaps"] = analysis.get("gaps", [])
        manifest["proposal_gaps"] = proposal.get("gaps", [])
        if not index["unread_refs"] and not index["missing_reasons"] and not manifest["failures"]:
            manifest["extraction_status"] = "completed"
    except TeacherError as exc:
        manifest["failures"].append({"reason": str(exc)})
        manifest["extraction_status"] = "partial" if index["reading_coverage"] else "error"
    finally:
        write_json(root / "input_index.json", index)
        write_bytes(root / "candidates.jsonl", b"".join(encode(row) + b"\n" for row in outcomes))
        write_json(root / "evidence.json", {"schema_version": SCHEMA, "extraction_id": extraction_id,
                                           "candidates": outcomes, "gaps": manifest["failures"]})
        manifest.update({"teacher_calls": teacher.calls, "teacher_usage": teacher.usage_summary(),
                         "candidate_count": sum(row["candidate_status"] == "candidate" for row in outcomes)})
        write_json(root / "manifest.json", manifest)
    return manifest
