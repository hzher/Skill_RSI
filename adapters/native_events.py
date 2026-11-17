"""Classify native CLI and session events without interpreting endpoint payloads."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


EventPart = tuple[str, str, dict[str, Any]]

_ACTORS = {"assistant": "agent", "user": "harness", "system": "harness"}
_CODEX_TOOLS = {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}
_CODEX_CALLS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call", "mcp_call"}
_PRIVATE_BLOCKS = {"thinking", "redacted_thinking", "reasoning", "thinking_delta", "signature_delta"}


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def records(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def native_session(row: dict, harness: str) -> str | None:
    if harness == "claude_code":
        value = row.get("session_id") or row.get("sessionId")
    elif row.get("type") == "session_meta":
        value = mapping(row.get("payload")).get("id")
    else:
        value = row.get("thread_id")
    return value if isinstance(value, str) and value else None


def _claude_block(block: dict, role: str, pointer: str) -> EventPart:
    kind = block.get("type")
    actor = _ACTORS.get(role)
    if kind in {"tool_use", "server_tool_use"}:
        return "tool_call", pointer, {"actor_type": actor, "tool_call_id": block.get("id"), "tool_name": block.get("name")}
    if kind == "tool_result":
        return "tool_result", pointer, {"actor_type": "harness", "tool_call_id": block.get("tool_use_id"), "is_error": block.get("is_error")}
    if kind in _PRIVATE_BLOCKS:
        return "native_private_block", pointer + "/type", {"teacher_visible": False}
    if kind == "text" and isinstance(block.get("text"), str):
        event_type = "model_response" if role == "assistant" else "user_message"
        return event_type, pointer + "/text", {"actor_type": actor, "status": "partial"} if role == "assistant" else {"actor_type": actor}
    return "native_record", pointer + "/type", {"actor_type": actor, "teacher_visible": False}


def _claude_parts(row: dict) -> Iterable[EventPart]:
    kind, subtype = row.get("type"), row.get("subtype")
    if kind in {"assistant", "user"}:
        message = mapping(row.get("message"))
        content = message.get("content")
        if isinstance(content, str):
            yield ("model_response" if kind == "assistant" else "user_message"), "/message/content", {"actor_type": _ACTORS[kind]}
            return
        if isinstance(content, list) and content:
            for index, block in enumerate(content):
                if isinstance(block, dict) and isinstance(block.get("type"), str):
                    yield _claude_block(block, kind, f"/message/content/{index}")
            return
    if kind == "result":
        status = "completed" if subtype == "success" and not row.get("is_error") else "failed" if row.get("is_error") else "partial"
        yield "harness_outcome", "/subtype" if isinstance(subtype, str) else "/type", {"actor_type": "harness", "status": status}
        if isinstance(row.get("usage"), dict):
            yield "usage", "/usage", {"actor_type": "harness", "usage_scope": "cli_invocation"}
        return
    if kind == "system" and subtype == "init":
        yield "state_boundary", "/subtype", {"actor_type": "harness", "boundary": "session_present"}
    elif kind == "system" and subtype == "compact_boundary":
        yield "context_change", "/subtype", {"actor_type": "harness", "change_kind": "context_compact", "evidence_status": "observed"}
    else:
        yield "native_record", "/type" if isinstance(kind, str) else "", {"teacher_visible": False}


def _codex_parts(row: dict) -> Iterable[EventPart]:
    kind = row.get("type")
    item = mapping(row.get("item"))
    payload = mapping(row.get("payload"))
    if kind in {"thread.started", "session_meta", "turn.started", "turn.completed", "turn.failed", "error"}:
        status = {"turn.completed": "completed", "turn.failed": "failed", "error": "failed"}.get(kind)
        yield "harness_outcome" if status else "state_boundary", "/type", {"actor_type": "harness", "boundary": kind, "status": status}
        usage_key = "last_usage" if isinstance(row.get("last_usage"), dict) else "usage"
        if isinstance(row.get(usage_key), dict):
            yield "usage", f"/{usage_key}", {"actor_type": "harness", "usage_scope": "last_usage" if usage_key == "last_usage" else "provided_cli_total"}
        return
    if kind == "compacted" or (kind == "event_msg" and payload.get("type") == "context_compacted"):
        pointer = "/type" if kind == "compacted" else "/payload/type"
        yield "context_change", pointer, {"actor_type": "harness", "change_kind": "context_compact", "evidence_status": "observed"}
        return
    item_type = item.get("type")
    payload_type = payload.get("type")
    if not isinstance(payload_type, str):
        payload_type = None
    if isinstance(item_type, str) and item_type in _PRIVATE_BLOCKS:
        yield "native_private_block", "/item/type", {"teacher_visible": False}
    elif isinstance(payload_type, str) and payload_type in _PRIVATE_BLOCKS:
        yield "native_private_block", "/payload/type", {"teacher_visible": False}
    elif isinstance(item_type, str) and item_type in _CODEX_TOOLS and kind in {"item.started", "item.updated", "item.completed"}:
        event_type = {"item.started": "tool_call", "item.updated": "tool_progress", "item.completed": "tool_result"}[kind]
        yield event_type, "/item", {"actor_type": "agent" if kind == "item.started" else "harness", "tool_call_id": item.get("id"), "tool_name": item.get("name", item_type)}
    elif item_type == "agent_message" and isinstance(item.get("text"), str):
        yield "model_response", "/item/text", {"actor_type": "agent", "status": "partial"}
    elif kind == "response_item" and isinstance(payload_type, str) and payload_type in _CODEX_CALLS:
        yield "tool_call", "/payload", {"actor_type": "agent", "tool_call_id": payload.get("call_id"), "tool_name": payload.get("name", payload_type)}
    elif kind == "response_item" and payload_type in {"function_call_output", "custom_tool_call_output", "mcp_call_output"}:
        yield "tool_result", "/payload", {"actor_type": "harness", "tool_call_id": payload.get("call_id")}
    elif kind == "response_item" and payload_type == "message":
        role = payload.get("role")
        if not isinstance(role, str):
            role = None
        event_type = {"assistant": "model_response", "user": "user_message"}.get(role, "native_record")
        for index, block in enumerate(records(payload.get("content"))):
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text") and isinstance(block.get("text"), str):
                yield event_type, f"/payload/content/{index}/text", {"actor_type": _ACTORS.get(role), "role": role, "teacher_visible": role in {"assistant", "user"}}
    elif kind == "event_msg" and payload_type in {"user_message", "agent_message"} and isinstance(payload.get("message"), str):
        yield "model_response" if payload_type == "agent_message" else "user_message", "/payload/message", {"actor_type": "agent" if payload_type == "agent_message" else "harness"}
    else:
        yield "native_record", "/type" if isinstance(kind, str) else "", {"teacher_visible": False}


def native_parts(row: dict, harness: str) -> Iterable[EventPart]:
    if not isinstance(row.get("type"), str):
        yield "capture_error", "", {"teacher_visible": False}
        return
    parts = _claude_parts(row) if harness == "claude_code" else _codex_parts(row)
    fallback = True
    for part in parts:
        fallback = False
        yield part
    if fallback:
        yield "native_record", "/type" if isinstance(row.get("type"), str) else "", {"teacher_visible": False}
