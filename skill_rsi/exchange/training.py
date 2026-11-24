"""Versioned, file-only handoff to the separate skill_rl_train project."""
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from ..evidence.io import digest, encode, file_lock, identifier, inside, read_json, write_bytes, write_json
from ..review.snapshot import load_snapshot

SCHEMA = "skill-rsi.training-evidence.v1"
PREFIX_FIELDS = ("query", "feedback", "memory", "checkpoint_ref", "instance_id", "instance_index", "seed",
                 "host", "host_version", "environment_version", "task_id", "instance_order", "boundary_ref")


_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _reference(value):
    return (isinstance(value, dict) and _sha256(value.get("sha256"))
            and _text(value.get("path")) and isinstance(value.get("locator"), dict))


def _restore_state(state):
    memory = state.get("memory")
    feedback = state.get("feedback")
    return (
        all(_text(state.get(key)) for key in ("query", "host", "host_version", "environment_version", "task_id", "instance_id"))
        and type(state.get("seed")) is int and type(state.get("instance_index")) is int and state["instance_index"] >= 0
        and isinstance(state.get("instance_order"), list) and bool(state["instance_order"])
        and isinstance(memory, dict) and all(_MEMORY_KEY.fullmatch(key) and _text(value) for key, value in memory.items())
        and isinstance(feedback, list) and all(isinstance(value, str) for value in feedback)
        and _reference(state.get("checkpoint_ref")) and isinstance(state.get("boundary_ref"), dict)
        and bool(state["boundary_ref"])
    )


def export_training(snapshot: Path, output: Path, *, dataset_id: str, score_protocol: str):
    index, windows, _ = load_snapshot(snapshot, purpose="training")
    identifier(dataset_id)
    if not isinstance(score_protocol, str) or not score_protocol.strip():
        raise ValueError("score protocol is required")
    authorization_ref = index.get("authorization_ref")
    if not isinstance(authorization_ref, str) or not authorization_ref.strip():
        raise ValueError("training authorization reference is required")
    source_refs, partitions, excluded, semantic_splits = {}, {}, [], {}
    for window in windows:
        split = window.get("split")
        if split not in {"train", "dev"}:
            raise ValueError("training snapshot contains an unexpected split")
        for record in window.get("structure", ()):
            if record.get("phase") not in {"before_boundary", "boundary"}:
                continue
            value = record.get("data")
            if not isinstance(value, dict):
                continue
            visible = value.get("visible_state")
            if not isinstance(visible, dict) or not isinstance(visible.get("query"), str):
                continue
            state = {name: visible[name] for name in PREFIX_FIELDS if name in visible}
            origin = {"window_id": window["window_id"], "revision": window["revision"],
                      "lineage_id": window["lineage_id"], "run_id": window["run_id"],
                      "record_index": record.get("record_index"), "source": record.get("source"),
                      "event_id": value.get("event_id"), "source_ref": value.get("visible_state_source_ref"), "split": split}
            state_id = digest(encode(origin))
            if not origin["source"] or not origin["event_id"] or not _reference(origin["source_ref"]):
                excluded.append({"state_id": state_id, "reason": "source_unavailable"})
                continue
            if not _restore_state(state):
                excluded.append({"state_id": state_id, "reason": "restore_contract_incomplete"})
                continue
            projected_sha = digest(encode(state))
            observation_sha = digest(encode([state[key] for key in ("query", "feedback", "memory")]))
            for identity in (("lineage", origin["lineage_id"]), ("observation", observation_sha)):
                if semantic_splits.setdefault(identity, split) != split:
                    raise ValueError("a training lineage or identical observation crosses splits")
            partitions.setdefault(split, {})[state_id] = {"state_id": state_id, **state}
            source_refs[state_id] = {**origin, "state_id": state_id, "projection_sha256": projected_sha}
    manifest = {"schema": SCHEMA, "dataset_id": dataset_id, "score_protocol": score_protocol,
                "training_permission": "weight_training", "authorization_ref": authorization_ref,
                "snapshot_id": index.get("snapshot_id"), "snapshot_sha256": index.get("snapshot_digest"),
                "partitions": {key: len(rows) for key, rows in partitions.items()},
                "excluded": excluded, "source_proof": "approved_projection_with_external_raw_refs", "files": {}}
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(output.with_name(output.name + ".lock")):
        if output.exists():
            raise FileExistsError(output)
        with TemporaryDirectory(prefix=".skill-rsi-exchange-", dir=output.parent) as temporary:
            stage = Path(temporary) / "bundle"
            stage.mkdir(mode=0o700)
            files = {f"{key}/states.jsonl": rows.values() for key, rows in partitions.items()}
            files["sources.jsonl"] = source_refs.values()
            for name, rows in files.items():
                contents = b"".join(encode(record) + b"\n" for record in rows)
                write_bytes(stage / name, contents)
                manifest["files"][name] = {"sha256": digest(contents), "bytes": len(contents)}
            manifest["content_sha256"] = digest(encode(manifest))
            write_json(stage / "manifest.json", manifest)
            stage.rename(output)
    return manifest


def verify_export(bundle: Path):
    manifest = read_json(bundle / "manifest.json")
    if manifest.get("schema") != SCHEMA or manifest.get("training_permission") != "weight_training":
        raise ValueError("unsupported or unauthorized training bundle")
    summary = {key: value for key, value in manifest.items() if key != "content_sha256"}
    if digest(encode(summary)) != manifest["content_sha256"]:
        raise ValueError("training bundle manifest digest differs")
    partitions = manifest.get("partitions", {})
    if not isinstance(partitions, dict) or any(
        split not in {"train", "dev"} or type(count) is not int or count < 1 for split, count in partitions.items()
    ):
        raise ValueError("invalid training partitions")
    if not _sha256(manifest.get("snapshot_sha256")) or not _text(manifest.get("authorization_ref")):
        raise ValueError("training bundle is missing approval provenance")
    if set(manifest["files"]) != {"sources.jsonl", *(f"{split}/states.jsonl" for split in partitions)}:
        raise ValueError("training bundle contains unexpected or missing files")
    records = {}
    for name, ref in manifest["files"].items():
        contents = inside(bundle, name).read_bytes()
        if len(contents) != ref["bytes"] or digest(contents) != ref["sha256"]:
            raise ValueError(f"training bundle file digest differs: {name}")
        records[name] = [json.loads(line) for line in contents.splitlines() if line]
    sources = {}
    for origin in records["sources.jsonl"]:
        state_id = origin["state_id"]
        identity = {key: value for key, value in origin.items() if key not in {"state_id", "projection_sha256"}}
        if (digest(encode(identity)) != state_id or state_id in sources or not _reference(origin.get("source_ref"))
                or not all(_text(origin.get(key)) for key in ("run_id", "event_id", "lineage_id", "window_id"))
                or not _sha256(origin.get("revision")) or not _sha256(origin.get("projection_sha256"))):
            raise ValueError("invalid approved training state source")
        sources[state_id] = origin
    semantic_splits = {}
    for split, count in partitions.items():
        states = records[f"{split}/states.jsonl"]
        if len(states) != count:
            raise ValueError("training state count differs from manifest")
        for row in states:
            state_id = row["state_id"]
            origin = sources.pop(state_id, None)
            state = {key: value for key, value in row.items() if key != "state_id"}
            if (not origin or origin["split"] != split or state.keys() - set(PREFIX_FIELDS)
                    or not _restore_state(state) or digest(encode(state)) != origin["projection_sha256"]):
                raise ValueError("training state does not match its approved projection")
            observation = digest(encode([state[key] for key in ("query", "feedback", "memory")]))
            for identity in (("lineage", origin["lineage_id"]), ("observation", observation)):
                if semantic_splits.setdefault(identity, split) != split:
                    raise ValueError("a training lineage or identical observation crosses splits")
    if sources:
        raise ValueError("training source has no exported decision state")
    return manifest


def verify_receipt(bundle: Path, receipt_path: Path):
    bundle_manifest = verify_export(bundle)
    receipt = read_json(receipt_path)
    if (receipt.get("schema") != "skill-rsi.training-receipt.v1"
            or receipt.get("bundle_sha256") != bundle_manifest["content_sha256"]
            or receipt.get("dataset_id") != bundle_manifest["dataset_id"]):
        raise ValueError("training receiver receipt does not match this bundle")
    received = receipt.get("accepted")
    if not isinstance(received, dict) or any(
        type(count) is not int or not 0 <= count <= bundle_manifest["partitions"].get(split, -1)
        for split, count in received.items()
    ):
        raise ValueError("invalid accepted state counts")
    return receipt
