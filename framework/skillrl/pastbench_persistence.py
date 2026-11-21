"""Collection profile: keep ordinary stages enabled; restore original controls."""
from copy import deepcopy

PROTOCOL = 'experience-collection-control-switches-v1'
FLAG_NAMES = frozenset(('memory_enabled', 'user_profile_enabled', 'skills_enabled', 'session_search_enabled'))


def restore_specifications(specifications, restoration):
    if restoration is None:
        return specifications
    if restoration.get('protocol') != PROTOCOL or not restoration.get('upstream_commit'):
        raise ValueError('Unknown or unpinned persistence restoration')
    rules = restoration['episodes']
    from pathlib import Path
    if set(rules) != {Path(s['task']).name for s in specifications}:
        raise ValueError('Persistence rules must cover the exact whole-family inventory')
    result = deepcopy(specifications)
    for spec in result:
        rule = rules[Path(spec['task']).name]
        flags = rule['tool_flags']
        if set(flags) != FLAG_NAMES or any(type(v) is not bool for v in flags.values()):
            raise ValueError('Four explicit boolean persistence flags required')
        for field in ('persistence_allowed', 'history_mode', 'history_load_anchor', 'history_save_anchor'):
            spec[field] = rule[field]
        spec['persistence_tool_flags'] = dict(flags)
        spec['persistence_runtime_enabled'] = bool(rule['persistence_allowed'])
        spec['persistence_restoration_protocol'] = PROTOCOL
    return result


def restore_environment_flags(environment, specification):
    flags = specification.get('persistence_tool_flags')
    if flags is None:
        return
    environment.persistence_flags = dict(flags)
    if not flags['skills_enabled'] and hasattr(environment, 'skill_write_capability'):
        del environment.skill_write_capability


def persistence_runtime_enabled(specification):
    return specification.get('persistence_runtime_enabled', specification['persistence_allowed'])
