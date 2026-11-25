"""Opt-in runtime plugin harness. No plugin implementation is imported until selected."""
from .registry import build_plugins, validate_plugins, configured_plugins, executor_options, plugin_catalog
from .harness import PluginHarness, initial_context
from .audit import audit_plugins


def restore_checkpoints(instruction, snapshot, specifications):
    if not any(s['kind'].startswith('knowledge-checkpoint-') for s in specifications):
        return '', []
    from .knowledge_persistence_break.checkpoint import restore_checkpoints as restore
    return restore(instruction, snapshot, specifications)
