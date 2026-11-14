"""One explicitly selected archive authority, with retained-original freshness."""
import hashlib
import json
import os
from pathlib import Path
import zipfile

from integrations import evidence_archive as ea
from workenv.core import contained, save
from workenv.leases import owned_lock

ACCOUNTING = "archive-authorities-v1"
POLICY = ea.DIRECTORY + "/active.json"
CHECK = "archive_authority_originals_and_logical_budget"
DRAFT_SCHEMA = "evidence-selection-draft-v1"
DRAFT = "evidence/mainline/evidence_selection_draft.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(root, ref):
    observed = ea.verify(root, ref)
    with zipfile.ZipFile(contained(root, ref)) as archive:
        raw = archive.read(ea.MANIFEST)
        manifest = ea._json(raw)
        metadata_bytes = len(raw) + archive.getinfo(ea.AUDIT_COPY).file_size
        if manifest["schema_version"] in (ea.REBASED_SCHEMA, ea.DRAFT_ARCHIVE_SCHEMA):
            metadata_bytes += archive.getinfo(ea.SELECTION).file_size
    # The second opening must still refer to the fully verified archive.
    path = contained(root, ref)
    with path.open("rb") as stream: actual, _ = ea._hash_stream(stream, ea.MAX_ARCHIVE)
    if actual != observed["archive_sha256"]: raise ValueError("Archive changed while loading authority")
    return {"ref": ref, "sha256": actual, "members": manifest["members"],
            "metadata_bytes": metadata_bytes, "observed": observed}


def budget(root, files, authority):
    ref = authority["ref"]
    if not 1 <= len(files) <= 4096 or not 1 <= len(authority["members"]) <= 4096 or files.get(ref) != authority["sha256"]:
        raise ValueError("Archive authority is missing or exceeds the input budget")
    total = authority["observed"]["expanded_bytes"] + authority["metadata_bytes"]
    direct = 0
    for name in files:
        if name == ref: continue
        if name in authority["members"]: raise ValueError("Evidence is counted as both direct and archived")
        path = contained(root, name)
        size = path.stat().st_size
        if not path.is_file() or size > ea.MAX_FILE: raise ValueError("Direct audit file exceeds its byte budget")
        total += size
        direct += 1
    if total > ea.MAX_CONTENT: raise ValueError("Logical audit content exceeds 128 MiB")
    return {"accounting": ACCOUNTING, "authority_files": len(files), "authority_file_limit": 4096,
            "direct_files": direct, "archived_members": len(authority["members"]), "archive_member_limit": 4096,
            "logical_files": direct + len(authority["members"]), "logical_bytes": total,
            "logical_byte_limit": ea.MAX_CONTENT, "archive_transport_bytes": authority["observed"]["archive_bytes"],
            "archive_transport_byte_limit": ea.MAX_ARCHIVE, "archive_metadata_bytes": authority["metadata_bytes"]}


def logical_inputs(files, authority, used):
    if (not isinstance(used, list) or len(used) > 4096 or any(not isinstance(n, str) for n in used)
        or used != sorted(set(used)) or not set(used) <= set(authority["members"])):
        raise ValueError("Invalid archived input member inventory")
    return {**{n: v for n, v in files.items() if n != authority["ref"]},
            **{n: authority["members"][n]["sha256"] for n in used}}


def fields(root, files, authority, used):
    used = sorted(used)
    logical = logical_inputs(files, authority, used)
    return {"archive_inputs": {"accounting": ACCOUNTING, "archive_ref": authority["ref"], "used_members": used},
            "logical_input_manifest_sha256": digest(logical), "input_budget": budget(root, files, authority)}


def current(root, receipt):
    spec = receipt["archive_inputs"]
    if (not isinstance(spec, dict) or set(spec) != {"accounting", "archive_ref", "used_members"}
        or spec["accounting"] != ACCOUNTING): raise ValueError("Unknown archive input accounting")
    authority = load(root, spec["archive_ref"])
    files = receipt["input_sha256"]
    if contained(root, POLICY).exists():
        policy = read_policy(root)
        if POLICY not in files or policy["comparison_ref"] not in files:
            raise ValueError("Current archive policy and provenance were not bound")
    expected = fields(root, files, authority, spec["used_members"])
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Archive accounting or logical input inventory changed")
    if any(type(receipt["input_budget"][key]) is not type(value) for key, value in expected["input_budget"].items()):
        raise ValueError("Archive accounting counter types changed")
    for name, checksum in files.items():
        ea.checksum(checksum)
        if name == authority["ref"]: continue  # Already completely rehashed, including every original.
        with contained(root, name).open("rb") as stream: actual, _ = ea._hash_stream(stream, ea.MAX_FILE)
        if actual != checksum: raise ValueError("Direct audit bytes changed: " + name)
    return True


def capture(root, value, category="audit-snapshots"):
    """Private immutable JSON copies used for migration provenance."""
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    checksum = hashlib.sha256(raw).hexdigest()
    ref = ea.DIRECTORY + "/" + category + "/" + checksum + ".json"
    path = contained(root, ref)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if ea._bytes(root, ref, 4 * ea.MAX_MANIFEST) != raw: raise ValueError("Historical snapshot bytes changed")
    else:
        with os.fdopen(descriptor, "wb") as stream: stream.write(raw)
    return ref, checksum


def read_policy(root):
    path = contained(root, POLICY)
    if not path.exists(): return None
    policy = ea._json(ea._bytes(root, POLICY, 65536))
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "workspace_owner", "archive_ref", "comparison_ref", "comparison_sha256"}
        or policy["schema_version"] != ACCOUNTING or policy["workspace_owner"] != str(Path(root).resolve())):
        raise ValueError("Invalid or foreign archive authority policy")
    ref = policy["comparison_ref"]
    checksum = ea.checksum(policy["comparison_sha256"])
    if ref != ea.DIRECTORY + "/comparisons/" + checksum + ".json": raise ValueError("Invalid comparison snapshot reference")
    raw = ea._bytes(root, ref, 4 * ea.MAX_MANIFEST)
    comparison = ea._json(raw)
    if (hashlib.sha256(raw).hexdigest() != checksum or not isinstance(comparison, dict) or comparison.get("passed") is not True
        or comparison.get("schema_version") not in ("archive-input-equivalence-v1", "archive-authority-equivalence-v1", "draft-authority-equivalence-v1")
        or comparison.get("archive_ref") != policy["archive_ref"]):
        raise ValueError("Archive comparison provenance changed")
    return policy


def compare(root, raw, grouped, raw_summary, grouped_summary):
    """Same program, evidence paths/SHA, checks, absences and task qualification."""
    from integrations.mainline_audit import inputs_current
    if (not raw.get("passed") or not grouped.get("passed") or "archive_inputs" in raw
        or not inputs_current(root, raw) or not inputs_current(root, grouped)):
        raise ValueError("Both audit modes must currently pass")
    raw_checks = {row["name"]: row for row in raw["checks"]}
    grouped_checks = {row["name"]: row for row in grouped["checks"]}
    if (len(raw_checks) != len(raw["checks"]) or len(grouped_checks) != len(grouped["checks"])
        or grouped_checks.pop(CHECK, {}).get("passed") is not True or raw_checks != grouped_checks):
        raise ValueError("Existing audit conclusions differ between modes")
    spec = grouped["archive_inputs"]
    authority = load(root, spec["archive_ref"])
    flat = logical_inputs(grouped["input_sha256"], authority, spec["used_members"])
    for key in ("input_inventory", "expected_absent", "source_sha256", "contracts_sha256"):
        if raw.get(key) != grouped.get(key): raise ValueError("Audit mode context differs: " + key)
    if raw["input_sha256"] != flat or raw_summary != grouped_summary:
        raise ValueError("Logical evidence inputs or task qualification differ")
    raw_ref, raw_sha = capture(root, raw)
    grouped_ref, grouped_sha = capture(root, grouped)
    result = {"schema_version": "archive-input-equivalence-v1", "passed": True, "archive_ref": authority["ref"],
              "raw_audit_ref": raw_ref, "raw_audit_sha256": raw_sha,
              "grouped_audit_ref": grouped_ref, "grouped_audit_sha256": grouped_sha,
              "matched_existing_checks": len(raw_checks), "matched_logical_inputs": len(flat),
              "task_qualification_summary": raw_summary, "grouped_budget": grouped["input_budget"],
              "originals_removed": False, "archive_transport_exported": False}
    return result


def compare_archives(root, before, after, before_summary, after_summary):
    """Compare two bounded authorities without a flat intermediate receipt."""
    from integrations.mainline_audit import inputs_current
    if (before.get("passed") is not True or after.get("passed") is not True
        or "archive_inputs" not in before or "archive_inputs" not in after
        or not inputs_current(root, before) or not inputs_current(root, after)):
        raise ValueError("Both grouped archive audits must currently pass")
    conclusions = []
    for receipt in (before, after):
        rows = receipt.get("checks", [])
        checks = {row["name"]: row for row in rows}
        if len(rows) != len(checks) or checks.pop(CHECK, {}).get("passed") is not True:
            raise ValueError("Archive audit coverage differs")
        conclusions.append(checks)
    if conclusions[0] != conclusions[1]: raise ValueError("Existing archive audit conclusions differ")
    for key in ("input_inventory", "expected_absent", "source_sha256", "contracts_sha256"):
        if before.get(key) != after.get(key): raise ValueError("Archive comparison context differs: " + key)
    inventories = []
    for receipt in (before, after):
        spec = receipt["archive_inputs"]
        authority = load(root, spec["archive_ref"])
        inventories.append(logical_inputs(receipt["input_sha256"], authority, spec["used_members"]))
    if inventories[0] != inventories[1] or before_summary != after_summary:
        raise ValueError("Archive logical inputs or task qualification differ")
    before_ref, before_sha = capture(root, before)
    after_ref, after_sha = capture(root, after)
    return {"schema_version": "archive-authority-equivalence-v1", "passed": True,
            "previous_archive_ref": before["archive_inputs"]["archive_ref"],
            "archive_ref": after["archive_inputs"]["archive_ref"],
            "before_audit_ref": before_ref, "before_audit_sha256": before_sha,
            "grouped_audit_ref": after_ref, "grouped_audit_sha256": after_sha,
            "matched_existing_checks": len(conclusions[0]), "matched_logical_inputs": len(inventories[0]),
            "task_qualification_summary": before_summary, "before_budget": before["input_budget"],
            "grouped_budget": after["input_budget"], "originals_removed": False, "archive_transport_exported": False}


def policy_sha256(root):
    """Read a valid owned policy; never silently replace malformed state."""
    policy = read_policy(root)
    return hashlib.sha256(ea._bytes(root, POLICY, 65536)).hexdigest() if policy is not None else None


def draft_fields(root, files):
    """Actual inputs only; reserve one authority for the future private ZIP."""
    if not isinstance(files, dict) or not 1 <= len(files) <= 8191:
        raise ValueError("Invalid bounded selection input inventory")
    members, direct, total = [], [], 0
    for name, sha in files.items():
        ea.checksum(sha)
        path = contained(root, name)
        size = path.stat().st_size
        if not path.is_file() or size > ea.MAX_FILE: raise ValueError("Selection input exceeds file budget")
        try: ea.evidence_name(name)
        except ValueError: direct.append(name)
        else: members.append(name)
        if name.startswith(ea.DIRECTORY + "/") and name.endswith(".zip"):
            raise ValueError("Selection drafts cannot bind a previous archive ZIP")
        total += size
    if len(members) > 4096 or len(direct) + 1 > 4096 or total > ea.MAX_CONTENT:
        raise ValueError("Selection exceeds member/authority/content capacity")
    return {"selection_draft": {"accounting": DRAFT_SCHEMA, "workspace_owner": str(Path(root).resolve()),
                "members": sorted(members), "direct_files": len(direct), "authority_files": len(direct) + 1,
                "logical_files": len(files), "logical_bytes_before_archive_metadata": total,
                "authority_file_limit": 4096, "archive_member_limit": 4096, "logical_byte_limit": ea.MAX_CONTENT},
            "logical_input_manifest_sha256": digest(files)}


def draft_header(receipt):
    checks, files = receipt.get("checks"), receipt.get("input_sha256")
    if (receipt.get("schema_version") != DRAFT_SCHEMA or receipt.get("passed") is not False
        or receipt.get("checks_passed") is not True or not isinstance(files, dict) or not 1 <= len(files) <= 8191
        or not isinstance(checks, list) or not 1 <= len(checks) <= 128
        or any(not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("passed") is not True for c in checks)
        or len({c["name"] for c in checks}) != len(checks)
        or "archive_inputs" in receipt or "input_budget" in receipt
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("A complete successful selection draft is required, never a passing audit")
    spec = receipt.get("selection_draft")
    if (not isinstance(spec, dict) or spec.get("accounting") != DRAFT_SCHEMA
        or not isinstance(spec.get("workspace_owner"), str)
        or digest(files) != receipt.get("logical_input_manifest_sha256")):
        raise ValueError("Selection draft identity/digest changed")
    for sha in files.values(): ea.checksum(sha)
    keys={"accounting","workspace_owner","members","direct_files","authority_files","logical_files",
          "logical_bytes_before_archive_metadata","authority_file_limit","archive_member_limit","logical_byte_limit"}
    members=[]
    for name in files:
        if not isinstance(name,str): raise ValueError("Invalid selection input path")
        try:ea.evidence_name(name)
        except ValueError:pass
        else:members.append(name)
    direct=len(files)-len(members)
    expected={"members":sorted(members),"direct_files":direct,"authority_files":direct+1,"logical_files":len(files),
              "authority_file_limit":4096,"archive_member_limit":4096,"logical_byte_limit":ea.MAX_CONTENT}
    if (set(spec)!=keys or len(members)>4096 or direct+1>4096
        or any(spec.get(k)!=v or type(spec.get(k)) is not type(v) for k,v in expected.items())
        or type(spec.get("logical_bytes_before_archive_metadata")) is not int
        or not 0<=spec["logical_bytes_before_archive_metadata"]<=ea.MAX_CONTENT):
        raise ValueError("Selection draft partition/budget changed")
    return files


def draft_inputs_current(root, receipt):
    """Separate predicate; ordinary inputs_current always rejects drafts."""
    from integrations.mainline_audit import inventory_snapshot
    files = receipt["input_sha256"]
    expected = draft_fields(root, files)
    if any(receipt.get(k) != v for k, v in expected.items()): raise ValueError("Selection accounting changed")
    if any(type(receipt["selection_draft"][k]) is not type(v) for k,v in expected["selection_draft"].items()):
        raise ValueError("Selection accounting counter types changed")
    for name, sha in files.items():
        with contained(root, name).open("rb") as stream: actual, _ = ea._hash_stream(stream, ea.MAX_FILE)
        if actual != sha: raise ValueError("Selection input bytes changed: " + name)
    missing = receipt.get("expected_absent")
    if (not isinstance(missing, list) or len(missing) > 4096 or any(not isinstance(n, str) for n in missing)
        or missing != sorted(set(missing)) or any(contained(root,n).exists() for n in missing)):
        raise ValueError("Selection absence inventory changed")
    if receipt.get("input_inventory") != inventory_snapshot(root): raise ValueError("Selection program/receipt inventory changed")
    return True


def draft_current(root, receipt):
    from integrations.mainline_audit import source_binding
    draft_header(receipt)
    draft_inputs_current(root, receipt)
    source_binding(root, receipt.get("source_sha256"))
    raw = ea._bytes(root, "evidence/mainline/contracts.json", ea.MAX_MANIFEST)
    contracts = ea._json(raw)
    if contracts.get("passed") is not True or hashlib.sha256(raw).hexdigest() != receipt.get("contracts_sha256"):
        raise ValueError("Selection contracts are not current")
    source_binding(root, contracts.get("source_sha256"))
    return True


def compare_draft(root, draft, after, summary):
    from integrations.mainline_audit import inputs_current
    from training.runtime_capabilities import project_capabilities
    draft_current(root, draft)
    if after.get("passed") is not True or not inputs_current(root, after):
        raise ValueError("Candidate requires a current ordinary passing audit")
    if ea._json(ea._bytes(root, ea.AUDIT, ea.MAX_MANIFEST)) != after or project_capabilities(root)["summary"] != summary:
        raise ValueError("Candidate qualification does not match the current ordinary audit")
    conclusions = []
    for receipt in (draft, after):
        rows = receipt.get("checks", [])
        checks = {row["name"]:row for row in rows}
        if (len(checks) != len(rows) or any(c.get("passed") is not True for c in checks.values())
            or checks.pop(CHECK, {}).get("passed") is not True):
            raise ValueError("Draft/candidate check coverage differs")
        conclusions.append(checks)
    if conclusions[0] != conclusions[1]: raise ValueError("Draft/candidate business conclusions differ")
    for key in ("input_inventory", "expected_absent", "source_sha256", "contracts_sha256"):
        if draft.get(key) != after.get(key): raise ValueError("Draft/candidate context differs: " + key)
    spec = after["archive_inputs"]
    logical = logical_inputs(after["input_sha256"], load(root,spec["archive_ref"]), spec["used_members"])
    if draft["input_sha256"] != logical: raise ValueError("Draft/candidate logical evidence differs")
    before_ref, before_sha = capture(root, draft)
    after_ref, after_sha = capture(root, after)
    return {"schema_version":"draft-authority-equivalence-v1", "passed":True,
            "archive_ref":spec["archive_ref"], "before_audit_ref":before_ref, "before_audit_sha256":before_sha,
            "grouped_audit_ref":after_ref, "grouped_audit_sha256":after_sha,
            "matched_existing_checks":len(conclusions[0]), "matched_logical_inputs":len(logical),
            "task_qualification_summary":summary, "grouped_budget":after["input_budget"],
            "draft_is_task_acceptance":False,"originals_removed":False,"archive_transport_exported":False}


def activate(root, grouped, comparison, *, expected_policy_sha256=None):
    from integrations.mainline_audit import inputs_current
    drafted = comparison.get("schema_version") == "draft-authority-equivalence-v1"
    rebased = comparison.get("schema_version") == "archive-authority-equivalence-v1" or drafted
    if rebased: ea.checksum(expected_policy_sha256)
    directory = contained(root, ea.DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with owned_lock(directory / "activate.lock", root, ACCOUNTING):
        old_sha = policy_sha256(root)
        if expected_policy_sha256 is not None and expected_policy_sha256 != old_sha:
            raise ValueError("Archive policy changed since the reviewed plan")
        if (comparison.get("passed") is not True or not inputs_current(root, grouped)
            or grouped.get("archive_inputs", {}).get("archive_ref") != comparison.get("archive_ref")
            or capture(root, grouped)[1] != comparison.get("grouped_audit_sha256")):
            raise ValueError("Activation requires the matching current comparison")
        if rebased:
            sha = ea.checksum(comparison.get("before_audit_sha256"))
            ref = comparison.get("before_audit_ref")
            if ref != ea.DIRECTORY + "/audit-snapshots/" + sha + ".json":
                raise ValueError("Invalid previous audit snapshot reference")
            raw = ea._bytes(root, ref, 4 * ea.MAX_MANIFEST)
            if hashlib.sha256(raw).hexdigest() != sha: raise ValueError("Previous audit snapshot changed")
            summary = comparison.get("task_qualification_summary")
            observed = compare_draft(root, ea._json(raw), grouped, summary) if drafted else compare_archives(root, ea._json(raw), grouped, summary, summary)
            if observed != comparison:
                raise ValueError("Rebase comparison cannot be independently recomputed")
        comparison_ref, comparison_sha = capture(root, comparison, "comparisons")
        old = read_policy(root)
        if old is not None: capture(root, old, "policy-history")
        policy = {"schema_version": ACCOUNTING, "workspace_owner": str(Path(root).resolve()),
                  "archive_ref": comparison["archive_ref"], "comparison_ref": comparison_ref, "comparison_sha256": comparison_sha}
        if policy_sha256(root) != old_sha or not inputs_current(root, grouped):
            raise ValueError("Archive policy or inputs changed before publication")
        save(contained(root, POLICY), policy)
        return policy
