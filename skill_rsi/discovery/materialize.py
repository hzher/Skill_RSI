"""Package an explicitly selected candidate only after its contents have been reviewed."""
from importlib.resources import files
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ..components import builtin_policy_assets, pack
from ..evidence.io import digest, encode, inside, read_json, write_json
from ..review.projection import SECRET
from .implementation import configuration


def materialize(extraction: Path, skill_id: str, version: str, review: dict, output: Path):
    rows = [json.loads(line) for line in (extraction / "candidates.jsonl").read_text().splitlines() if line]
    matches = [
        row for row in rows
        if (row.get("skill_id"), row.get("version"), row.get("candidate_status")) == (skill_id, version, "candidate")
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one statically reviewed discovery candidate")
    candidate, = matches
    skill = inside(extraction, candidate["skill_path"]).read_bytes()
    sha = digest(skill)
    if candidate["skill_sha256"] != sha:
        raise ValueError("candidate bytes differ from its discovery manifest")
    if (review.get("approved_sha256") != sha or not isinstance(review.get("review_ref"), str)
            or not review["review_ref"].strip()):
        raise ValueError("provide the reviewed Skill digest and review reference")
    if SECRET.search(skill.decode("utf-8")):
        raise ValueError("candidate contains a credential pattern")
    manifest = {
        "schema": "skill-rsi.component.v1",
        "component_id": skill_id,
        "version": version,
        "form": "skill",
        "entrypoint": "payload/SKILL.md",
        "required_capabilities": {"apply": ["append_context"], "suggest": ["suggest"], "shadow": []},
        "scope": {"hosts": review.get("hosts", ["*"]), "modes": ["shadow", "suggest", "apply"]},
        "trigger": {"boundaries": review.get("boundaries", ["before_action"])},
        "action": {"kind": "append_context", "target": "context.skill." + skill_id, "writes": []},
        "budget": {"max_action_bytes": review.get("max_action_bytes", 16384)},
        "fallback": "native",
    }
    implementation = configuration(candidate)
    assets = {"payload/SKILL.md": skill}
    if implementation:
        approved_config = review.get("approved_configuration_sha256")
        if approved_config != candidate.get("configuration_sha256") or approved_config != digest(encode(implementation)):
            raise ValueError("provide the approved declarative implementation digest")
        strategy = implementation["strategy"]
        template = files("skill_rsi.components.resources").joinpath(strategy, "manifest.json")
        manifest = json.loads(template.read_text(encoding="utf-8"))
        manifest.update(component_id=skill_id, version=version, builtin_policy=strategy,
                        defaults=implementation["defaults"])
        if "hosts" in review:
            hosts = review["hosts"]
            if not isinstance(hosts, list) or not set(hosts) <= set(manifest["scope"]["hosts"]):
                raise ValueError("reviewed hosts must be a subset of the implementation scope")
            manifest["scope"]["hosts"] = hosts
        assets.update(builtin_policy_assets(strategy))
        assets["implementation.json"] = encode(implementation) + b"\n"
    manifest_source = read_json(extraction / "manifest.json")
    input_bytes = (extraction / "input_index.json").read_bytes()
    inputs = json.loads(input_bytes)
    cited = {(ref["run_id"], ref["event_id"])
             for key in ("source_refs", "counterexample_refs") for ref in candidate[key]}
    read_windows = {row["window_id"] for row in inputs["reading_coverage"]}
    evidence = {"candidate_id": candidate["candidate_id"], "snapshot_sha256": inputs["snapshot_digest"],
                "input_index_sha256": digest(input_bytes), "source_refs": candidate["source_refs"],
                "counterexample_refs": candidate["counterexample_refs"],
                "windows": [{key: window[key] for key in ("window_id", "revision", "sha256")}
                            for window in inputs["windows"] if window["window_id"] in read_windows
                            and any((ref["run_id"], ref["event_id"]) in cited for ref in window["event_refs"])]}
    observed = {(ref["run_id"], ref["event_id"]) for window in inputs["windows"] if window["window_id"] in read_windows
                for ref in window["event_refs"]}
    if cited - observed:
        raise ValueError("candidate references are outside the discovery reading scope")
    assets["evidence.json"] = encode(evidence) + b"\n"
    with TemporaryDirectory(prefix="skill-rsi-candidate-") as stage:
        source = Path(stage)
        for name, content in assets.items():
            path = inside(source, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        write_json(source / "review.json", {
            "approved_sha256": sha, "review_ref": review["review_ref"],
            "configuration_sha256": candidate.get("configuration_sha256"),
            "candidate_id": candidate["candidate_id"], "prompt_version": manifest_source["prompt_version"],
        })
        write_json(source / "manifest.json", manifest)
        return pack(source, output)
