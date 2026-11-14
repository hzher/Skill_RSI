"""Bounded diagnostics that never persist credentials or entire failed requests."""
import hashlib
import json
import os
import re


def redact(value, limit=2500):
    text = str(value)
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[:limit]


def error_details(exc):
    result = {"exception": type(exc).__name__, "message": redact(exc)}
    for key in ("status_code", "code", "param", "type", "request_id"):
        value = getattr(exc, key, None)
        if isinstance(value, (str, int, bool)):
            result[key] = redact(value, 300) if isinstance(value, str) else value
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
        if isinstance(body, dict):
            result["provider_error"] = {key: redact(body[key]) for key in ("message", "code", "type", "param") if key in body}
    return result


def request_summary(request):
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, default=str).encode()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "model": request.get("model"), "messages": len(request.get("messages", [])),
        "tools": len(request.get("tools", [])),
        "output_budget": request.get("max_completion_tokens", request.get("max_tokens"))}
