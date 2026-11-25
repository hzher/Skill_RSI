"""Host-independent project bindings, events and intervention records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

RECORD_SCHEMA = "skill-rsi.runtime.v1"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    host: str
    event_id: str
    boundary: str
    session_id: str
    payload: Mapping[str, Any]
    input_version: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    lineage_id: str | None = None
    trigger_ref: Any = None

    def __post_init__(self):
        for name in ("host", "event_id", "boundary", "session_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"Runtime event requires {name}")
        if not isinstance(self.payload, Mapping):
            raise ValueError("Runtime event payload must be an object")
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class Binding:
    project_id: str
    host: str
    component_id: str
    version: str
    form: str
    artifact_digest: str
    enabled: bool
    mode: str
    binding_revision: int
    previous_revision: int | None
    reload_boundary: str
    state_version: str | None
    options: dict = field(default_factory=dict)
    effective_boundary_ref: Any = None
    adapter_version: str | None = None
    capability_receipt: dict | None = None
    activation_error: str | None = None

    @property
    def status(self) -> str:
        if self.activation_error:
            return "rejected"
        if self.effective_boundary_ref is None:
            return "pending"
        return "effective" if self.enabled else "disabled"

    def to_dict(self) -> dict:
        return {"schema": RECORD_SCHEMA, **asdict(self), "status": self.status}


@dataclass(frozen=True, slots=True)
class Decision:
    project_id: str
    host: str
    session_id: str
    event_id: str
    boundary: str
    trigger_ref: Any
    scope: dict
    component_id: str
    version: str
    form: str
    artifact_digest: str
    binding_revision: int
    effective_boundary_ref: Any
    mode: str
    decision_id: str
    operation_key: str
    stage: str
    status: str
    reason: str
    proposed_action: dict | None = None
    receipt: dict | None = None
    receipts: tuple[dict, ...] = ()
    costs: dict = field(default_factory=dict)
    run_id: str | None = None
    thread_id: str | None = None
    lineage_id: str | None = None
    memory_before: str | None = None
    memory_after: str | None = None
    options_digest: str | None = None

    def to_dict(self) -> dict:
        return {"schema": RECORD_SCHEMA, **asdict(self)}


class Host(Protocol):
    def capabilities(self, session: Mapping | None = None) -> Mapping: ...

    def deliver(self, action: Mapping, key: str) -> Mapping: ...
