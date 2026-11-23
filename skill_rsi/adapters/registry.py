"""Lazy host registration, keeping native dependencies optional."""

from importlib import import_module

_MODULES = {
    "dsh": ".dsh",
    "claude_code": ".claude_code",
    "codex": ".codex",
}
_ALIASES = {"claude": "claude_code", "claude-code": "claude_code"}


def available():
    return tuple(_MODULES)


def get_adapter(name):
    key = _ALIASES.get(name, name)
    if key not in _MODULES:
        raise ValueError(f"Unsupported host {name!r}. Expected one of {', '.join(_MODULES)}.")
    return import_module(_MODULES[key], __package__).Adapter()
