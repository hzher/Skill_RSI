"""Capability checks over the actual catalog; no extra permissions or invented tool reads."""
import json
from pathlib import Path

from ...storage import canonical
import hashlib


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def available(config):
    from ...codex_mcp import base_catalog
    return {item['name']: item for item in base_catalog(config)}


def schema_issues(value, schema, path='$'):
    issues=[]
    kind=schema.get('type')
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'integer':isinstance(value,int) and not isinstance(value,bool),
           'number':isinstance(value,(int,float)) and not isinstance(value,bool),'boolean':isinstance(value,bool),
           'null':value is None}
    if isinstance(kind,str) and kind in valid and not valid[kind]:
        return [path+' must be '+kind]
    if 'enum' in schema and value not in schema['enum']:issues.append(path+' must be one of '+repr(schema['enum']))
    if isinstance(value,dict):
        props=schema.get('properties',{})
        issues.extend(path+'/'+key+' is required' for key in schema.get('required',[]) if key not in value)
        if schema.get('additionalProperties') is False:
            issues.extend(path+'/'+key+' is not accepted by the declared schema' for key in value if key not in props)
        for key,item in value.items():
            if key in props:issues.extend(schema_issues(item,props[key],path+'/'+key))
    if isinstance(value,list) and isinstance(schema.get('items'),dict):
        for i,item in enumerate(value):issues.extend(schema_issues(item,schema['items'],path+'/'+str(i)))
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if 'minimum' in schema and value<schema['minimum']:issues.append(path+' below minimum '+str(schema['minimum']))
        if 'maximum' in schema and value>schema['maximum']:issues.append(path+' above maximum '+str(schema['maximum']))
    return issues


def report(config):
    tools=available(config)
    return {'catalog_sha256':digest(list(tools.values())), 'available_tools':list(tools),
            'knowledge_channels':{name:name in tools for name in ('Skill','session_search','memory','skill_manage')},
            'available_skill_names':list(config.get('skills',{})),
            'contracts':{name:{'required':t['inputSchema'].get('required',[]),
                               'parameters':list(t['inputSchema'].get('properties',{})),
                               'inputSchema':t['inputSchema']} for name,t in tools.items()},
            'boundary':'Available source retrieval may recover facts, but is not equivalent to an unavailable Skill read. '
                       'No permission, capability or successful operation is created by this report.'}


class CapabilityGuard:
    version2=True  # Use recoverable MCP tool errors, with a capability-specific recovery key.
    recovery_key='capability_recovery'

    def __init__(self,config,spec):
        self.config,self.spec=config,spec
        self.evidence=None
        if spec['kind'] in ('capability-guard-v3','capability-guard-v4','capability-guard-v5'):
            from .evidence import EvidencePreparation
            self.evidence=EvidencePreparation(self)

    def record(self,status,**fields):
        item={'plugin':self.spec['kind'],'source_actor':'framework_plugin','status':status,**fields}
        with Path(self.config['plugin_events']).open('a') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
        return item

    def status(self):return report(self.config)

    def catalog(self):
        result=[{'name':'capability_check','description':'Check a requested operation against the actual public tool catalog and argument schema. '
            'Use this to resolve an unavailable tool or parameter. Alternatives are source channels, not equivalent operations. '
            'No business action is dispatched; perform supported actions using original tools and verify their results.',
            'inputSchema':{'type':'object','properties':{'tool':{'type':'string'},'arguments':{'type':'object'}},
                           'required':['tool'],'additionalProperties':False}}]
        if self.evidence:
            from .evidence import catalog
            result.append(catalog([name for name in self.spec['action_tools'] if name in available(self.config)]))
        return result

    def check(self,name,args=None):
        tools=available(self.config)
        result={'tool':name,'available':name in tools,'operation_executed':False,
                'requested_capability_fulfilled':False,'catalog_sha256':digest(list(tools.values()))}
        if name not in tools:
            result.update(reason='Tool is not exposed under current permissions',
                alternative_source_channels=[n for n in ('session_search','memory','Skill') if n in tools],
                alternatives_are_semantically_equivalent=False)
        else:
            result['inputSchema']=tools[name]['inputSchema']
            if args is not None:
                result['issues']=schema_issues(args,tools[name]['inputSchema'])
                if name=='Skill' and args.get('name') not in self.config.get('skills',{}):
                    result['issues'].append('Requested skill is not in the actual catalog; available names: '+repr(list(self.config.get('skills',{}))))
                result['arguments_valid']=not result['issues']
                result['undeclared_parameters']=[k for k in args if k not in tools[name]['inputSchema'].get('properties',{})]
                result['unknown_parameter_semantics']='An open schema does not establish that an undocumented field is supported; check returned state.'
        return result

    def handle(self,name,args,call_id):
        if name=='capability_prepare' and self.evidence:
            from .evidence import catalog
            if self.spec['kind']=='capability-guard-v5':
                # Validation is an inspection with a negative result, not a
                # failed business dispatch. Unprepared real writes still fail.
                try:
                    args=dict(args)
                    if isinstance(args.get('tool'),str) and args['tool'].startswith('mcp__benchmark__'):
                        args['tool']=args['tool'][len('mcp__benchmark__'):]
                    issues=schema_issues(args,catalog()['inputSchema'])
                    if issues:raise ValueError('; '.join(issues))
                    return self.evidence.prepare(args,call_id)
                except (ValueError,KeyError,TypeError,IndexError) as exc:
                    self.record('preparation_rejected',executor_call_id=str(call_id),reason=str(exc))
                    return {'prepared':False,'operation_executed':False,'issue':str(exc),
                            'next':'Correct the preparation using bare tool names and paths from evidence_manifest. Do not execute an unprepared write.'}
            issues=schema_issues(args,catalog()['inputSchema'])
            if issues:raise ValueError('; '.join(issues))
            return self.evidence.prepare(args,call_id)
        if name!='capability_check' or set(args)-{'tool','arguments'}:raise ValueError('Invalid capability check')
        value=self.check(args['tool'],args.get('arguments'))
        self.record('capability_checked',executor_call_id=str(call_id),check=value)
        return value

    def before(self,name,args,call_id):
        self.check_before(name,args,call_id)
        self.commit_before(name,args,call_id)

    def check_before(self,name,args,call_id):
        value=self.check(name,args)
        # The original session adapter already clamps retrieval page size. Preserve
        # that established behavior; never normalize business fields or other tools.
        if self.spec['kind'] in ('capability-guard-v2','capability-guard-v3','capability-guard-v4','capability-guard-v5') and name=='session_search' and value['available']:
            limit=args.get('limit',5)
            if isinstance(limit,int) and not isinstance(limit,bool):
                effective={**args,'limit':max(1,min(8,limit))}
                if effective!=args and self.check(name,effective).get('arguments_valid'):
                    self.record('native_normalization_accepted',executor_call_id=str(call_id),
                                tool=name,original_arguments=args,effective_arguments=effective,
                                basis='Original session_search adapter clamps limit to [1,8]')
                    value=self.check(name,effective)
        if not value['available'] or not value.get('arguments_valid'):
            self.record('unsupported_dispatch_blocked',executor_call_id=str(call_id),check=value)
            raise ValueError(json.dumps(value,ensure_ascii=False))
        if self.evidence and name in self.spec['action_tools']:
            self.evidence.check_before(name,args,call_id)

    def commit_before(self,name,args,call_id):
        if self.evidence and name in self.spec['action_tools']:
            self.evidence.commit_before(name,args,call_id)
        self.record('dispatch_contract_checked',executor_call_id=str(call_id),tool=name,
                    catalog_sha256=report(self.config)['catalog_sha256'],arguments_sha256=digest(args))

    def abort_before(self,name,args,call_id):
        if self.evidence and name in self.spec['action_tools']:self.evidence.abort_before(name,args,call_id)

    def observe(self,event):
        self.record('dispatch_returned',executor_call_id=event['call_id'],tool=event['name'],
                    arguments_sha256=digest(event['arguments']),result_sha256=digest(event['result']),
                    transport_success=not event.get('error'),semantic_completion_verified=False)
        if self.evidence and not event.get('error') and event['name'] in self.spec['read_tools']:
            extra={'evidence_manifest':self.evidence.manifest(event)} if self.spec['kind']=='capability-guard-v5' else {}
            return self.record('evidence_available',source_call_id=event['call_id'],persisted=False,**extra)
        return None
