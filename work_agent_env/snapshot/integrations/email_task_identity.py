"""Task-scoped email identity gates; does not launch tools or query business SQL.

The shared runtime consumes this contract. Caller-side cluster ownership and
initialization evidence are checked here; SQL completeness and
business correctness remain the responsibility of the task components.
"""
import ast
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from uuid import UUID

from integrations.episode_cluster import EpisodeCluster
from integrations.email_build import verify_build
from workenv.core import ResourceError, contained
from workenv.process_identity import process_identity
from training import email_task_review as vip, inventory_task_review as inventory
from adapters.inventory_alert_validation import source_identity as inventory_contract

TASKS = MappingProxyType({
    vip.TASK_ID: ("vip-program@store.example.com", "email-initialized.json"),
    inventory.TASK_ID: ("purchasing@ourstore.com", "inventory-initialized.json"),
})
METHODS = ("send_email", "get_emails", "read_email")
BACKENDS = ("emails_mcp.backends.pg_backend.PgIMAPBackend", "emails_mcp.backends.pg_backend.PgSMTPBackend")
SOURCES = ("integrations/email_task_identity.py", "scripts/review_email_task_identity.py", "tests/test_email_task_identity.py",
           "integrations/email_components.py", "adapters/email_pg_native.py", "integrations/email_build.py",
           "training/email_task_review.py", "training/inventory_task_review.py", "adapters/inventory_alert_validation.py",
           "configs/mainline_sources.lock.json", "training/source_api.py", "integrations/episode_cluster.py",
           "workenv/core.py", "workenv/process_identity.py")
REPORT = "evidence/mainline/email_task_identity.json"
CHECK = "fixed_email_task_identity_contract_not_tool_integration"


@dataclass(frozen=True)
class EmailTaskIdentity:
    task_id: str = vip.TASK_ID

    def __post_init__(self):
        if type(self.task_id) is not str or self.task_id not in TASKS:
            raise ValueError("Email task is outside the fixed allowlist")

    @property
    def sender(self): return TASKS[self.task_id][0]

    @property
    def marker(self): return TASKS[self.task_id][1]


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ResourceError("Duplicate identity JSON key")
        value[key] = item
    return value


def _read(root, path, *, limit=65536):
    path = contained(root, path.relative_to(root).as_posix())
    if not path.is_file() or path.stat().st_size > limit: raise ResourceError("Identity record missing or oversized")
    with path.open("rb") as stream: raw = stream.read(limit + 1)
    if len(raw) > limit: raise ResourceError("Identity record grew beyond budget")
    def reject(_): raise ResourceError("Nonfinite identity value")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs, parse_constant=reject)
    if type(value) is not dict: raise ResourceError("Identity record must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _cluster(root, cluster):
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root or cluster.started is not True:
        raise ResourceError("Email identity requires an owned active cluster")
    cluster.require_owner()
    return root


def _preprocess(root, identity):
    if identity.task_id == vip.TASK_ID: return vip.public_blob(root, vip.BASE + "preprocess/main.py")[1]
    return inventory.public_blob(root, "preprocess/main.py")[1]


def require_resolved_requests(root, cluster, identity):
    path = contained(root, (cluster.profile.base / "email-components-run/requests.json").relative_to(root).as_posix())
    if not path.exists(): return
    journal, _ = _read(root, path, limit=32 * 1024 * 1024)
    entries = journal.get("entries")
    if (journal.get("schema_version") != "owned-email-request-journal-v1" or journal.get("profile") != cluster.profile.record()
        or type(entries) is not dict or len(entries) > 128): raise ResourceError("Email journal ownership or budget differs")
    # Existing VIP journals have no task fields. Inventory must never borrow one.
    scoped = "task_id" in journal or "sender" in journal
    if (identity.task_id == inventory.TASK_ID or scoped) and (
        journal.get("task_id") != identity.task_id or journal.get("sender") != identity.sender):
        raise ResourceError("Email journal belongs to another task or sender")
    for request_id, entry in entries.items():
        if (not isinstance(request_id, str) or str(UUID(request_id)) != request_id or type(entry) is not dict
            or entry.get("state") != "committed"):
            raise ResourceError("Unresolved or malformed email request; reset refused, no replay")


def attest_initialization(root, cluster, *, task_id=vip.TASK_ID):
    identity = EmailTaskIdentity(task_id)
    root = _cluster(root, cluster)
    path = cluster.profile.base / identity.marker
    marker, checksum = _read(root, path)
    source = _preprocess(root, identity)
    if (marker.get("state") != "ready" or marker.get("task_id") != task_id or marker.get("profile") != cluster.profile.record()
        or marker.get("preprocess_source") != source or not isinstance(marker.get("readonly_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", marker["readonly_sha256"])
        or "sender" in marker and marker["sender"] != identity.sender):
        raise ResourceError("Email task initialization identity differs")
    if task_id == inventory.TASK_ID and marker.get("contract_source") != inventory_contract():
        raise ResourceError("Inventory initialization contract changed")
    require_resolved_requests(root, cluster, identity)
    return {"schema_version": "email-task-identity-v1", "task_id": task_id, "sender": identity.sender,
            "profile": cluster.profile.record(), "marker_ref": path.relative_to(root).as_posix(),
            "marker_sha256": checksum, "preprocess_source": source, "readonly_sha256": marker["readonly_sha256"]}


def _build_digest(build):
    raw = json.dumps(build, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > 2 * 1024 * 1024: raise ResourceError("Email launch build record exceeds budget")
    return hashlib.sha256(raw).hexdigest()


def make_launch(root, cluster, build, *, task_id=vip.TASK_ID):
    binding = attest_initialization(root, cluster, task_id=task_id)
    if type(build) is not dict: raise ResourceError("Email launch requires a build object")
    verify_build(Path(root).resolve(), build)
    binding["build_sha256"] = _build_digest(build)
    return {"schema_version": "owned-email-task-launch-v1", "profile": cluster.profile.record(),
            "task_binding": binding, "build": deepcopy(build)}


def validate_launch(root, cluster, launch, *, task_id=vip.TASK_ID):
    if type(launch) is not dict or set(launch) != {"schema_version", "profile", "task_binding", "build"}:
        raise ResourceError("Email launch scope is incomplete")
    expected = make_launch(root, cluster, launch["build"], task_id=task_id)
    if launch != expected: raise ResourceError("Email launch was rebound after initialization")
    return deepcopy(expected["task_binding"])


def validate_process(root, cluster, launch, observed, *, task_id=vip.TASK_ID):
    binding = validate_launch(root, cluster, launch, task_id=task_id)
    if type(observed) is not dict: raise ResourceError("Missing owned email child identity")
    birth = observed.get("process")
    if (observed.get("schema_version") != "owned-email-task-process-v1" or observed.get("task_binding") != binding
        or observed.get("profile") != cluster.profile.record() or observed.get("methods") != list(METHODS)
        or observed.get("backend_classes") != list(BACKENDS)
        or observed.get("pg_data_ref") != cluster.profile.data.relative_to(cluster.profile.owner).as_posix()
        or observed.get("build_fingerprint") != launch["build"].get("build_fingerprint")
        or observed.get("forbidden_socket_blocked") is not True
        or type(observed.get("external_messages_sent")) is not int or observed["external_messages_sent"] != 0
        or type(birth) is not dict or type(birth.get("pid")) is not int or birth["pid"] <= 0
        or birth.get("status") != "alive" or not isinstance(birth.get("creation_id"), str) or not birth["creation_id"]):
        raise ResourceError("Email child task, PG backend or process scope differs")
    actual = process_identity(birth["pid"])
    if actual.get("status") != "alive" or actual.get("pid") != birth["pid"] or actual.get("creation_id") != birth["creation_id"]:
        raise ResourceError("Email child process exited, was reused or is unknown")
    return deepcopy(binding)


def describe(root):
    root = Path(root).resolve()
    rows = []
    for task_id in TASKS:
        identity = EmailTaskIdentity(task_id)
        rows.append({"task_id": task_id, "sender": identity.sender, "marker": identity.marker,
                     "preprocess_source": _preprocess(root, identity)})
    imports = {}
    for name in ("integrations/email_components.py", "adapters/email_pg_native.py"):
        path = contained(root, name)
        if path.stat().st_size > 2 * 1024 * 1024: raise ResourceError("Legacy email source exceeds budget")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports[name] = any(
            isinstance(n, ast.ImportFrom) and (n.module in (__name__, "email_task_identity")
                or n.module in (None, "integrations") and any(a.name == "email_task_identity" for a in n.names))
            or isinstance(n, ast.Import) and any(a.name == __name__ for a in n.names) for n in ast.walk(tree))
        if name.startswith("adapters/"):
            values = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "SENDER" for t in n.targets)]
            if values != [EmailTaskIdentity().sender]: raise ResourceError("Legacy VIP sender default changed")
    return {"schema_version": "email-task-identity-contract-v2", "passed": True,
            "scope": "task_identity_contract_not_email_tool_execution", "tasks": rows, "legacy_runtime_imports_contract": imports,
            "runtime_imports_present": all(imports.values()), "runtime_integrated": False,
            "original_email_tools_called": False, "sql_completeness_proven": False,
            "nemo_task_executed": False, "new_runtime_profiles": 0, "real_model_calls": 0, "training_updates": 0,
            "external_messages_sent": 0, "task_passed": None, "reward": None,
            "source_sha256": {n: hashlib.sha256(contained(root, n).read_bytes()).hexdigest() for n in SOURCES}}


def audit_review(root, path):
    from integrations.mainline_audit import read_json, source_binding
    receipt = read_json(path, root)
    if set(receipt.get("source_sha256", {})) != set(SOURCES): raise ValueError("Email identity source coverage differs")
    source_binding(root, receipt["source_sha256"])
    if receipt != describe(root): raise ValueError("Email identity review differs from pinned sources or runtime boundary")
    return {"tasks": 2, "fixed_senders": [row["sender"] for row in receipt["tasks"]],
            "runtime_integrated": False, "original_email_tools_called": False, "new_runtime_profiles": 0}
