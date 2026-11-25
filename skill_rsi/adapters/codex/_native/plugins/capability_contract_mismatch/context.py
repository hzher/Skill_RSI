"""Executor context supplied only when this family is enabled."""
import json
from ...storage import atomic

def context(config, path, specifications):
    restored_context = ""
    if any(spec['kind'].startswith('capability-guard-') for spec in specifications):
        from .guard import report
        capability_report = report(config)
        atomic(path / 'capability-report.json', capability_report)
        restored_context += ('\n\nMachine-checked current capability contract:\n' +
            json.dumps(capability_report, ensure_ascii=False) +
            '\nUse capability_check to inspect an uncertain operation. If a requested tool is absent, '
            'use only available authorized source channels to recover facts or procedures; this does '
            'not constitute the missing operation. Do not invent a Skill read or bypass disabled '
            'persistence. Complete the independently supported business actions with original tools, '
            'verify returned state and do not abandon them solely because one requested mechanism is '
            'unavailable. Preserve the requested final output format. Do not claim unsupported fields '
            'were written merely because a service accepted other fields.')
        if any(spec['kind'] in ('capability-guard-v3','capability-guard-v4','capability-guard-v5') for spec in specifications):
            restored_context += ('\nCapabilityGuard evidence preparation is enforced before business writes. '
                'Read the current target and original applicable evidence first, then call capability_prepare '
                'with exact tool arguments, a verbatim current-task quote, target and source result pointers, '
                'a verbatim source quote and applicability reasoning. Evidence IDs are the source_call_id '
                'explicitly returned in runtime_plugin_receipt. Read back the target after a write before '
                'preparing another action on it. Read current target instructions before choosing a historical '
                'source: a general policy to prefer latest does not override a request for an exact prior '
                'decision. Resolve conflicting sources using scope and the requested time, not titles alone. '
                'Retain complete semantic qualifiers in output fields, including the activity permitted, '
                'not just a location or ID. Preparation is neither execution nor proof of semantic correctness.')
    return restored_context
