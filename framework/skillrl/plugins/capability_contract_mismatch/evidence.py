"""Evidence-bound preparation for capability fallbacks, using only observed tool returns."""
import json
import re
from pathlib import Path

from ...storage import atomic
from ..common import pointer
from .guard import digest


def evidence_pointer(value,path):
    """Accept RFC6901 or a small non-evaluating JSONPath subset; no expressions."""
    if path.startswith('$'):
        rest=path[1:];tokens=[]
        while rest:
            match=re.match(r'(?:\.([A-Za-z_][A-Za-z0-9_-]*)|\[([0-9]+)\])',rest)
            if not match:raise ValueError('Use JSON Pointer or simple $.field[index]; filters and expressions are not supported')
            tokens.append(match.group(1) if match.group(1) is not None else match.group(2));rest=rest[match.end():]
        path=''.join('/'+token.replace('~','~0').replace('/','~1') for token in tokens)
    return pointer(value,path)


def catalog(actions=None):
    props={name:{'type':'string'} for name in ('tool','request_quote','target_call_id','target_path',
           'source_call_id','source_path','source_quote','selection_reason')}
    props['arguments']={'type':'object'}
    if actions:props['tool']['enum']=actions
    return {'name':'capability_prepare','description':
        'Bind a supported write to the current request, a current target read, and actual source evidence. '
        'Use source_call_id from runtime_plugin_receipt, NOT a model call ID. target_path selects the target object '
        'in its tool result; source_path selects a source object/string. Paths use JSON Pointer: empty string for '
        'the result root, /sessions/0/messages/1/content for a nested field. request_quote and source_quote must be '
        'verbatim. Explain why this source applies, including prior-vs-latest intent and conflicting records. '
        'Use bare tool names listed in the tool enum, not mcp__benchmark__ names. Paths address the parsed '
        'business result, not the MCP content/text envelope. Preparation does not execute an action or prove '
        'semantic correctness. If prepared=false, fix the reported issue. Otherwise call the original tool with EXACT arguments.',
        'inputSchema':{'type':'object','properties':props,'required':list(props),'additionalProperties':False}}


class EvidencePreparation:
    def __init__(self,guard):
        self.guard=guard
        self.path=Path(guard.config['events']).with_name('capability-preparations.json')
        self.select=evidence_pointer if guard.spec['kind'] in ('capability-guard-v4','capability-guard-v5') else pointer

    def events(self):
        p=Path(self.guard.config['events'])
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def state(self):return json.loads(self.path.read_text()) if self.path.exists() else {}

    def read(self,events,call_id):
        matches=[e for e in events if str(e.get('call_id'))==str(call_id)]
        if len(matches)!=1:raise ValueError('Evidence ID must identify exactly one observed tool call')
        e=matches[0]
        if e.get('error') or e['name'] not in self.guard.spec['read_tools']:
            raise ValueError('Evidence must be a successful authorized read')
        if isinstance(e.get('result'),dict) and (e['result'].get('error') or e['result'].get('status') in ('error','failed')):
            raise ValueError('An application error is not source evidence')
        if e['name']=='memory' and e['arguments'].get('action')!='read':
            raise ValueError('A memory write is not source evidence')
        return e

    def prepare(self,args,call_id):
        tool,arguments=args['tool'],args['arguments']
        fields=self.guard.spec['target_fields']
        if tool not in fields:raise ValueError('Prepare only configured business actions')
        check=self.guard.check(tool,arguments)
        if not check.get('arguments_valid'):raise ValueError(json.dumps(check))
        quote=args['request_quote']
        if not quote.strip() or quote not in self.guard.config.get('public_task',''):
            raise ValueError('request_quote must be verbatim from the current public task')
        if not args['selection_reason'].strip():raise ValueError('Explain source applicability and conflicting records')
        events=self.events();target=self.read(events,args['target_call_id']);source=self.read(events,args['source_call_id'])
        if target['name'] in ('session_search','Skill','memory'):
            raise ValueError('Read the current business target, not only historical context')
        target_value=self.select(target['result'],args['target_path'])
        if not isinstance(target_value,dict):raise ValueError('target_path must select a current target object')
        if pointer(target_value,fields[tool])!=pointer(arguments,fields[tool]):
            raise ValueError('Current target identity does not match planned action')
        source_value=self.select(source['result'],args['source_path'])
        def strings(v):
            if isinstance(v,str):yield v
            elif isinstance(v,dict):
                for x in v.values():yield from strings(x)
            elif isinstance(v,list):
                for x in v:yield from strings(x)
        if not args['source_quote'].strip() or not any(args['source_quote'] in s for s in strings(source_value)):
            raise ValueError('source_quote is not present in the selected real result')
        # An old target read cannot authorize a later write after the object changed.
        for e in events[events.index(target)+1:]:
            if e.get('name') in fields and not e.get('error'):
                if pointer(e['arguments'],fields[e['name']])==pointer(arguments,fields[tool]):
                    raise ValueError('Target changed after this read; read back its current state first')
        key=digest({'tool':tool,'arguments':arguments});state=self.state()
        state[key]={'preparation_call_id':str(call_id),'tool':tool,'arguments':arguments,
                    'request_quote':quote,'target_call_id':str(args['target_call_id']),
                    'target_path':args['target_path'],
                    'target_result_sha256':digest(target['result']),'source_call_id':str(args['source_call_id']),
                    'source_result_sha256':digest(source['result']),'source_path':args['source_path'],
                    'source_quote':args['source_quote'],'selection_reason':args['selection_reason']}
        atomic(self.path,state)
        self.guard.record('evidence_prepared',executor_call_id=str(call_id),preparation_key=key,**state[key])
        return {'prepared':True,'preparation_key':key,'tool':tool,'arguments':arguments,
                'operation_executed':False,'missing_capability_fulfilled':False,'semantic_correctness_verified':False,
                'next':'Call the original tool with these exact arguments. Preserve full qualifiers in the requested final output.'}

    def before(self,tool,args,call_id):
        self.check_before(tool,args,call_id)
        self.commit_before(tool,args,call_id)

    def check_before(self,tool,args,call_id):
        key=digest({'tool':tool,'arguments':args});state=self.state();prepared=state.get(key)
        if not prepared:
            raise ValueError('Call capability_prepare before this action, with current target read and applicable original source evidence')
        if prepared.get('consumed_by'):
            raise ValueError('Preparation already consumed; read back state and prepare again before retrying')
        events=self.events();target=self.read(events,prepared['target_call_id']);fields=self.guard.spec['target_fields']
        for event in events[events.index(target)+1:]:
            if event.get('name') in fields and not event.get('error'):
                if pointer(event['arguments'],fields[event['name']])==pointer(args,fields[tool]):
                    raise ValueError('Target changed since the preparation evidence; read back and prepare again')
    def commit_before(self,tool,args,call_id):
        key=digest({'tool':tool,'arguments':args});state=self.state();prepared=state[key]
        prepared['consumed_by']=str(call_id);atomic(self.path,state)
        self.guard.record('prepared_action_dispatched',executor_call_id=str(call_id),preparation_key=key,
                          preparation_call_id=prepared['preparation_call_id'],tool=tool,arguments_sha256=digest(args))

    def abort_before(self,tool,args,call_id):
        key=digest({'tool':tool,'arguments':args});state=self.state();prepared=state.get(key,{})
        if prepared.get('consumed_by')==str(call_id):
            del prepared['consumed_by'];atomic(self.path,state)
            self.guard.record('dispatch_reservation_cancelled',executor_call_id=str(call_id),preparation_key=key)

    def manifest(self,event):
        """Pointers into the same bounded public result the executor receives."""
        from ...bounded_mcp import bounded_result
        value=event['result']
        if event['name']=='session_search' and isinstance(value,dict) and isinstance(value.get('sessions'),list):
            value=bounded_result(value)
        objects=[];quotes=[];bindings=set(self.guard.spec['target_fields'].values())
        def visit(v,path='',depth=0):
            if depth>6:return
            if isinstance(v,dict):
                if event['name'] not in ('session_search','Skill','memory'):
                    for binding in bindings:
                        try:identity=pointer(v,binding)
                        except (KeyError,TypeError,ValueError,IndexError):continue
                        if len(objects)<12:objects.append({'target_path':path,'identity_path':binding,'identity':identity})
                for k,item in v.items():visit(item,path+'/'+str(k).replace('~','~0').replace('/','~1'),depth+1)
            elif isinstance(v,list):
                for i,item in enumerate(v[:12]):visit(item,path+'/'+str(i),depth+1)
            elif isinstance(v,str) and path.endswith('/content') and 0<len(v)<=700 and len(quotes)<12:
                quotes.append({'source_path':path,'source_quote':v})
        visit(value)
        return {'source_call_id':str(event['call_id']),'paths_address':'parsed tool result; not MCP content/text wrapper',
                'target_objects':objects,'quote_examples':quotes,'examples_complete':False,
                'selection_is_executor_responsibility':True}
