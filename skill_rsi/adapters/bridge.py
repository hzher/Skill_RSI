"""Synchronous native CLI hook and project-scoped MCP launcher."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from ..evidence.archive import write
from ..evidence.io import at_pointer, digest, inside
from ..evidence.schema import encode, identifier
from .base import host_record, read_optional
from .native_events import mapping
from .registry import get_adapter

_LIMIT = 2 * 1024 * 1024
_CLAUDE = {
    "SessionStart": "session_start", "PreCompact": "compaction", "PreToolUse": "before_action",
    "PostToolUse": "tool_result", "PostToolUseFailure": "tool_result", "Stop": "turn_end",
}
_CODEX = {
    "SessionStart": "session_start", "UserPromptSubmit": "prompt_submit", "PreToolUse": "before_action",
    "PostToolUse": "tool_result", "Stop": "turn_end",
}


class EventAdapter:
    def __init__(self, host, handshake, payload, input_version, translate):
        self.host = host
        self.handshake = handshake
        self.payload = payload
        self.input_version = input_version
        self.translate = translate

    def capabilities(self, session=None):
        return self.host.capabilities(self.handshake)

    def propose_native(self, context, engine):
        if engine != "wasm":
            raise ValueError(f"The current host bridge has no proposal executor for {engine}")
        from .wasm import decide

        return decide(context)

    def deliver(self, action, key):
        if action.get("expected_input_version") not in (None, self.input_version):
            return {"status": "skipped", "stage": "submission", "missing_reason": "input_version_changed"}
        if action.get("kind") in {"replace_tool_result", "update_tool_input"} and self.payload.get("target") and action.get("target") != self.payload["target"]:
            return {"status": "skipped", "stage": "submission", "missing_reason": "write_target_changed"}
        output = self.translate(action["kind"], mapping(action.get("payload")))
        if output is not None and (self.host.name == "dsh" or action.get("boundary", "").startswith("mcp_")):
            output = {**output, "operation_key": key}
        if output is None:
            return {"status": "skipped", "stage": "submission", "missing_reason": "host_output_shape_unavailable"}
        return {"status": "submitted", "stage": "submission", "operation_key": key,
                "host_output": output, "delivery_scope": "local_bridge_response", "model_input_visibility": "unknown"}


def _text_shape(value):
    """Bind a projection to a single text-valued native result without changing its type."""
    if isinstance(value, str):
        return value, lambda text: text
    if isinstance(value, dict):
        for key in ("output", "stdout", "text", "content"):
            if isinstance(value.get(key), str):
                return value[key], lambda text, key=key: {**value, key: text}
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        block = value[0]
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"], lambda text: [{**block, "text": text}]
    return None, None


def _checkpoint(project, directory, registration, session, raw, boundary):
    path = directory / "checkpoints" / (identifier(session) + ".json")
    if boundary == "compaction":
        facts = []
        for source in registration.get("checkpoint_paths", []):
            try:
                origin = inside(project, source)
                if origin.is_file() and origin.stat().st_size <= _LIMIT:
                    contents = origin.read_bytes()
                    facts.append({"path": source, "bytes": len(contents), "sha256": digest(contents)})
            except (OSError, ValueError):
                continue
        result = {"session_id": session, "checkpoint_id": identifier(session, facts, raw.get("trigger")),
                  "source": raw.get("trigger"), "facts": facts}
        write(path, result)
        return result
    if boundary == "session_start" and raw.get("source") == "compact":
        return read_optional(path)
    return None


def _approved_resource(project, registration, source, *, prospective=False):
    for resource in registration.get("constraints", []):
        if not isinstance(resource, dict):
            continue
        location = resource.get("source_path")
        if not isinstance(location, str):
            continue
        try:
            approved = inside(project, location)
            if source is not None and Path(source).is_absolute():
                selected = Path(source)
            elif source is not None:
                selected = project / source
            else:
                selected = None
            targets = [inside(project, path) for path in resource.get("target_paths", [])] if prospective else []
            if selected != approved and selected not in targets:
                continue
        except (ValueError, OSError):
            continue
        return resource
    return None


def _constraint(project, resource):
    identity = {"object_id": resource.get("object_id") or resource["source_path"], "field": resource.get("field")}
    try:
        path = inside(project, resource["source_path"])
        if not path.is_file() or path.stat().st_size > _LIMIT:
            raise ValueError("source exceeds configured scope")
        content = path.read_bytes()
        value = at_pointer(json.loads(content), resource["pointer"]) if resource.get("pointer") is not None else content.decode("utf-8")
        return {**identity, "verified": True, "value": value, "source_digest": digest(content),
                "source_ref": {"path": resource["source_path"], "sha256": digest(content),
                               "pointer": resource.get("pointer")}}
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        return {**identity, "verified": False, "missing_reason": "source_revision_unavailable"}


def _hook_event(host, raw, project, registration):
    event_name = raw.get("hook_event_name")
    session = raw.get("session_id")
    turn = raw.get("turn_id")
    target = raw.get("tool_use_id") or raw.get("tool_call_id")
    tool = raw.get("tool_name")
    shape = None
    if host == "dsh":
        boundary = raw.get("boundary")
        session = raw.get("session_id")
        turn = raw.get("turn_id")
        target = raw.get("call_id") or raw.get("target")
        tool = raw.get("tool_name")
    else:
        boundaries = _CLAUDE if host == "claude_code" else _CODEX
        boundary = boundaries.get(event_name)
    if not isinstance(session, str) or not session or not isinstance(boundary, str):
        return None
    event_payload = {"target": target, "tool": tool, "call_id": target}
    direct = mapping(raw.get("payload"))
    tool_input = mapping(raw.get("tool_input") or direct.get("tool_input"))
    source = tool_input.get("file_path") or tool_input.get("path")
    resource = _approved_resource(project, registration, source if isinstance(source, str) else None,
                                  prospective=boundary in {"before_action", "before_use"})
    if resource and boundary in {"before_action", "before_use"}:
        event_payload["resource"] = resource
    if boundary == "tool_result":
        value = raw.get("tool_response") if host != "dsh" else direct.get("text")
        text, shape = _text_shape(value)
        success = event_name != "PostToolUseFailure" and direct.get("success") is not False
        event_payload.update(text=text, success=success)
        if resource and success and str(tool).lower() in {"read", "read_file"}:
            observation = _constraint(project, resource)
            # Treat current local bytes as a verified source observation only when the callback
            # renders those same bytes. External commands and partial reads remain unknown.
            if observation.get("verified") and text is not None and digest(text.encode()) == observation["source_digest"]:
                event_payload["constraint_observation"] = observation
    native = raw.get("event_id") or target or raw.get("message_id") or turn
    key = identifier(host, session, boundary, native, digest(encode(raw)))
    version = raw.get("input_version") or digest(encode(tool_input if boundary in {"before_action", "before_use"} else event_payload))
    return {"session": session, "boundary": boundary, "native": native, "turn": turn, "target": target,
            "key": key, "input_version": version, "event_payload": event_payload, "shape": shape}


def _handshake(host, registration, event, raw):
    nodes = {}
    boundary = event["boundary"]
    if boundary in host.nodes:
        nodes[boundary] = []
    if host.name == "dsh":
        for name, caps in mapping(raw.get("supported_nodes")).items():
            if name in host.nodes and isinstance(caps, list) and all(isinstance(cap, str) for cap in caps):
                nodes[name] = caps
    elif host.name == "claude_code":
        if boundary in {"session_start", "before_action"}:
            nodes[boundary] = ["read_origin", "append_context", "suggest"]
        if boundary == "tool_result" and event["shape"] and event["target"] and event["event_payload"].get("success"):
            nodes[boundary] = ["read_origin", "replace_tool_result", "retrieve_original", "suggest"]
    elif host.name == "codex" and boundary == "before_action":
        nodes[boundary] = ["read_origin", "append_context", "suggest"]
    return {"registration_id": registration["registration_id"],
            "host_version": registration.get("native_host_version"), "nodes": nodes}


def _translate(host, raw, event, kind, payload):
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    if host == "dsh":
        if kind in {"replace_tool_result", "append_context"}:
            return {"kind": kind, "text": text, "target": event["target"]}
        return None
    event_name = raw.get("hook_event_name")
    if kind == "display_suggestion":
        return {"systemMessage": text}
    if kind == "append_context" and event["boundary"] in {"before_action", "session_start"}:
        return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}
    if kind == "replace_tool_result" and host == "claude_code" and event["shape"] and event["event_payload"].get("success"):
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": event["shape"](text)}}
    return None


def _mark_loaded(directory, handshake, boundary_ref):
    # Keep independent node observations. Concurrent hooks publish one file per node,
    # so a session-start callback cannot erase a previously exercised tool boundary.
    for name, capabilities in handshake["nodes"].items():
        write(directory / "nodes" / (identifier(name) + ".json"), {
            **handshake, "nodes": {name: capabilities}, "last_event": boundary_ref
        })


def _merge(output, extra):
    specific = {**mapping(output.get("hookSpecificOutput")), **mapping(extra.get("hookSpecificOutput"))}
    if output.get("systemMessage") and extra.get("systemMessage"):
        extra = {**extra, "systemMessage": output["systemMessage"] + "\n" + extra["systemMessage"]}
    if mapping(output.get("hookSpecificOutput")).get("additionalContext") and mapping(extra.get("hookSpecificOutput")).get("additionalContext"):
        specific["additionalContext"] = output["hookSpecificOutput"]["additionalContext"] + "\n" + extra["hookSpecificOutput"]["additionalContext"]
    merged = {**output, **extra}
    if specific:
        merged["hookSpecificOutput"] = specific
    return merged


def _dispatch(host, project, state_dir, registration, directory, raw, event, handshake, translate):
    from ..runtime import Runtime, RuntimeEvent

    key, session, boundary = event["key"], event["session"], event["boundary"]
    run_id = identifier(host.name, str(project), session)
    ref = {"host": host.name, "boundary": boundary, "session_id": session, "event_id": key,
           "native_event_id": event["native"], "native_turn_id": event["turn"]}
    runtime = Runtime(project, state_dir=state_dir)
    _mark_loaded(directory, handshake, ref)
    if registration.get("observe"):
        _journal(directory, {
            "project_id": runtime.project_id, "run_id": run_id,
            "source_id": f"{host.name}:cli_bridge", "type": "session_resume" if boundary == "session_start" and
            raw.get("source") == "resume" else boundary, "event_id": key,
            "native_session_id": session, "thread_id": session if host.name == "codex" and not boundary.startswith("mcp_") else None,
            "turn_id": event["turn"], "tool_call_id": event["target"], "native_record": raw,
            "visibility": {"host_callback": "observed", "model_input": "unknown"},
        })
    adapted = EventAdapter(host, handshake, event["event_payload"], event["input_version"], translate)
    decisions = runtime.dispatch(RuntimeEvent(
        host=host.name, session_id=session, event_id=key, boundary=boundary,
        input_version=event["input_version"], payload=event["event_payload"], run_id=run_id,
        thread_id=session if host.name == "codex" and not boundary.startswith("mcp_") else None, trigger_ref=ref,
    ), adapted)
    output = {}
    serialized = [decision.to_dict() for decision in decisions]
    for decision in serialized:
        if registration.get("observe"):
            _journal(directory, {
                "project_id": runtime.project_id, "run_id": run_id,
                "source_id": f"{host.name}:component_runtime", "type": "component_decision",
                "event_id": decision["decision_id"], "native_session_id": session,
                "decision": decision, "visibility": {"model_input": "unknown"},
            })
        receipt = next((item for item in reversed(decision.get("receipts", ())) if item.get("host_output")),
                       mapping(decision.get("receipt")))
        extra = mapping(receipt.get("host_output"))
        output = _merge(output, extra) if host.name != "dsh" else {
            "actions": [*output.get("actions", []), extra]
        } if extra else output
    write(directory / "observations" / (key + ".json"), {
        **ref, "run_id": run_id, "decisions": serialized, "native_response_prepared": bool(output),
        "delivery_status": "unknown", "protocol": "skill-rsi.host.cli.v1",
    })
    return output


def _journal(directory, record):
    try:
        from ..evidence.journal import EventJournal

        EventJournal(directory / "journal").append(record)
    except Exception as error:
        try:
            write(directory / "last_capture_error.json", {"reason": type(error).__name__,
                                                         "event_id": record.get("event_id")})
        except OSError:
            pass


def _confirmed(runtime, directory, payload):
    operation = payload.get("operation_key")
    if not isinstance(operation, str) or not operation:
        return
    session = payload.get("session_id")
    layer = payload.get("layer")
    ref = {"host": payload.get("host"), "session_id": session, "layer": layer,
           "operation_key": operation, "request_ref": payload.get("request_ref"),
           "tool_call_id": payload.get("tool_call_id"), "previous": payload.get("previous"),
           "projected": payload.get("projected"), "effective_scope": payload.get("effective_scope")}
    decision = runtime.record_receipt(operation, {"stage": "delivered", "status": "applied", "receipt_ref": ref,
                                                  "model_input_visibility": "unknown"})
    receipt_id = identifier("delivery", operation, layer)
    write(directory / "observations" / (receipt_id + ".json"), {
        **ref, "delivery_status": "confirmed_at_local_boundary", "model_input_visibility": "unknown",
    })
    registration = read_optional(directory / "registration.json") or {}
    if registration.get("observe") and decision is not None:
        body = decision.to_dict()
        _journal(directory, {
            "project_id": runtime.project_id, "run_id": body.get("run_id") or identifier(registration["project"], session),
            "source_id": f"{payload.get('host')}:component_runtime", "type": "component_receipt",
            "event_id": receipt_id, "native_session_id": session, "decision": body,
            "receipt_ref": ref, "visibility": {"model_input": "unknown"},
        })


def run_hook(host, project, state_dir, registration, directory, raw):
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).resolve().is_relative_to(project):
        return {}
    event = _hook_event(host.name, raw, project, registration)
    if event is None:
        return {}
    handshake = _handshake(host, registration, event, raw)
    if host.name == "claude_code" and registration.get("checkpoint_paths") and event["boundary"] in {"compaction", "session_start"}:
        checkpoint = _checkpoint(project, directory, registration, event["session"], raw, event["boundary"])
        if checkpoint:
            event["event_payload"]["checkpoint"] = checkpoint
            raw = {**raw, "skill_rsi_checkpoint": checkpoint,
                   "checkpoint_observation_scope": "same_session_native_compaction_hooks"}
    return _dispatch(host, project, state_dir, registration, directory, raw, event, handshake,
                     lambda kind, payload: _translate(host.name, raw, event, kind, payload))


_MCP_TOOLS = [
    {"name": "skill_rsi_expand", "description": "Read the original text behind a Skill RSI view handle. Line numbers are inclusive.",
     "inputSchema": {"type": "object", "properties": {"handle": {"type": "string"},
                                                      "start_line": {"type": "integer", "minimum": 1},
                                                      "end_line": {"type": "integer", "minimum": 1}},
                     "required": ["handle"], "additionalProperties": False}},
    {"name": "skill_rsi_read", "description": "Read a text file explicitly exposed through this project's Skill RSI MCP bridge.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"], "additionalProperties": False}},
]


def run_mcp(host, project, state_dir, registration, directory):
    from ..runtime import Runtime

    connection = "mcp-" + uuid4().hex
    base_ref = {"host": host.name, "boundary": "mcp", "session_id": connection}
    tools = {tool["name"]: tool for tool in _MCP_TOOLS}
    supported = {"mcp": ["retrieve_original"]}
    if registration.get("mcp_paths"):
        supported.update(mcp_tool_result=["read_origin", "replace_tool_result", "retrieve_original"],
                         mcp_before_action=["read_origin", "append_context"])
    handshake = {"registration_id": registration["registration_id"],
                 "host_version": registration.get("native_host_version"), "nodes": supported}
    runtime = Runtime(project, state_dir=state_dir)

    def call(name, args, key):
        if name == "skill_rsi_expand":
            content = runtime.expand(args["handle"], start_line=args.get("start_line"), end_line=args.get("end_line"))
            text = content["text"]
            write(directory / "observations" / (key + ".json"), {
                **base_ref, "event_id": key, "kind": "expanded", "range": {k: content.get(k) for k in ("start_line", "end_line")},
                "handle": args["handle"], "delivery_status": "returned_to_mcp", "model_input_visibility": "unknown",
            })
            return text, []
        if name != "skill_rsi_read" or args.get("path") not in registration.get("mcp_paths", []):
            raise ValueError("Unknown tool or project source")
        source = args["path"]
        file = inside(project, source)
        if not file.is_file() or file.stat().st_size > _LIMIT:
            raise ValueError("Project source is unavailable")
        result_event = {
            "key": key, "session": connection, "boundary": "mcp_tool_result", "native": key,
            "turn": None, "target": key, "input_version": None, "event_payload": {"target": key, "call_id": key},
        }
        resource = _approved_resource(project, registration, source)
        raw = {"tool": name, "path": source}
        note = {}
        if resource:
            before = {**result_event, "key": identifier(key, "before"), "boundary": "mcp_before_action",
                      "input_version": digest(encode({"path": source})), "event_payload": {
                          **result_event["event_payload"], "resource": resource}}
            note = _dispatch(host, project, state_dir, registration, directory, raw, before, handshake,
                             lambda kind, payload: {"text": payload["text"]} if kind == "append_context" and isinstance(payload.get("text"), str) else None)
        content = file.read_bytes()
        text = content.decode("utf-8")
        checksum = digest(content)
        result_event["input_version"] = checksum
        result_event["event_payload"].update(text=text, success=True, source_ref={"path": source, "sha256": checksum, "bytes": len(content)})
        if resource:
            observation = _constraint(project, resource)
            if observation.get("source_digest") == checksum:
                result_event["event_payload"]["constraint_observation"] = observation
        output = _dispatch(host, project, state_dir, registration, directory,
                           {**raw, "sha256": checksum}, result_event, handshake,
                           lambda kind, payload: {"text": payload["text"]} if kind == "replace_tool_result" and isinstance(payload.get("text"), str) else None)
        edits = [value["operation_key"] for value in (note, output) if value.get("operation_key")]
        return "\n\n".join(filter(None, [note.get("text"), output.get("text", text)])), edits

    for line in sys.stdin.buffer:
        request_id = None
        operations = []
        try:
            if len(line) > _LIMIT:
                raise ValueError("MCP request exceeds size limit")
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("jsonrpc") != "2.0":
                raise ValueError("Invalid MCP request")
            if "id" not in row:
                continue
            request_id = row["id"]
            method, params = row.get("method"), mapping(row.get("params"))
            if method == "initialize":
                _mark_loaded(directory, handshake, base_ref)
                received = params.get("protocolVersion")
                version = received if received in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-03-26"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "skill-rsi", "version": host.version}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(tools.values())}
            elif method == "tools/call":
                try:
                    text, operations = call(params.get("name"), mapping(params.get("arguments")), identifier(connection, request_id))
                    result = {"content": [{"type": "text", "text": text}]}
                except (KeyError, IndexError, OSError, TypeError, ValueError) as error:
                    result = {"isError": True, "content": [{"type": "text", "text": f"Skill RSI source is unavailable ({type(error).__name__})"}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False), flush=True)
            for operation in operations:
                try:
                    _confirmed(runtime, directory, {"host": host.name, "session_id": connection,
                                                    "operation_key": operation, "layer": "mcp_stdio"})
                except (OSError, RuntimeError, ValueError):
                    continue
        except (ValueError, UnicodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("hook", "mcp", "expand", "receipt"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    project, state_dir = args.project.resolve(), args.state_dir.resolve()
    host = get_adapter(args.host)
    record = host_record(project, state_dir, host.name)
    registration = read_optional(record)
    if not registration or registration["registration_id"] != args.owner:
        if args.mode == "hook":
            print("{}")
        return
    if args.mode == "mcp":
        run_mcp(host, project, state_dir, registration, record.parent)
        return
    try:
        data = sys.stdin.buffer.read(_LIMIT + 1)
        row = json.loads(data) if len(data) <= _LIMIT else {}
        if args.mode in {"expand", "receipt"}:
            from ..runtime import Runtime

            runtime = Runtime(project, state_dir=state_dir)
            if args.mode == "expand":
                output = runtime.expand(row["handle"], start_line=row.get("start_line"), end_line=row.get("end_line"))
            else:
                for event in row.get("receipts", []):
                    _confirmed(runtime, record.parent, {**event, "host": host.name, "layer": "dsh_sdk_options"})
                output = {}
        else:
            output = run_hook(host, project, state_dir, registration, record.parent, row)
    except Exception as error:
        print(f"Skill RSI hook skipped ({type(error).__name__})", file=sys.stderr)
        output = {}
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
