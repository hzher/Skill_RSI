"""Check the narrow versioned field explicitly needed by the next host action."""

import json


def _matches(record, resource):
    return (isinstance(record, dict) and record.get("object_id") == resource["object_id"]
            and record.get("field") == resource["field"])


def _verified(record, resource):
    return (_matches(record, resource) and record.get("verified") is True
            and isinstance(record.get("source_digest"), str) and bool(record["source_digest"])
            and bool(record.get("source_ref")) and "value" in record)


def _pointer(value, pointer):
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Expected a JSON Pointer beginning with /")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not part.isdecimal() or (part != "0" and part.startswith("0")):
                raise ValueError("Invalid JSON Pointer array index")
            value = value[int(part)]
        else:
            value = value[part]
    return value


def _fact(context, resource, event):
    current = event.get("current")
    if _verified(current, resource):
        return current
    if not resource.get("source_path"):
        return None
    raw, ref = context.read_origin(resource["source_path"])
    pointer = resource.get("pointer")
    value = _pointer(json.loads(raw), pointer) if pointer is not None else raw.decode("utf-8")
    if pointer is not None:
        ref = {**ref, "locator": {"json_pointer": pointer}}
    return {
        "object_id": resource["object_id"], "field": resource["field"],
        "source_digest": ref["sha256"], "source_ref": ref,
        "verified": True, "value": value,
    }


def _identity(resource):
    return json.dumps([resource["object_id"], resource["field"]], ensure_ascii=False, separators=(",", ":"))


def _known_scope(value):
    return isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key]
                                           for key in ("object_id", "field"))


def _remember(context, resource, fact, result):
    identity = _identity(resource)
    previous = context.state.get(identity)
    if fact == previous:
        return result
    return {**result, "state": {**context.state, identity: fact}}


def _proposal(event, resource, fact, reason):
    target = event.get("target")
    if not isinstance(target, str) or not target:
        return {"reason": "missing_decision_target"}
    field = f"{resource['object_id']}.{resource['field']}"
    if fact is None:
        text = f"Check {field} against the current source before using this constraint. Its revision is unverified."
    else:
        value = json.dumps(fact["value"], ensure_ascii=False, sort_keys=True)
        text = f"Verified constraint for {field}: {value} (source revision {fact['source_digest']})."
    return {"reason": reason, "action": {"kind": "append_context", "target": target, "payload": {
        "text": text, "object_id": resource["object_id"], "field": resource["field"],
        "reason": reason, "source_digest": fact["source_digest"] if fact else None,
        "source_ref": fact["source_ref"] if fact else None,
    }}}


def propose(context):
    event = context.event.payload
    if context.event.boundary in {"tool_result", "mcp_tool_result"}:
        observed = event.get("constraint_observation")
        if not _known_scope(observed) or not _verified(observed, observed):
            return {"reason": "no_verified_constraint_observation"}
        if event.get("success") is False:
            return {"reason": "failed_constraint_source"}
        identity = _identity(observed)
        old = context.state.get(identity)
        if old == observed:
            return {"reason": "constraint_already_recorded"}
        if old and old["source_digest"] == observed["source_digest"] and old["value"] != observed["value"]:
            return {"reason": "conflicting_fact_for_same_revision"}
        return {"reason": "verified_constraint_observed", "state": {**context.state, identity: observed}}

    resource = event.get("resource")
    if event.get("needs_constraint") is False or not _known_scope(resource):
        return {"reason": "no_explicit_constraint_dependency"}
    previous = event.get("previous") or context.state.get(_identity(resource))
    if not _verified(previous, resource):
        return {"reason": "no_prior_verified_constraint_in_scope"}
    try:
        current = _fact(context, resource, event)
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError):
        current = None
    if current is None:
        return _proposal(event, resource, None, "source_unverified")
    visible = event.get("visible")
    if _matches(visible, resource) and visible.get("source_digest") == current["source_digest"]:
        return _remember(context, resource, current, {"reason": "current_constraint_already_visible"})
    if current["source_digest"] == previous["source_digest"]:
        if current["value"] != previous["value"]:
            return _proposal(event, resource, None, "conflicting_fact_for_same_revision")
        return _remember(context, resource, current, _proposal(event, resource, current, "recall_verified_constraint"))
    if current["value"] == previous["value"] and _matches(visible, resource) and (
        visible.get("source_digest") == previous["source_digest"]
    ):
        return _remember(context, resource, current, {"reason": "unrelated_source_change"})
    return _remember(context, resource, current, _proposal(event, resource, current, "source_changed"))
