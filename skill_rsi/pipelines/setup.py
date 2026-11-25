"""Connect a project's host to locally selected extension releases."""

from pathlib import Path

from ..components import MODES
from ..runtime import Runtime


def _install_extensions(registry, entries, base):
    if not isinstance(entries, list):
        raise ValueError("extensions must be a list of sources or {source, mode, options} objects")
    selected = {}
    for entry in entries:
        if isinstance(entry, str):
            entry = {"source": entry}
        if not isinstance(entry, dict) or entry.keys() - {"source", "mode", "options"}:
            raise ValueError("each extension accepts source, mode and options")
        source, mode, options = entry.get("source"), entry.get("mode", "shadow"), entry.get("options", {})
        if not isinstance(source, str) or not source.strip():
            raise ValueError("each extension requires a nonempty source")
        if not isinstance(mode, str) or mode not in MODES or not isinstance(options, dict):
            raise ValueError("an extension needs mode shadow, suggest or apply and an options object")
        location = source if source.startswith("builtin:") else base / Path(source).expanduser()
        release = registry.install(location)
        if release.component_id in selected:
            raise ValueError(f"Select only one release of {release.component_id} per host")
        selected[release.component_id] = (release, mode, options)
    return selected


def setup(adapter, project, state_dir, *, options, extensions=None, base, reconcile=False, manage_all=False):
    runtime = Runtime(project, state_dir=state_dir)
    observe = options.get("observe")
    if observe is not None and type(observe) is not bool:
        raise ValueError("observe must be true or false")
    selected = _install_extensions(runtime.registry, extensions, base) if extensions is not None else {}
    # The adapter describes what it can support. The live callback checks again
    # against the nodes the host actually provided before activating a binding.
    capabilities = adapter.capabilities({"nodes": adapter.nodes}) if selected or reconcile else {}
    with runtime.configure(adapter.name, selected, capabilities=capabilities,
                           reconcile=reconcile, observe=observe, manage_all=manage_all) as bindings:
        registration = adapter.attach(project, state_dir, **options)
    if extensions is None:
        return registration
    return {**registration, "extensions": [binding.to_dict() for binding in bindings]}
