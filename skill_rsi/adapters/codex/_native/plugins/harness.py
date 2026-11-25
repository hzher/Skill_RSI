"""Lifecycle composition: validate all guards before any guard reserves a write."""
from importlib import import_module
from .registry import REGISTRY, build_plugins, configured_plugins


class PluginHarness:
    def __init__(self,config):
        self.config=config
        self.plugins=build_plugins(config)

    def tools(self):
        from ..codex_mcp import base_catalog
        reserved={t['name'] for t in base_catalog(self.config)} | {'Skill','memory','session_search','skill_manage'}
        catalog=[];owners={}
        for plugin in self.plugins:
            for item in plugin.catalog() if hasattr(plugin,'catalog') else []:
                name=item['name']
                if name in reserved or name in owners:raise ValueError('Plugin tool name collision: '+name)
                catalog.append(item);owners[name]=plugin
        return catalog,owners

    def before(self,name,args,call_id):
        # No state is consumed until every enabled policy accepts the call.
        for plugin in self.plugins:
            if hasattr(plugin,'check_before'):plugin.check_before(name,args,call_id)
        committed=[]
        try:
            for plugin in self.plugins:
                if hasattr(plugin,'commit_before'):
                    committed.append(plugin)
                    plugin.commit_before(name,args,call_id)
        except Exception:
            for plugin in reversed(committed):
                if hasattr(plugin,'abort_before'):plugin.abort_before(name,args,call_id)
            raise


def initial_context(config,path,instruction,snapshot,specifications):
    specs=configured_plugins(specifications,config.get('plugin_harness_enabled',True))
    if not specs:return '',[]
    text='';restored=[]
    # Keep the established single-plugin prompt text and deterministic family order.
    for prefix,item in REGISTRY.items():
        selected=[s for s in specs if s['kind'].startswith(prefix+'-v')]
        if not selected:continue
        if prefix in ('information-orchestrator','source-body-recovery','public-literal-recall','source-version-index'):
            module=import_module('.'+item['module'],__package__)
            text+=getattr(module,item['class'])(config,selected[0]).initial_context()
        elif prefix=='knowledge-checkpoint':
            module=import_module('.'+item['module'],__package__)
            addition,versions=module.restore_checkpoints(instruction,snapshot,selected)
            text+=addition;restored.extend(versions)
        else:
            module=import_module('.'+item['task_type']+'.context',__package__)
            text+=module.context(config,path,selected)
    active_context_specs=[s for s in specs if not (s['kind'].startswith('information-orchestrator-')
                          and s['selection']['max_active']==0)]
    if len(active_context_specs)>1:
        if any(s['kind']=='information-orchestrator-v5' for s in active_context_specs):
            text+=('\nAll enabled runtime policies apply together. For business writes protected by '
                   'ScopeGuard or CapabilityGuard, prepare each applicable guard for the SAME original '
                   'arguments. Information assessment is optional for genuine concrete dependencies; '
                   'it is not a mandatory additional approval for every read or write. Supported work '
                   'needs no information plan. A preparation does not bypass another guard or grant permissions.')
        else:text+=('\nAll enabled runtime policies apply together. Complete each enabled guard preparation '
               'for the SAME original tool arguments before dispatch. A successful plan/check from one '
               'plugin does not bypass another. These checks do not grant extra permissions.')
    return text,restored
