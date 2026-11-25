"""Local Codex generation preferences, independent of user auth and API settings."""

import os
from pathlib import Path
import shutil

from ..components._local import state_root
from ..evidence.io import file_lock, read_json, write_json

MODEL = "gpt-6.1-sol"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


def _command():
    found = shutil.which("codex")
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    return found or (str(bundled) if bundled.is_file() else "codex")


def get_settings(state_dir=None):
    path = state_root(state_dir) / "codex-generation.json"
    saved = read_json(path) if path.is_file() else {}
    values = {"command": saved.get("command") or _command(),
              "model": saved.get("model") or MODEL, "effort": saved.get("effort") or "ultra"}
    executable = shutil.which(values["command"])
    return {**values, "available": bool(executable and os.access(executable, os.X_OK)),
            "provider": "codex_cli", "authentication": "local_codex_account"}


def save_settings(state_dir, data):
    if not isinstance(data, dict):
        raise ValueError("Codex 配置需要 JSON 对象")
    values = get_settings(state_dir)
    for key in ("command", "model", "effort"):
        if key in data:
            value = data[key]
            if (not isinstance(value, str) or not value.strip() or len(value) > 4096
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError(f"{key} 需要填写有效文本")
            values[key] = value.strip()
    if values["effort"] not in EFFORTS:
        raise ValueError("不支持的 Codex 推理强度")
    if len(values["model"]) > 200:
        raise ValueError("模型名称过长")
    state = state_root(state_dir)
    with file_lock(state / ".codex-generation.lock"):
        write_json(state / "codex-generation.json", {key: values[key] for key in ("command", "model", "effort")})
    return get_settings(state)
