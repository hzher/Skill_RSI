"""Private LLM configuration with endpoint-bound credential references."""

from pathlib import Path
import os
import re
from urllib.parse import urlsplit

from ..components._local import state_root
from ..evidence.io import file_lock, read_json, write_json

_DEFAULT_ENV = Path.home() / "Desktop/memorax/Skill_RSI_hzh/.env"
_PLACEHOLDER = re.compile(r"(?i)^(?:YOUR[_ -]?(?:API[_ -]?)?KEY|YOUR[_ -]?TOKEN|CHANGEME|<.*>|\$\{.*\}|sk-placeholder)$")


def _env(path):
    if not path:
        return {}
    source = Path(path).expanduser()
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise ValueError("请选择已存在且小于 1 MiB 的 .env 文件")
    values = {}
    for line in source.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match:
            value = match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            values[match[1]] = value
    return values


def _origin(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM 地址需要使用不含凭据、查询参数的 HTTPS 地址")
    return parsed.scheme + "://" + parsed.netloc.lower()


def _valid_key(value):
    return (isinstance(value, str) and bool(value.strip()) and not _PLACEHOLDER.fullmatch(value.strip())
            and not any(ord(character) < 33 or ord(character) == 127 for character in value.strip()))


def _load(state):
    path = state_root(state) / "llm-settings.json"
    if path.is_file():
        return read_json(path)
    return {"protocol": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro",
            "env_file": str(_DEFAULT_ENV) if _DEFAULT_ENV.is_file() else ""}


def _credential(settings):
    origin = _origin(settings["base_url"])
    protocol = settings["protocol"]
    if (_valid_key(settings.get("api_key")) and settings.get("credential_origin") == origin
            and settings.get("credential_protocol") == protocol):
        return settings["api_key"], "saved"
    values = _env(settings.get("env_file"))
    env_key = settings.get("env_key")
    if (env_key and settings.get("env_credential_origin") == origin
            and settings.get("env_credential_protocol") == protocol):
        key = values.get(env_key)
        if _valid_key(key):
            return key, "env:" + env_key
    # Each native environment variable is tied to its own protocol and provider.
    if protocol == "openai" and origin == "https://api.deepseek.com":
        key = values.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        return (key, "DEEPSEEK_API_KEY") if _valid_key(key) else (None, None)
    for source in (values, os.environ):
        if protocol == "openai":
            base = source.get("OPENAI_BASE_URL") or "https://api.openai.com"
            key = source.get("OPENAI_API_KEY")
        else:
            base = source.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
            key = source.get("ANTHROPIC_AUTH_TOKEN") or source.get("ANTHROPIC_API_KEY")
        if _origin(base) == origin and _valid_key(key):
            return key, "protocol_environment"
    return None, None


def get_settings(state) -> dict:
    settings = _load(state)
    public = {name: settings.get(name, "") for name in ("protocol", "base_url", "model", "env_file", "env_key")}
    try:
        key, source = _credential(settings)
    except (OSError, ValueError):
        return {**public, "has_api_key": False, "credential_source": None, "error": "凭据来源不可读，请重新配置"}
    return {**public, "has_api_key": bool(key), "credential_source": source}


def save_settings(state, values) -> dict:
    if not isinstance(values, dict):
        raise ValueError("LLM 配置需要 JSON 对象")
    path = state_root(state) / "llm-settings.json"
    with file_lock(path.with_suffix(".lock")):
        previous = _load(state)
        settings = {name: values.get(name, previous.get(name, "")) for name in ("protocol", "base_url", "model", "env_file")}
        if settings["protocol"] not in {"openai", "anthropic"}:
            raise ValueError("protocol 请选择 openai 或 anthropic")
        if any(not isinstance(settings[key], str) for key in settings):
            raise ValueError("LLM 配置字段需要填写文本")
        settings["base_url"] = settings["base_url"].strip().rstrip("/")
        origin = _origin(settings["base_url"])
        if not settings["model"].strip() or len(settings["model"]) > 200:
            raise ValueError("请填写模型名称")
        if settings["env_file"]:
            settings["env_file"] = str(Path(settings["env_file"]).expanduser().resolve(strict=True))
            _env(settings["env_file"])
        env_key = values.get("env_key", previous.get("env_key", ""))
        if not isinstance(env_key, str) or (env_key and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key)):
            raise ValueError("环境变量名需要使用字母、数字和下划线")
        if env_key:
            if "env_key" in values:
                settings.update(env_key=env_key, env_credential_origin=origin,
                                env_credential_protocol=settings["protocol"])
            elif origin == previous.get("env_credential_origin") and settings["protocol"] == previous.get("env_credential_protocol"):
                for name in ("env_key", "env_credential_origin", "env_credential_protocol"):
                    settings[name] = previous[name]
        supplied = values.get("api_key")
        if supplied:
            if not _valid_key(supplied):
                raise ValueError("请填写真实 API Key，YOUR_API_KEY 是示例占位符")
            settings.update(api_key=supplied.strip(), credential_origin=origin, credential_protocol=settings["protocol"])
        elif not values.get("clear_api_key") and origin == previous.get("credential_origin") and settings["protocol"] == previous.get("credential_protocol"):
            for name in ("api_key", "credential_origin", "credential_protocol"):
                if name in previous:
                    settings[name] = previous[name]
        write_json(path, settings)
        path.chmod(0o600)
    return get_settings(state)


def execution_settings(state):
    settings = _load(state)
    key, _ = _credential(settings)
    if not key:
        raise ValueError("所选 LLM 地址没有对应的有效凭据，请配置匹配的 API Key 或 .env")
    return settings, key


def endpoint(settings):
    base = settings["base_url"].rstrip("/")
    if settings["protocol"] == "anthropic":
        return base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"
