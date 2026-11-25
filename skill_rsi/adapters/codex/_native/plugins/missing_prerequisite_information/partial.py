"""Compile explicitly declared field dependencies; never synthesize a value."""
from copy import deepcopy
from ..common import digest
from ..schema import schema_issues


def compile_fields(parent, request):
    request = deepcopy(request)
    requirements = {r['id']:r for r in request.get('requirements',[]) if isinstance(r,dict) and 'id' in r}
    tools = {t['name']:t for t in parent.config['tools']}
    records = []
    for action in request.get('actions',[]):
        dependencies = action.pop('field_requirements', {})
        if not isinstance(dependencies,dict):
            raise ValueError('field_requirements must map top-level argument names to requirement IDs')
        if action.get('tool') not in tools:
            raise ValueError('Partial actions require an actual currently available business tool')
        schema = tools[action['tool']]['inputSchema']
        original = deepcopy(action['arguments'])
        required = set(schema.get('required',[]))
        dropped = []
        for field, needed in dependencies.items():
            absent = field not in original
            optional_absence = (parent.variant5 and absent and field in schema.get('properties',{})
                                and field not in required)
            if (not isinstance(field,str) or (absent and not optional_absence) or '/' in field
                    or not isinstance(needed,list) or not needed
                    or any(not isinstance(n,str) or n not in requirements for n in needed)):
                raise ValueError('Each field must exist in the original arguments and cite declared requirements')
            if any(requirements[n]['state']!='supported' for n in needed):
                dropped.append(field)
                if field not in required:
                    action['arguments'].pop(field,None)
            else:
                if absent:raise ValueError('A supported field needs its actual argument value; no value is synthesized')
                action['requires'] = list(dict.fromkeys(action['requires']+needed))
        missing_required = required & set(dropped)
        if dropped and not required:
            raise ValueError('Partial compilation needs an explicit required target scope in the original schema')
        no_changes = bool(dropped) and not (set(action['arguments'])-required)
        blocked = bool(missing_required) or no_changes
        if blocked:
            # Keep an explicit blocked action in the original ledger, including
            # the real missing requirement, rather than emitting a selector-only
            # no-op that could be mistaken for a completed update.
            action['arguments'] = original
            action['requires'] = list(dict.fromkeys(action['requires']+
                [n for field in dropped for n in dependencies[field]]))
        issues=schema_issues(action['arguments'],schema)
        if issues:
            raise ValueError('Compiled arguments must satisfy the original public tool schema'+(': '+'; '.join(issues) if parent.variant5 else ''))
        records.append({'tool':action['tool'],'identity':{k:original[k] for k in required if k in original},
                        'original_sha256':digest({'tool':action['tool'],'arguments':original}),
                        'compiled_arguments':action['arguments'],'omitted_fields':dropped,
                        'blocked':blocked,'no_supported_changes':no_changes})
    return request,records


def assessment_schema(parent):
    from .bridge import schema
    value=schema(parent.action_tools(),adaptive=True)
    action=value['properties']['actions']['items']
    action['properties']['field_requirements']={'type':'object','additionalProperties':{
        'type':'array','items':{'type':'string'},'minItems':1}}
    return value
