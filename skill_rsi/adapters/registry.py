"""Discover packaged host adapters without importing their native dependencies."""

from functools import cache
from importlib import import_module
from importlib.resources import files

_ALIASES = {"claude": "claude_code", "claude-code": "claude_code"}


@cache
def available():
    return tuple(sorted(
        child.name for child in files(__package__).iterdir()
        if child.name.isidentifier() and not child.name.startswith("_")
        and child.joinpath("__init__.py").is_file()
    ))


def get_adapter(name):
    key = _ALIASES.get(name, name)
    if key not in available():
        raise ValueError(f"Unsupported host {name!r}. Expected one of {', '.join(available())}.")
    try:
        module = import_module(f".{key}", __package__)
    except ImportError as error:
        raise ValueError(f"Cannot load the packaged {key} adapter: {error}") from error
    factory = getattr(module, "Adapter", None)
    if not callable(factory):
        raise ValueError(f"{module.__name__} must export an Adapter factory")
    adapter = factory()
    if getattr(adapter, "name", None) != key:
        raise ValueError(f"{module.__name__}.Adapter must declare name = {key!r}")
    return adapter
