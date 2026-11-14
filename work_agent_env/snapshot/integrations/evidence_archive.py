"""Private evidence copies, verified against retained originals; no extraction.

This is deliberately independent of mainline input collection. Creating a ZIP
does not reduce the current audit's input count or replace its freshness checks.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import zipfile

from workenv.core import contained, no_links
from workenv.leases import owned_lock

SCHEMA = "private-evidence-archive-v1"
REBASED_SCHEMA = "private-evidence-archive-v2"
DRAFT_ARCHIVE_SCHEMA = "private-evidence-archive-v3"
SELECTION = "LOGICAL_SELECTION.json"
DIRECTORY = ".runtime/evidence-archives"
MANIFEST = "EVIDENCE_MANIFEST.json"
AUDIT_COPY = "SOURCE_AUDIT.json"
AUDIT = "evidence/mainline/consistency_audit.json"
MAX_MEMBERS = 4096
MAX_FILE = 32 * 1024 * 1024
MAX_CONTENT = 128 * 1024 * 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
CHUNK = 64 * 1024


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA256")
    return value


def evidence_name(name):
    """Positive allowlist of private runtime evidence, never arbitrary files."""
    if (not isinstance(name, str) or len(name) > 512 or "\\" in name or ":" in name
        or any(ord(c) < 32 for c in name)):
        raise ValueError("Noncanonical evidence path")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p for p in parts):
        raise ValueError("Noncanonical evidence path")
    if not (len(parts) >= 4 and parts[:2] == [".runtime", "native-episodes"]
            or len(parts) >= 3 and parts[0] == "runs"
            or len(parts) >= 4 and parts[:2] == ["evidence", "mainline"]):
        raise ValueError("Evidence path is outside the archive allowlist")
    # Do not traverse PG storage or capture secrets/configuration as evidence.
    if any(p.startswith(".") or p.lower() in {"pgdata", "data", "base", "global", "pg_wal", "secrets"}
           for p in parts[2:]):
        raise ValueError("Database/private configuration path cannot be archived")
    if PurePosixPath(name).suffix.lower() not in {".json", ".log", ".txt", ".csv", ".sql", ".xlsx", ".py", ".mjs"}:
        raise ValueError("Unsupported evidence file type")
    if any(word in parts[-1].lower() for word in ("password", "credential", "secret")):
        raise ValueError("Credential file cannot be archived")
    return name


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    def reject(_): raise ValueError("Nonfinite JSON value")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=reject)


def _bytes(root, name, limit):
    path = contained(root, name)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Missing or oversized file: " + name)
    with path.open("rb") as stream: raw = stream.read(limit + 1)
    if len(raw) > limit: raise ValueError("File grew beyond budget")
    return raw


def _hash_stream(stream, limit):
    digest, count = hashlib.sha256(), 0
    while True:
        chunk = stream.read(CHUNK)
        if not chunk: return digest.hexdigest(), count
        count += len(chunk)
        if count > limit: raise ValueError("Expanded member exceeds byte budget")
        digest.update(chunk)


def _manifest(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "source_audit", "members"}
        or value["schema_version"] not in (SCHEMA, REBASED_SCHEMA, DRAFT_ARCHIVE_SCHEMA)):
        raise ValueError("Invalid archive manifest schema")
    binding = value["source_audit"]
    source_ref = "evidence/mainline/evidence_selection_draft.json" if value["schema_version"] == DRAFT_ARCHIVE_SCHEMA else AUDIT
    if not isinstance(binding, dict) or set(binding) != {"ref", "sha256"} or binding["ref"] != source_ref:
        raise ValueError("Invalid source audit binding")
    checksum(binding["sha256"])
    members = value["members"]
    if not isinstance(members, dict) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ValueError("Archive member count exceeds budget")
    total = 0
    for name, entry in members.items():
        evidence_name(name)
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
            or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE):
            raise ValueError("Invalid member size/schema")
        checksum(entry["sha256"])
        total += entry["size"]
        if total > MAX_CONTENT: raise ValueError("Expanded archive exceeds byte budget")
    return total


def _verify(root, path):
    """Read every member and every retained original. Never extract or execute."""
    no_links(path, root)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Archive exceeds compressed byte budget")
    with path.open("rb") as stream:
        archive_sha, archive_size = _hash_stream(stream, MAX_ARCHIVE)
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if MAX_MEMBERS < 1 or not 3 <= len(entries) <= MAX_MEMBERS + 3:
            raise ValueError("ZIP entry count exceeds budget")
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names) or names.count(MANIFEST) != 1 or names.count(AUDIT_COPY) != 1:
            raise ValueError("Duplicate or missing archive member")
        total = 0
        for entry in entries:
            if (entry.orig_filename != entry.filename or entry.is_dir() or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError("Unsupported ZIP member type")
            limit = MAX_MANIFEST if entry.filename in (MANIFEST, AUDIT_COPY, SELECTION) else MAX_FILE
            if entry.filename not in (MANIFEST, AUDIT_COPY, SELECTION): evidence_name(entry.filename)
            if entry.file_size > limit: raise ValueError("ZIP member exceeds byte budget")
            total += entry.file_size
            if total > MAX_CONTENT + 3 * MAX_MANIFEST: raise ValueError("ZIP expansion exceeds budget")
        value = _json(archive.read(MANIFEST))
        content_bytes = _manifest(value)
        metadata = (MANIFEST, AUDIT_COPY) if value["schema_version"] == SCHEMA else (MANIFEST, AUDIT_COPY, SELECTION)
        if len(entries) > MAX_MEMBERS + len(metadata): raise ValueError("ZIP entry count exceeds budget")
        if total > MAX_CONTENT + len(metadata) * MAX_MANIFEST: raise ValueError("ZIP expansion exceeds budget")
        if set(names) != {*metadata, *value["members"]}:
            raise ValueError("ZIP members disagree with manifest")
        audit_raw = archive.read(AUDIT_COPY)
        if hashlib.sha256(audit_raw).hexdigest() != value["source_audit"]["sha256"]:
            raise ValueError("Captured source audit bytes changed")
        audit = _json(audit_raw)
        if (value["schema_version"] == DRAFT_ARCHIVE_SCHEMA) != (audit.get("schema_version") == "evidence-selection-draft-v1"):
            raise ValueError("Archive version and source draft type disagree")
        if value["schema_version"] in (REBASED_SCHEMA, DRAFT_ARCHIVE_SCHEMA):
            logical = _selection(audit, _json(archive.read(SELECTION)))
        else:
            _audit_header(audit)
            logical = audit["input_sha256"]
        selected = {}
        for name, digest in logical.items():
            try: evidence_name(name)
            except ValueError: continue
            selected[name] = checksum(digest)
        if selected != {name: entry["sha256"] for name, entry in value["members"].items()}:
            raise ValueError("Archive selection differs from captured audit inputs")
        for name, expected in value["members"].items():
            info = archive.getinfo(name)
            if info.file_size != expected["size"]: raise ValueError("Member length disagrees with manifest")
            with archive.open(info) as stream: observed = _hash_stream(stream, MAX_FILE)
            if observed != (expected["sha256"], expected["size"]):
                raise ValueError("Archived member bytes changed: " + name)
            original = contained(root, name)
            if not original.is_file() or original.stat().st_size != expected["size"]:
                raise ValueError("Missing or resized retained original: " + name)
            with original.open("rb") as stream: actual = _hash_stream(stream, MAX_FILE)
            if actual != observed: raise ValueError("Retained original bytes changed: " + name)
    # Detect replacement during verification; original evidence remains mandatory.
    with path.open("rb") as stream: after = _hash_stream(stream, MAX_ARCHIVE)
    if after != (archive_sha, archive_size): raise ValueError("Archive changed during verification")
    return {"schema_version": value["schema_version"], "passed": True, "archive_sha256": archive_sha,
            "archive_bytes": archive_size, "member_count": len(value["members"]),
            "expanded_bytes": content_bytes, "source_audit": value["source_audit"],
            "originals_rechecked": True, "originals_removed": False,
            "mainline_input_capacity_released": 0, "mainline_collector_integrated": False}


def _audit_header(audit, *, grouped=False):
    if not isinstance(audit, dict): raise ValueError("Invalid source audit")
    checks, hashes = audit.get("checks"), audit.get("input_sha256")
    if (audit.get("schema_version") != "mainline-consistency-audit-v1" or audit.get("passed") is not True
        or (not grouped and ("archive_inputs" in audit or "input_budget" in audit))
        or not isinstance(checks, list) or not 1 <= len(checks) <= 4096
        or any(not isinstance(c, dict) or c.get("passed") is not True or not isinstance(c.get("name"), str) for c in checks)
        or len({c["name"] for c in checks}) != len(checks)
        or not isinstance(hashes, dict) or not 1 <= len(hashes) <= 4096):
        raise ValueError("Archive creation requires a current passing mainline audit")


def _selection(audit, value):
    """Validate a grouped snapshot, never reinterpret it as a flat audit."""
    if audit.get("schema_version") == "evidence-selection-draft-v1":
        from integrations.archive_inputs import draft_header
        logical = draft_header(audit)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "input_sha256"}
            or value.get("schema_version") != "archive-logical-selection-v1" or value.get("input_sha256") != logical):
            raise ValueError("Archive selection differs from the captured draft")
        for name in logical:
            if (not isinstance(name, str) or len(name)>512 or "\\" in name or ":" in name
                or any(ord(c)<32 for c in name) or any(p in ("", ".", "..") or p.rstrip(" .")!=p for p in name.split("/"))):
                raise ValueError("Noncanonical draft logical input reference")
        return logical
    _audit_header(audit, grouped=True)
    spec = audit.get("archive_inputs")
    if (not isinstance(spec, dict) or set(spec) != {"accounting", "archive_ref", "used_members"}
        or spec["accounting"] != "archive-authorities-v1"
        or not isinstance(spec["archive_ref"], str)
        or not re.fullmatch(re.escape(DIRECTORY) + r"/[0-9a-f]{64}\.zip", spec["archive_ref"])):
        raise ValueError("Rebase requires a grouped archive source")
    checksum(audit["input_sha256"].get(spec["archive_ref"]))
    used = spec["used_members"]
    if (not isinstance(used, list) or len(used) > MAX_MEMBERS or any(not isinstance(n, str) for n in used)
        or used != sorted(set(used))): raise ValueError("Invalid source archived member selection")
    if (not isinstance(value, dict) or set(value) != {"schema_version", "input_sha256"}
        or value["schema_version"] != "archive-logical-selection-v1"
        or not isinstance(value["input_sha256"], dict) or not 1 <= len(value["input_sha256"]) <= 8191):
        raise ValueError("Invalid bounded logical selection snapshot")
    logical = value["input_sha256"]
    for name, sha in logical.items():
        if (not isinstance(name, str) or len(name) > 512 or "\\" in name or ":" in name
            or any(ord(c) < 32 for c in name) or any(p in ("", ".", "..") or p.rstrip(" .") != p for p in name.split("/"))):
            raise ValueError("Noncanonical logical input reference")
        checksum(sha)
    direct = {n: sha for n, sha in audit["input_sha256"].items() if n != spec["archive_ref"]}
    if (set(direct) & set(used) or set(logical) != set(direct) | set(used)
        or any(logical[n] != sha for n, sha in direct.items())):
        raise ValueError("Logical selection differs from grouped direct/member inputs")
    for name in used: evidence_name(name)
    actual = hashlib.sha256(json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual != audit.get("logical_input_manifest_sha256"):
        raise ValueError("Logical selection differs from source audit digest")
    return logical


def _source(root, rebase, *, draft=False):
    from integrations.mainline_audit import inputs_current
    source_ref = "evidence/mainline/evidence_selection_draft.json" if draft else AUDIT
    raw = _bytes(root, source_ref, MAX_MANIFEST)
    audit = _json(raw)
    if draft:
        from integrations.archive_inputs import draft_current
        draft_current(root, audit)
    else:
        _audit_header(audit, grouped=rebase)
        if not inputs_current(root, audit):
            raise ValueError("Archive creation requires a current passing mainline audit")
    selection_raw = None
    if rebase or draft:
        from integrations.archive_inputs import load, logical_inputs
        if draft: logical = audit["input_sha256"]
        else:
            authority = load(root, audit.get("archive_inputs", {}).get("archive_ref"))
            logical = logical_inputs(audit["input_sha256"], authority, audit["archive_inputs"]["used_members"])
        selection = {"schema_version": "archive-logical-selection-v1", "input_sha256": logical}
        _selection(audit, selection)
        selection_raw = (json.dumps(selection, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(selection_raw) > MAX_MANIFEST: raise ValueError("Logical selection exceeds metadata byte budget")
    else: logical = audit["input_sha256"]
    members = {}
    for name, expected in sorted(logical.items()):
        try: evidence_name(name)
        except ValueError: continue
        payload = _bytes(root, name, MAX_FILE)
        if hashlib.sha256(payload).hexdigest() != expected: raise ValueError("Input changed before archive creation")
        members[name] = {"sha256": expected, "size": len(payload)}
    manifest = {"schema_version": DRAFT_ARCHIVE_SCHEMA if draft else REBASED_SCHEMA if rebase else SCHEMA,
                "source_audit": {"ref": source_ref, "sha256": hashlib.sha256(raw).hexdigest()}, "members": members}
    _manifest(manifest)
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_MANIFEST: raise ValueError("Manifest exceeds byte budget")
    return raw, audit, logical, members, encoded, selection_raw


def inspect(root):
    """Read-only current input accounting and exact pre-activation projection."""
    from integrations.archive_inputs import load
    raw, audit, logical, members, encoded, selection = _source(root, True)
    authority = load(root, audit["archive_inputs"]["archive_ref"])
    unused = set(authority["members"]) - set(audit["archive_inputs"]["used_members"])
    metadata_bytes = len(raw) + len(encoded) + len(selection)
    projected = sum(contained(root, n).stat().st_size for n in logical) + metadata_bytes
    return {"schema_version": "archive-rebase-capacity-observation-v1", "passed": True,
            "actual_logical_inputs": len(logical), "eligible_members": len(members),
            "unused_previous_members": len(unused),
            "unused_previous_member_bytes": sum(authority["members"][n]["size"] for n in unused),
            "current_budget": audit["input_budget"], "candidate_metadata_bytes": metadata_bytes,
            "projected_logical_bytes_before_new_comparison": projected,
            "projected_logical_bytes_fit": projected <= MAX_CONTENT,
            "policy_changed": False, "originals_removed": False, "creates_archive": False}


def verify(root, archive_ref):
    """Content-addressed private ZIP only; an old audit binding is provenance."""
    if not isinstance(archive_ref, str) or not re.fullmatch(re.escape(DIRECTORY) + r"/[0-9a-f]{64}\.zip", archive_ref):
        raise ValueError("Expected content-addressed private archive")
    path = contained(root, archive_ref)
    result = _verify(root, path)
    if path.stem != result["archive_sha256"]: raise ValueError("Archive SHA filename mismatch")
    result["archive_ref"] = archive_ref
    return result


def create(root, *, rebase=False, draft=False):
    """Archive only evidence already bound by a current, fully passing audit."""
    from integrations.mainline_audit import inputs_current
    root = Path(root).absolute()
    if type(rebase) is not bool or type(draft) is not bool or rebase and draft:
        raise ValueError("Archive creation modes must be distinct booleans")
    raw, audit, logical, members, encoded, selection = _source(root, rebase, draft=draft)
    if sum(contained(root, n).stat().st_size for n in logical) + len(raw) + len(encoded) + (len(selection) if selection else 0) > MAX_CONTENT:
        raise ValueError("Candidate logical audit content exceeds 128 MiB")
    directory = contained(root, DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with owned_lock(directory / "create.lock", root, SCHEMA):
        pending = directory / ("pending-" + uuid.uuid4().hex + ".zip")
        # Failed/interrupted packages are intentionally retained for inspection.
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            metadata = {MANIFEST: encoded, AUDIT_COPY: raw}
            if selection is not None: metadata[SELECTION] = selection
            for name in (*metadata, *members):
                payload = metadata[name] if name in metadata else _bytes(root, name, MAX_FILE)
                if name not in metadata and hashlib.sha256(payload).hexdigest() != members[name]["sha256"]:
                    raise ValueError("Input changed during archive creation")
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, payload)
                if pending.stat().st_size > MAX_ARCHIVE: raise ValueError("Archive exceeds compressed byte budget")
        result = _verify(root, pending)
        if draft:
            from integrations.archive_inputs import DRAFT, draft_current
            still_current = _bytes(root, DRAFT, MAX_MANIFEST) == raw and draft_current(root, audit)
        else: still_current = _bytes(root, AUDIT, MAX_MANIFEST) == raw and inputs_current(root, audit)
        if not still_current:
            raise ValueError("Audit or its inputs changed during archive creation")
        destination = directory / (result["archive_sha256"] + ".zip")
        ref = destination.relative_to(root).as_posix()
        # Hard-link publication is atomic and refuses replacement; never rename
        # over an earlier artifact. Both files are in the same owned directory.
        try:
            os.link(pending, destination)
            reused = False
        except FileExistsError:
            verify(root, ref)
            reused = True
        pending.unlink()  # Only this invocation's verified temporary copy.
        result.update(archive_ref=ref, reused=reused, selected_from_inputs=len(logical), policy_changed=False)
        return result
