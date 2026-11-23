"""Plugin specification validation."""
def validate_plugins(specifications):
    if not isinstance(specifications, list):
        raise ValueError('runtime_plugins must be a list')
    seen = set()
    families = set()
    for spec in specifications:
        if not isinstance(spec, dict):
            raise ValueError('Unknown runtime plugin')
        from .registry import definition
        family,_=definition(spec.get('kind'))
        if family in families:raise ValueError('Choose only one version of each plugin family: '+family)
        families.add(family)
        allowed = {'kind', 'read_tools', 'action_tools'}
        parent = spec['kind'].startswith('information-orchestrator-')
        if parent:
            if any(s.get('kind','').startswith('evidence-bridge-') for s in specifications):
                raise ValueError('Choose the information parent or legacy evidence bridge, not both')
            allowed |= {'read_variants','source_expansions','follow_history_references','follow_document_references','subplugins','selection'}
            from .missing_prerequisite_information.subplugins import CHILDREN
            pool, choice = spec.get('subplugins'), spec.get('selection')
            if (not isinstance(pool,list) or any(not isinstance(n,str) or n not in CHILDREN for n in pool)
                    or len(set(pool)) != len(pool) or not isinstance(choice,dict)
                    or set(choice) != {'mode','max_active'} or choice['mode'] not in ('auto','manual')
                    or type(choice['max_active']) is not int or not 0 <= choice['max_active'] <= 3
                    or (choice['mode']=='manual' and len(pool)>choice['max_active'])):
                raise ValueError('Invalid information subplugin pool or selection limit')
        if spec['kind'].startswith('evidence-bridge-'):
            allowed.add('read_variants')
        if spec['kind'] in ('evidence-bridge-v4', 'evidence-bridge-v5', 'evidence-bridge-v6', 'evidence-bridge-v7','evidence-bridge-v8','evidence-bridge-v9'): allowed.add('source_expansions')
        if spec['kind'] in ('evidence-bridge-v6', 'evidence-bridge-v7','evidence-bridge-v8','evidence-bridge-v9'): allowed.add('follow_history_references')
        if spec['kind'] in ('evidence-bridge-v7','evidence-bridge-v8','evidence-bridge-v9'): allowed.add('follow_document_references')
        if spec['kind'].startswith('scope-guard-'):
            allowed |= {'inventory_tools', 'target_fields'}
        if spec['kind'] in ('capability-guard-v3','capability-guard-v4','capability-guard-v5'):allowed.add('target_fields')
        if set(spec) - allowed or spec['kind'] in seen:
            raise ValueError('Invalid or duplicate runtime plugin configuration')
        for field in ('read_tools', 'action_tools'):
            values = spec.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError('Plugin tool bindings must be lists of names')
            if len(values) != len(set(values)):
                raise ValueError('Duplicate plugin tool binding')
        overlap = set(spec.get('read_tools', [])) & set(spec.get('action_tools', []))
        if spec['kind'].startswith('evidence-bridge-') or parent:
            variants = spec.get('read_variants', {})
            if not isinstance(variants, dict) or not set(variants) <= set(spec['read_tools']):
                raise ValueError('Read variants must bind configured read tools')
            for variant in variants.values():
                if (not isinstance(variant, dict) or set(variant) != {'pointer', 'values'} or
                    not isinstance(variant['pointer'], str) or not variant['pointer'].startswith('/') or
                    not isinstance(variant['values'], list) or not variant['values'] or
                    any(not isinstance(v, str) or not v for v in variant['values'])):
                    raise ValueError('Read variants require an argument pointer and explicit string values')
            overlap -= set(variants)
            if 'follow_document_references' in spec and (not isinstance(spec['follow_document_references'], bool) or
                    (spec['follow_document_references'] and not {'kb_search','kb_get_article'} <= set(spec['read_tools']))):
                raise ValueError('Document references require explicit knowledge search/body read bindings')
            if 'follow_history_references' in spec and (not isinstance(spec['follow_history_references'], bool) or
                    (spec['follow_history_references'] and 'session_search' not in spec['read_tools'])):
                raise ValueError('History reference expansion requires an explicit enabled read binding')
            expansions = spec.get('source_expansions', [])
            if not isinstance(expansions, list) or len(expansions) > 8: raise ValueError('Use at most eight source expansion bindings')
            for rule in expansions:
                if (not isinstance(rule, dict) or set(rule) != {'source_tool','target_tool','items_pointer','id_pointer','argument'} or
                    any(not isinstance(v, str) or not v for v in rule.values()) or
                    rule['source_tool'] not in spec['read_tools'] or rule['target_tool'] not in spec['read_tools'] or
                    rule['target_tool'] in spec.get('action_tools', []) or
                    not rule['items_pointer'].startswith('/') or not rule['id_pointer'].startswith('/')):
                    raise ValueError('Source expansions must bind existing read-only tools and explicit public index paths')
        if not spec.get('read_tools') or overlap:
            raise ValueError('Knowledge reads and business actions require disjoint tool bindings')
        if spec['kind'] in ('capability-guard-v3','capability-guard-v4','capability-guard-v5'):
            fields=spec.get('target_fields')
            if not isinstance(fields,dict) or set(fields)!=set(spec.get('action_tools',[])) or any(not isinstance(p,str) or not p.startswith('/') for p in fields.values()):
                raise ValueError('Capability preparations require target identity paths for each action')
        if spec['kind'].startswith('scope-guard-'):
            inventory = spec.get('inventory_tools')
            targets = spec.get('target_fields')
            if (not isinstance(inventory, list) or not inventory or
                    any(not isinstance(name, str) for name in inventory) or
                    not set(inventory) <= set(spec['read_tools']) or
                    not isinstance(targets, dict) or not spec.get('action_tools') or
                    set(targets) != set(spec['action_tools']) or
                    any(not isinstance(path, str) or not path.startswith('/') for path in targets.values())):
                raise ValueError('Invalid inventory/target bindings for scope guard')
        seen.add(spec['kind'])
