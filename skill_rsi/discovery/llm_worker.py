"""One detached HTTP analysis job using the shared task lifecycle."""

import argparse
import json
import os
from pathlib import Path
import signal
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from ..evidence.io import file_lock, read_json, write_bytes, write_json
from ..review.projection import SECRET
from ..tasks.store import _edit, _now, _process_stamp
from .llm_settings import endpoint, execution_settings
from .teacher import NoRedirect, SYSTEM, Teacher


class _TaskTeacher(Teacher):
    def body(self, phase, prompt):
        body = {"model": self.config["model"], "temperature": 0, "max_tokens": 6000,
                "stream": False, "messages": [{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": prompt}]}
        if self.config["model"].lower().startswith("deepseek"):
            body["thinking"] = {"type": "disabled"}
            body["response_format"] = {"type": "json_object"}
        return json.dumps(body, ensure_ascii=False).encode()


def _anthropic(settings, key, prompt, capture):
    body = json.dumps({"model": settings["model"], "max_tokens": 6000, "system": SYSTEM,
                       "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode()
    request = Request(endpoint(settings), data=body, method="POST", headers={
        "Content-Type": "application/json", "anthropic-version": "2023-06-01",
        "x-api-key": key, "Authorization": "Bearer " + key})
    started = monotonic()
    try:
        response = build_opener(NoRedirect).open(request, timeout=180)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(131073)
        cleaned = raw[:131072].replace(key.encode(), b"[redacted]")
        write_bytes(capture / "response.json", cleaned)
        if len(raw) > 131072:
            raise ValueError("LLM 响应超过 128 KiB")
        if cleaned != raw:
            raise ValueError("LLM 响应出现凭据，已拒绝处理")
        if not 200 <= response.code < 300:
            raise ValueError(f"LLM HTTP {response.code}，请检查地址、模型与凭据")
        value = json.loads(raw)
    if value.get("stop_reason") not in {"end_turn", "stop_sequence"}:
        raise ValueError("LLM 返回未完成的内容，请调整模型或长度后重试")
    text = "\n".join(block["text"] for block in value.get("content", []) if isinstance(block, dict)
                      and block.get("type") == "text" and isinstance(block.get("text"), str))
    return text, {"model": value.get("model"), "usage": value.get("usage"), "elapsed_seconds": monotonic() - started}


def _cancel(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _cancel)
    capture = args.task / "capture"
    capture.mkdir(mode=0o700, exist_ok=True)
    key = None
    with file_lock(args.task / ".worker.lock"):
        try:
            task = read_json(args.task / "task.json")
            request = read_json(args.task / "request.json")
            with _edit(args.task) as current:
                current.update(status="running", started_at=_now(), worker_pid=os.getpid(),
                               worker_stamp=_process_stamp(os.getpid()))
            if (args.task / "cancel.json").exists():
                raise KeyboardInterrupt()
            settings, key = execution_settings(args.state_dir)
            for name in ("protocol", "base_url", "model", "env_file", "env_key"):
                if settings.get(name, "") != request["settings"].get(name, ""):
                    raise ValueError("分析配置已变更，请重新点击分析")
            prompt = task["prompt"]
            if key in prompt or SECRET.search(prompt):
                raise ValueError("输入出现凭据模式，已拒绝发送")
            if settings["protocol"] == "openai":
                credential_env = "_SKILL_RSI_ANALYSIS_KEY"
                os.environ[credential_env] = key
                teacher = _TaskTeacher(capture, {"endpoint": endpoint(settings), "provider": "user-configured",
                    "model": settings["model"], "credential_env": credential_env, "max_calls": 1,
                    "max_prompt_bytes": 60000, "max_response_bytes": 131072, "max_output_tokens": 6000,
                    "timeout_seconds": 180, "strength_basis": "user-selected analysis model"})
                parsed = teacher.ask("task_memory", prompt)
                text = json.dumps(parsed, ensure_ascii=False)
                receipt = {"model": settings["model"], "usage": teacher.usage_summary()}
            else:
                text, receipt = _anthropic(settings, key, prompt, capture)
            write_json(capture / "launch.json", {"status": "completed", "final_response": text,
                                                "protocol": settings["protocol"], **receipt})
            with _edit(args.task) as current:
                current.update(status="completed", finished_at=_now(), final_response=text, returncode=0)
        except BaseException as error:
            message = str(error) or type(error).__name__
            if key:
                message = message.replace(key, "[redacted]")
            message = SECRET.sub("[redacted]", message)[:1000]
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            write_json(capture / "launch.json", {"status": status, "error": message})
            with _edit(args.task) as current:
                current.update(status=status, finished_at=_now(), error=message, returncode=130 if status == "interrupted" else 1)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
