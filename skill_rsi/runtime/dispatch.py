"""Evaluate actions against a frozen binding, then commit through the selected host."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from time import monotonic, time
from typing import Any, Mapping
import json

from ..components.manifest import canonical, required_capabilities, sha256
from .bindings import ProjectBindings, capabilities_of, check_scope, supports_action
from .model import Binding, Decision, Host, RuntimeEvent
from .policy import DEFAULT_BUDGET, Original, PolicyContext, action_capability, normalized_action, propose, state_update
from .store import decision_from, forget_reference, identity, json_text, reference, save_decision

_IN_FLIGHT = frozenset({"queued", "pending", "unknown", "submitted", "accepted"})
_STAGE_ORDER = {
    "proposed": 0, "shadow": 0, "skipped": 0, "state_committed": 1,
    "queued": 1, "submitted": 2, "displayed": 3, "adopted": 4, "adoption_submitted": 5,
    "delivered": 6, "outcome": 7,
}


@dataclass(frozen=True, slots=True)
class _Draft:
    decision: Decision
    binding: Binding
    state: dict | None = None
    originals: tuple[Original, ...] = ()
    namespace: str = "live"
    lineage: str = ""


def _receipt_data(value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("Host receipt must be an object")
    record = dict(value)
    aliases = {"submission": "submitted", "display": "displayed", "delivery": "delivered", "applied": "delivered"}
    if isinstance(record.get("stage"), str):
        record["stage"] = aliases.get(record["stage"], record["stage"])
    if not isinstance(record.get("status"), str) or not record["status"]:
        raise ValueError("Host receipt requires a status")
    if not isinstance(record.get("stage"), str) or record["stage"] not in _STAGE_ORDER:
        raise ValueError("Host receipt requires a recognized lifecycle stage")
    if record["status"] == "unknown":
        record.setdefault("missing_reason", "host_outcome_unobservable")
    return json.loads(canonical(record))


class Runtime(ProjectBindings):
    def dispatch(self, event: RuntimeEvent, adapter: Host) -> tuple[Decision, ...]:
        """Plan every bound component before delivering anything for this host event."""
        description = dict(adapter.capabilities({
            "project": str(self.project), "project_id": self.project_id, "session_id": event.session_id,
            "boundary": event.boundary, "event_id": event.event_id,
        }))
        event_key = identity(["event", self.project_id, event.host, event.session_id, event.thread_id, event.event_id,
                              event.boundary])
        fingerprint = identity(asdict(event))
        with self.store.db.transaction(write=True) as connection:
            known = connection.execute("SELECT fingerprint FROM runtime_events WHERE event_key = ?", (event_key,)).fetchone()
            if known is not None and known["fingerprint"] != fingerprint:
                raise ValueError("Host reused a runtime event identity with different inputs")
            if known is None:
                self._plan(connection, event_key, fingerprint, event, description, adapter)
            rows = connection.execute("SELECT * FROM runtime_decisions WHERE event_key = ? ORDER BY component", (event_key,))
            decisions = tuple(decision_from(row) for row in rows)
        for decision in decisions:
            if decision.status == "queued":
                self._deliver(decision.operation_key, adapter)
            elif decision.status in _IN_FLIGHT:
                self._reconcile(decision.operation_key, adapter)
        return self._event_decisions(event_key)

    def _plan(self, connection, event_key: str, fingerprint: str, event: RuntimeEvent, description: Mapping, adapter: Host) -> None:
        connection.execute("INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?)",
                           (event_key, self.project_id, event.host, event.session_id, fingerprint))
        boundary_ref = event.trigger_ref or {"event_id": event.event_id, "session_id": event.session_id,
                                             "boundary": event.boundary, "input_version": event.input_version}
        if event.boundary == "session_end":
            self.store.close_session(connection, event.host, event.session_id)
            return
        configured = self.store.session_bindings(connection, event.host, event.session_id)
        if configured is None:
            self._activate(connection, event.host, event.boundary, boundary_ref, description)
            configured = self.store.revisions(connection, event.host, effective=True)
            if description.get("reload_boundary") == "session_start" and event.boundary == "session_start":
                self.store.open_session(connection, event.host, event.session_id, configured)
        drafts = [self._draft(connection, event_key, event, binding, description, adapter)
                  for binding in configured if binding.enabled]
        conflicts = Counter(
            draft.decision.proposed_action["target"] for draft in drafts
            if draft.decision.mode == "apply" and draft.decision.status == "queued"
        )
        for draft in drafts:
            decision = draft.decision
            if decision.mode == "apply" and decision.status == "queued" and conflicts[decision.proposed_action["target"]] > 1:
                decision = replace(decision, stage="skipped", status="skipped", reason="exclusive_target_conflict",
                                   memory_before=None, memory_after=None)
                draft = replace(draft, decision=decision, state=None, originals=())
            self._persist(connection, event_key, event, draft)

    def _state(self, connection, event: RuntimeEvent, binding: Binding, lineage: str, namespace: str) -> dict:
        if not binding.state_version:
            return {}
        for current in dict.fromkeys((namespace, "live")):
            row = connection.execute(
                """SELECT value FROM policy_state WHERE project = ? AND host = ? AND component = ?
                AND lineage = ? AND namespace = ? AND state_version = ?""",
                (self.project_id, event.host, binding.component_id, lineage, current, binding.state_version),
            ).fetchone()
            if row is not None:
                return json.loads(row["value"])
        return {}

    def _draft(self, connection, event_key: str, event: RuntimeEvent, binding: Binding,
               description: Mapping, adapter: Host) -> _Draft:
        operation_key = "op-" + identity([event_key, binding.component_id, binding.binding_revision, binding.artifact_digest])
        lineage = event.lineage_id or event.thread_id or event.session_id
        state_scope = identity([event.lineage_id, event.thread_id]) if event.thread_id else lineage
        namespace = f"shadow:{binding.binding_revision}" if binding.mode == "shadow" else "live"
        decision = Decision(
            self.project_id, event.host, event.session_id, event.event_id, event.boundary,
            event.trigger_ref or {"event_id": event.event_id, "session_id": event.session_id},
            {"project_id": self.project_id, "host": event.host, "session_id": event.session_id, "lineage_id": lineage},
            binding.component_id, binding.version, binding.form, binding.artifact_digest, binding.binding_revision,
            binding.effective_boundary_ref, binding.mode, "decision-" + operation_key[3:], operation_key,
            "skipped", "skipped", "not_evaluated", run_id=event.run_id, thread_id=event.thread_id, lineage_id=lineage,
            options_digest=identity(binding.options),
        )

        def skipped(reason: str, **fields) -> _Draft:
            return _Draft(replace(decision, reason=reason, **fields), binding, namespace=namespace, lineage=state_scope)

        try:
            release = self.registry._resolve(connection, binding.component_id, binding.version, binding.form, verify=False)
            if release.digest != binding.artifact_digest:
                return skipped("binding_digest_mismatch")
            try:
                check_scope(release, event.host, binding.mode, description)
            except ValueError as error:
                return skipped(f"component_scope_changed: {error}")
            manifest = release.manifest
            trigger = manifest["trigger"]
            if event.boundary not in trigger["boundaries"] and "*" not in trigger["boundaries"]:
                return skipped("outside_trigger_boundary")
            if trigger.get("tools") and event.payload.get("tool") not in trigger["tools"]:
                return skipped("outside_trigger_tool")
            deliver_kind = "display_suggestion" if binding.mode == "suggest" else manifest["action"]["kind"]
            required = required_capabilities(manifest, binding.mode) - {action_capability(deliver_kind)}
            if required - capabilities_of(description):
                return skipped("host_capabilities_changed")
            can_deliver = supports_action(description, event.boundary, deliver_kind)
            budget = {**DEFAULT_BUDGET, **manifest["budget"]}
            if len(canonical(event.payload)) > budget["max_input_bytes"]:
                return skipped("input_budget_exceeded")
            started = monotonic()
            self.registry._verify(release)
            state = self._state(connection, event, binding, state_scope, namespace)
            context = PolicyContext(
                self.project, self.project_id, replace(event, payload=json.loads(canonical(event.payload))),
                release, json.loads(canonical(binding.options)), json.loads(canonical(state)), budget, operation_key,
                supports_action(description, event.boundary, "read_origin"),
            )
            # External execution and proposal evaluation have distinct interfaces.
            result = propose(context, adapter)
            elapsed_ms = (monotonic() - started) * 1000
            costs = {"decision_ms": round(elapsed_ms, 3), "source_reads": context.reads,
                     "original_bytes": sum(len(original.raw) for original in context.originals)}
            if elapsed_ms > budget["max_decision_ms"]:
                return skipped("decision_time_budget_exceeded", costs=costs)
            action = normalized_action(result.get("action"), context)
            next_state = state_update(result, context)
            if action is not None and binding.mode == "apply" and event.input_version is None:
                return skipped("input_version_unknown", costs=costs)
            if action is not None and binding.mode != "shadow" and not can_deliver:
                return skipped("action_boundary_capability_missing", costs=costs)
            if action is not None and not self._within_session_budget(connection, decision, budget):
                return skipped("session_action_budget_exceeded", costs=costs)
            reason = result.get("reason", "policy_proposed" if action is not None else "policy_skipped")
            if not isinstance(reason, str):
                return skipped("invalid_policy_reason", costs=costs)
            stage, status = self._stage(action, next_state, binding.mode)
            decision = replace(decision, reason=reason, stage=stage, status=status, proposed_action=action, costs=costs,
                               memory_before=identity(state) if next_state is not None else None,
                               memory_after=identity(next_state) if next_state is not None else None)
            return _Draft(decision, binding, next_state, tuple(context.originals), namespace, state_scope)
        except Exception as error:
            return skipped(f"policy_fallback:{type(error).__name__}")

    @staticmethod
    def _stage(action: dict | None, next_state: dict | None, mode: str) -> tuple[str, str]:
        if action is None and next_state is None:
            return "skipped", "skipped"
        if mode == "shadow":
            return "shadow", "simulated"
        if action is None:
            return "state_committed", "recorded"
        return "queued", "queued"

    @staticmethod
    def _within_session_budget(connection, decision: Decision, budget: dict) -> bool:
        maximum = budget.get("max_actions_per_session")
        if maximum is None:
            return True
        rows = connection.execute(
            """SELECT decision FROM runtime_decisions WHERE project = ? AND host = ?
            AND session = ? AND component = ? AND stage <> 'skipped'""",
            (decision.project_id, decision.host, decision.session_id, decision.component_id),
        )
        used = sum(1 for row in rows if (past := decision_from(row)).mode == decision.mode
                   and past.binding_revision == decision.binding_revision and past.proposed_action is not None)
        return used < maximum

    def _persist(self, connection, event_key: str, event: RuntimeEvent, draft: _Draft) -> None:
        decision, binding = draft.decision, draft.binding
        if draft.state is not None:
            serialized = json_text(draft.state)
            connection.execute(
                """INSERT INTO policy_state VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, host, component, lineage, namespace, state_version)
                DO UPDATE SET value = excluded.value""",
                (self.project_id, event.host, binding.component_id, draft.lineage,
                 draft.namespace, binding.state_version, serialized),
            )
            connection.execute(
                "INSERT INTO policy_state_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision.operation_key, self.project_id, event.host, binding.component_id, draft.lineage,
                 draft.namespace, binding.state_version, decision.memory_before, decision.memory_after, serialized),
            )
        if decision.status == "queued" and binding.mode == "apply":
            for original in draft.originals:
                connection.execute("INSERT OR IGNORE INTO tool_originals VALUES (?, ?, ?, ?, ?, ?)",
                                   (self.project_id, original.handle, original.raw, original.digest,
                                    json_text(original.source_ref), self._expiry(binding.options)))
        save_decision(connection, event_key, decision)
        if decision.status == "queued":
            reference(connection, decision.operation_key, binding,
                      {"kind": "operation", "project_id": self.project_id, "host": event.host,
                       "session_id": event.session_id, "operation_key": decision.operation_key})

    @staticmethod
    def _expiry(options: dict) -> float | None:
        seconds = options.get("original_retention_seconds")
        if seconds is None:
            return None
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("original_retention_seconds must be a positive integer")
        return time() + seconds

    def _operation(self, connection, key: str):
        row = connection.execute("SELECT * FROM runtime_decisions WHERE project = ? AND operation = ?",
                                 (self.project_id, key)).fetchone()
        if row is None:
            raise ValueError("Unknown operation within this project")
        return row, decision_from(row)

    def _claim(self, key: str) -> Decision | None:
        with self.store.db.transaction(write=True) as connection:
            row, decision = self._operation(connection, key)
            if decision.status != "queued":
                return None
            pending = replace(decision, stage="submitted", status="pending")
            save_decision(connection, row["event_key"], pending)
        return pending

    def _deliver(self, key: str, adapter: Host) -> None:
        decision = self._claim(key)
        if decision is None:
            return
        action = decision.proposed_action
        if decision.mode == "suggest":
            proposed = action["payload"]
            if action["kind"] == "replace_tool_result":
                preview = str(proposed.get("text", "")).partition("\n")[2]
                detail = f"Keep original lines {proposed.get('start_line')}-{proposed.get('end_line')}.\n{preview}"
            else:
                detail = str(proposed.get("text", decision.reason))
            action = {
                **action, "kind": "display_suggestion",
                "payload": {"text": f"Skill RSI suggestion ({decision.component_id})\n{detail}",
                            "reason": decision.reason, "suggestion": decision.proposed_action},
            }
        try:
            receipt = _receipt_data(adapter.deliver(action, key))
        except Exception as error:
            receipt = {"stage": "submitted", "status": "unknown", "missing_reason": type(error).__name__}
        self.record_receipt(key, receipt)

    def _reconcile(self, key: str, adapter: Host) -> None:
        lookup = getattr(adapter, "reconcile", None)
        if callable(lookup):
            with self.store.db.transaction() as connection:
                effect = connection.execute("SELECT operation FROM suggestion_effects WHERE parent = ?", (key,)).fetchone()
            if effect is not None:
                key = effect["operation"]
            try:
                receipt = lookup(key)
            except (OSError, TimeoutError, ValueError):
                return
            if receipt is not None:
                self.record_receipt(key, receipt)

    def record_receipt(self, key: str, receipt: Mapping) -> Decision:
        """Append host evidence. A lost response never authorizes a second delivery."""
        record = _receipt_data(receipt)
        if record.get("operation_key", key) != key:
            raise ValueError("Host receipt operation key mismatch")
        with self.store.db.transaction(write=True) as connection:
            effect = connection.execute("SELECT parent FROM suggestion_effects WHERE operation = ?", (key,)).fetchone()
            if effect is not None:
                record.setdefault("operation_key", key)
                if record["stage"] == "submitted":
                    record["stage"] = "adoption_submitted"
                key = effect["parent"]
            row, decision = self._operation(connection, key)
            return self._append_receipt(connection, row, decision, record)

    @staticmethod
    def _append_receipt(connection, row, decision: Decision, record: dict) -> Decision:
        if decision.proposed_action is None or _STAGE_ORDER[decision.stage] < _STAGE_ORDER["submitted"]:
            raise ValueError("Receipt requires a submitted host action")
        if record in decision.receipts:
            return decision
        stage = record["stage"]
        apply_update = _STAGE_ORDER[stage] > _STAGE_ORDER[decision.stage] or (
            stage == decision.stage and decision.status in _IN_FLIGHT
        )
        update = {"stage": stage, "status": record["status"], "receipt": record} if apply_update else {}
        value = replace(decision, receipts=(*decision.receipts, record), **update)
        save_decision(connection, row["event_key"], value)
        if value.status not in _IN_FLIGHT:
            forget_reference(connection, decision.operation_key)
        return value

    def accept_suggestion(self, key: str, *, receipt_ref: Any, accepted: bool = True) -> Decision:
        if receipt_ref is None:
            raise ValueError("Adoption requires a user decision receipt reference")
        record = {"stage": "adopted", "status": "adopted" if accepted else "declined", "receipt_ref": receipt_ref}
        with self.store.db.transaction(write=True) as connection:
            row, decision = self._operation(connection, key)
            if decision.mode != "suggest" or not any(
                item["stage"] == "displayed" and item["status"] in {"displayed", "shown", "success"}
                for item in decision.receipts
            ):
                raise ValueError("Only an observed, displayed suggestion can receive an adoption receipt")
            prior = next((item for item in decision.receipts if item["stage"] == "adopted"), None)
            if prior is not None and prior["status"] != record["status"]:
                raise ValueError("An adoption outcome is already recorded for this suggestion")
            return self._append_receipt(connection, row, decision, record)

    def apply_suggestion(self, key: str, event: RuntimeEvent, adapter: Host) -> Decision:
        """Deliver a previously adopted suggestion against the same live input version."""
        description = adapter.capabilities({"project": str(self.project), "project_id": self.project_id,
                                            "session_id": event.session_id, "boundary": event.boundary})
        effect_key = "op-" + identity(["adopted_effect", key])
        with self.store.db.transaction(write=True) as connection:
            row, decision = self._operation(connection, key)
            if connection.execute("SELECT 1 FROM suggestion_effects WHERE operation = ?", (effect_key,)).fetchone():
                return decision
            if decision.mode != "suggest" or decision.stage != "adopted" or decision.status != "adopted":
                raise ValueError("Only an explicitly adopted suggestion can be executed")
            action = decision.proposed_action
            target = event.payload.get("target")
            if (event.host, event.session_id, event.boundary, event.event_id) != (
                decision.host, decision.session_id, decision.boundary, decision.event_id
            ) or not event.input_version or event.input_version != action["expected_input_version"] or (
                target and target != action["target"] and action["kind"] != "append_context"
            ):
                raise ValueError("Suggestion no longer matches the live host input")
            original_event = connection.execute("SELECT fingerprint FROM runtime_events WHERE event_key = ?",
                                                (row["event_key"],)).fetchone()
            if original_event["fingerprint"] != identity(asdict(event)):
                raise ValueError("Suggestion's visible input or source references changed")
            head = self.store.head(connection, decision.host, decision.component_id)
            if head is None or head["desired"] != decision.binding_revision or head["effective"] != decision.binding_revision:
                raise ValueError("Suggestion belongs to an earlier binding revision")
            binding = self.store.binding(connection, decision.host, decision.component_id, decision.binding_revision)
            release = self.registry._resolve(connection, binding.component_id, binding.version, binding.form)
            if release.digest != binding.artifact_digest or not supports_action(description, event.boundary, action["kind"]):
                raise ValueError("Suggestion action is no longer supported at the current boundary")
            self._check_conflicts(connection, release, decision.host, "apply")
            required = required_capabilities(release.manifest, "apply")
            if "apply" not in release.manifest["scope"]["modes"] or required - capabilities_of(description):
                raise ValueError("Host cannot execute the adopted action under its manifest")
            sibling_rows = connection.execute("SELECT * FROM runtime_decisions WHERE event_key = ?", (row["event_key"],))
            for sibling in map(decision_from, sibling_rows):
                if (sibling.mode == "apply" or sibling.stage in {"adoption_submitted", "delivered"}) and (
                    sibling.proposed_action or {}
                ).get("target") == action["target"]:
                    raise ValueError("An intervention already reserved this write target")
            if action["kind"] == "replace_tool_result":
                self._restore_adoption_original(connection, event, binding, action)
            self._check_adoption_source(event, description, action, release, binding, key)
            connection.execute("INSERT INTO suggestion_effects VALUES (?, ?)", (effect_key, key))
            self._append_receipt(connection, row, decision, {"stage": "adoption_submitted", "status": "pending",
                                                            "operation_key": effect_key})
            reference(connection, key, binding, {"kind": "adopted_operation", "project_id": self.project_id,
                                                 "host": event.host, "operation_key": effect_key})
        try:
            receipt = _receipt_data(adapter.deliver(action, effect_key))
        except Exception as error:
            receipt = {"stage": "submitted", "status": "unknown", "missing_reason": type(error).__name__}
        return self.record_receipt(effect_key, receipt)

    def _check_adoption_source(self, event, description, action, release, binding, key) -> None:
        source = action["payload"].get("source_ref")
        resource = event.payload.get("resource")
        if not isinstance(source, dict) or not isinstance(resource, dict):
            return
        path = source.get("path")
        if not isinstance(path, str) or path != resource.get("source_path") or not source.get("sha256"):
            return
        context = PolicyContext(
            self.project, self.project_id, event, release, binding.options, {},
            {**DEFAULT_BUDGET, **release.manifest["budget"]}, key,
            supports_action(description, event.boundary, "read_origin"),
        )
        _, current = context.read_origin(path)
        if current["sha256"] != source["sha256"]:
            raise ValueError("Suggestion source changed after it was displayed")

    def _restore_adoption_original(self, connection, event: RuntimeEvent, binding: Binding, action: dict) -> None:
        payload = action["payload"]
        text = event.payload.get("text")
        if not isinstance(text, str) or not isinstance(payload.get("handle"), str):
            raise ValueError("Adopted projection requires the original tool result")
        original = text.encode("utf-8")
        checksum = sha256(original)
        if checksum != payload.get("original_digest"):
            raise ValueError("Adopted projection's original digest changed")
        connection.execute("INSERT OR IGNORE INTO tool_originals VALUES (?, ?, ?, ?, ?, ?)",
                           (self.project_id, payload["handle"], original, checksum,
                            json_text(event.payload.get("source_ref") or event.trigger_ref), self._expiry(binding.options)))

    def _event_decisions(self, event_key: str) -> tuple[Decision, ...]:
        with self.store.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM runtime_decisions WHERE event_key = ? ORDER BY component", (event_key,))
            return tuple(decision_from(row) for row in rows)

    def decisions(self, host: str | None = None, *, session_id: str | None = None) -> tuple[Decision, ...]:
        sql = "SELECT * FROM runtime_decisions WHERE project = ?"
        arguments = [self.project_id]
        if host is not None:
            sql += " AND host = ?"
            arguments.append(host)
        if session_id is not None:
            sql += " AND session = ?"
            arguments.append(session_id)
        with self.store.db.transaction() as connection:
            rows = connection.execute(sql + " ORDER BY rowid", arguments)
            return tuple(decision_from(row) for row in rows)

    def expand(self, handle: str, start_line: int | None = None, end_line: int | None = None) -> dict:
        with self.store.db.transaction() as connection:
            row = connection.execute(
                """SELECT content, digest, source_ref FROM tool_originals WHERE project = ? AND handle = ?
                AND (expires IS NULL OR expires > ?)""", (self.project_id, handle, time()),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown or expired original within this project")
        if sha256(row["content"]) != row["digest"]:
            raise ValueError("Saved original digest mismatch")
        text = row["content"].decode("utf-8")
        lines = text.split("\n")
        start = 1 if start_line is None else start_line
        end = len(lines) if end_line is None else end_line
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError("Line range must be in the original tool result")
        return {"text": "\n".join(lines[start - 1:end]), "start_line": start, "end_line": end,
                "total_lines": len(lines), "source_ref": json.loads(row["source_ref"]), "artifact_digest": row["digest"]}
