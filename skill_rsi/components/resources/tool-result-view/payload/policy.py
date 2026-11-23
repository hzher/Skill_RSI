"""A reversible line projection for long text tool results."""

import hashlib
import re

_ERROR = re.compile(r"\b(?:fatal|error|traceback|panic|failed)\b", re.IGNORECASE)


def focus_source(text, start, end):
    """Port of the DSH focusSource projection with 1-based original line numbers."""
    lines = text.split("\n")
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        return None
    content = "\n".join(lines[start - 1:end])
    rendered = ("[…]\n" if start > 1 else "") + content + ("\n[…]" if end < len(lines) else "")
    if len(rendered.encode("utf-8")) >= len(text.encode("utf-8")):
        return None
    return rendered


def _range(lines, event, radius, projection):
    if projection == "fixed":
        return 1, min(len(lines), radius * 2 + 1)
    focus = event.get("focus")
    if isinstance(focus, dict):
        return focus.get("start_line"), focus.get("end_line")
    found = next((number for number, line in enumerate(lines, 1) if _ERROR.search(line)), None)
    if found is None:
        return None
    return max(1, found - radius), min(len(lines), found + radius)


def propose(context):
    event = context.event.payload
    text = event.get("text")
    target = event.get("target") or event.get("call_id")
    if not isinstance(text, str) or not isinstance(target, str) or not target:
        return {"reason": "missing_original_or_target"}
    if event.get("success") is False or event.get("format") not in {None, "text", "log"}:
        return {"reason": "native_result_required"}
    if event.get("allow_projection") is False:
        return {"reason": "projection_not_allowed"}
    lines = text.split("\n")
    min_lines = context.options.get("min_lines", 60)
    radius = context.options.get("context_lines", 4)
    projection = context.options.get("projection", "error_window")
    if (type(min_lines) is not int or min_lines < 2 or type(radius) is not int or not 0 <= radius <= 30
            or projection not in {"error_window", "fixed"}):
        return {"reason": "invalid_projection_options"}
    if len(lines) < min_lines:
        return {"reason": "result_short_enough"}

    selected = _range(lines, event, radius, projection)
    if selected is None:
        return {"reason": "no_relevant_focus_found"}
    start, end = selected
    focused = focus_source(text, start, end)
    if focused is None:
        return {"reason": "requested_focus_not_smaller"}
    handle = context.store_original(text, event.get("source_ref"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    heading = f"[Skill RSI: original lines {start}-{end} of {len(lines)}; expand {handle}]"
    rendered = heading + "\n" + focused
    if len(rendered.encode("utf-8")) >= len(text.encode("utf-8")):
        return {"reason": "projected_result_not_smaller"}
    return {
        "reason": "long_text_focused" if projection == "error_window" else "long_text_fixed_mask",
        "action": {
            "kind": "replace_tool_result",
            "target": target,
            "payload": {
                "text": rendered, "handle": handle, "artifact_digest": digest, "original_digest": digest,
                "start_line": start, "end_line": end, "total_lines": len(lines),
                "projection": projection,
            },
        },
    }
