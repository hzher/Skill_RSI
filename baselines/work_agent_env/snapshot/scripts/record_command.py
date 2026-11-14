"""Run a bounded command and save redacted, timestamped evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
MODEL_VARS = (
    "MODEL_API_KEY", "MODEL_API_URL", "MODEL_NAME", "MODEL_PLATFORM", "MODEL_PROVIDER",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
)


def now() -> str:
    return datetime.now(TZ).isoformat()


def decode(data: bytes | str | None) -> str:
    if isinstance(data, str):
        return data
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:100]:
        return data.decode("utf-16-le", errors="replace")
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def redact(text: str) -> str:
    secret_names = ("MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                    "PGPASSWORD", "PG_PASSWORD", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET")
    for name in secret_names:
        value = os.getenv(name)
        if value and len(value) >= 8:
            text = text.replace(value, f"[REDACTED:{name}]")
    text = re.sub(r"\bsk-[\w-]{12,}", "[REDACTED:KEY]", text)
    text = re.sub(r"\bAIza[\w-]{20,}", "[REDACTED:KEY]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--quiet", action="store_true", help="keep full output in evidence without echoing it")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("provide a command and a positive timeout")
    label = re.sub(r"[^a-zA-Z0-9_-]", "_", args.label)
    dest = ROOT / "evidence" / "commands" / (datetime.now(TZ).strftime("%Y%m%dT%H%M%S%f") + "_" + label)
    dest.mkdir(parents=True, exist_ok=False)
    started = now()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    status, code = "completed", None
    try:
        proc = subprocess.run(command, cwd=args.cwd.resolve(), env=env, capture_output=True, timeout=args.timeout)
        code, stdout, stderr = proc.returncode, decode(proc.stdout), decode(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        status, stdout, stderr = "timeout", decode(exc.stdout), decode(exc.stderr)
    except OSError as exc:
        status, stdout, stderr = "launch_error", "", str(exc)
    stdout, stderr = redact(stdout), redact(stderr)
    (dest / "stdout.txt").write_text(stdout, encoding="utf-8")
    (dest / "stderr.txt").write_text(stderr, encoding="utf-8")
    record = {
        "label": label, "command": [redact(c) for c in command],
        "cwd": str(args.cwd.resolve()), "started_at": started, "ended_at": now(),
        "timezone": "Asia/Shanghai", "timeout_seconds": args.timeout,
        "exit_code": code, "execution_status": status,
        "environment_variable_presence": {n: bool(os.getenv(n)) for n in MODEL_VARS},
        "stdout_ref": str((dest / "stdout.txt").relative_to(ROOT)),
        "stderr_ref": str((dest / "stderr.txt").relative_to(ROOT)),
    }
    (dest / "command.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
    print("Evidence:", dest.relative_to(ROOT))
    return code if code is not None else (124 if status == "timeout" else 127)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
