"""One explicit registry for configuration, loading, context and experiment identity."""
from copy import deepcopy
from importlib import import_module

HARNESS_VERSION = 'runtime-plugin-harness-v1'

REGISTRY = {
    'source-body-recovery': {'task_type':'missing_prerequisite_information', 'versions':range(1,2),
        'module':'missing_prerequisite_information.focused', 'class':'SourceBodyRecovery'},
    'public-literal-recall': {'task_type':'evaluation_evidence_contract_mismatch', 'versions':range(1,3),
        'module':'evaluation_evidence_contract_mismatch.public_literals', 'class':'PublicLiteralRecall'},
    'source-version-index': {'task_type':'evaluation_evidence_contract_mismatch', 'versions':range(1,2),
        'module':'evaluation_evidence_contract_mismatch.version_index', 'class':'SourceVersionIndex'},
    'information-orchestrator': {'task_type':'missing_prerequisite_information', 'versions':range(1,6),
        'module':'missing_prerequisite_information.orchestrator', 'class':'InformationOrchestrator'},
    'evidence-bridge': {'task_type':'missing_prerequisite_information', 'versions':range(1,10),
        'module':'missing_prerequisite_information.bridge', 'class':'EvidenceBridge'},
    'knowledge-checkpoint': {'task_type':'knowledge_persistence_break', 'versions':range(1,4),
        'module':'knowledge_persistence_break.checkpoint', 'class':'KnowledgeCheckpoint'},
    'capability-guard': {'task_type':'capability_contract_mismatch', 'versions':range(1,6),
        'module':'capability_contract_mismatch.guard', 'class':'CapabilityGuard'},
    'scope-guard': {'task_type':'scope_or_execution_misapplication', 'versions':range(1,8),
        'module':'scope_or_execution_misapplication.guard', 'class':'ScopeGuard'},
}


def definition(kind):
    if not isinstance(kind,str):raise ValueError('Unknown runtime plugin')
    for name,item in REGISTRY.items():
        if kind in [name+'-v'+str(v) for v in item['versions']]:return name,item
    raise ValueError('Unknown runtime plugin: '+kind)


def plugin_catalog():
    return [{'family':name,'task_type':item['task_type'],
             'kinds':[name+'-v'+str(v) for v in item['versions']]} for name,item in REGISTRY.items()]


def validate_plugins(specifications):
    from .validation import validate_plugins as validate
    validate(specifications)


def configured_plugins(specifications, enabled=True):
    if not isinstance(enabled,bool):raise ValueError('plugin_harness_enabled must be boolean')
    specs=deepcopy(specifications if specifications is not None else [])
    validate_plugins(specs)
    return specs if enabled else []


def build_plugins(config):
    specs=configured_plugins(config.get('runtime_plugins',[]),config.get('plugin_harness_enabled',True))
    result=[]
    for spec in specs:
        _,item=definition(spec['kind'])
        cls=getattr(import_module('.'+item['module'],__package__),item['class'])
        result.append(cls(config,spec))
    return result


def executor_options(config, section='codex'):
    """Same top-level options for CLI and PAST-Bench; preserve legacy nested configs."""
    options=deepcopy(config.get(section,{}))
    for key in ('runtime_plugins','plugin_harness_enabled'):
        if key in config:
            if key in options and options[key]!=config[key]:raise ValueError('Conflicting top-level and '+section+'.'+key)
            options[key]=deepcopy(config[key])
    configured_plugins(options.get('runtime_plugins',[]),options.get('plugin_harness_enabled',True))
    return options
