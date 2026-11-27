"""Run only inside a pinned PAST-Bench Python environment (JSONL RPC)."""
import json
import hashlib
from pathlib import Path
import queue
import sys
import threading
import traceback
from uuid import uuid4

ACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'tool_calls': {'type': 'array', 'items': {'type': 'object',
        'properties': {'name': {'type': 'string'}, 'arguments': {'type': 'object'}},
        'required': ['name', 'arguments'], 'additionalProperties': False}},
        'final_output': {'type': ['string', 'null']}},
    'required': ['tool_calls', 'final_output'],
}


class Session:
    def __init__(self):
        self.events, self.actions = queue.Queue(), queue.Queue()
        self.thread = None
        self.result = None

    def reset(self, family, parameters, instance_indices, seed):
        if self.thread is not None or instance_indices is not None:
            raise ValueError('one original PAST-Bench task per worker')
        if seed != 0:
            raise ValueError('this upstream exposes no seed; select its native default (0)')
        import past_bench
        expected = Path.cwd() / 'src' / 'past_bench'
        installed = Path(past_bench.__file__).resolve().parent
        def source_files(root):
            return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in root.rglob('*') if path.is_file() and '__pycache__' not in path.parts
                    and path.suffix not in {'.pyc', '.pyo'}}
        expected_files = source_files(expected)
        if not expected_files or source_files(installed) != expected_files:
            raise ValueError('worker package bytes differ from the pinned PAST-Bench source')
        (Path(sys.argv[1]) / 'runtime-source.json').write_text(json.dumps({
            'expected_source': str(expected), 'installed_source': str(installed), 'files': expected_files}, indent=2))
        self.thread = threading.Thread(target=self._run, args=(parameters,), daemon=True)
        self.thread.start()
        return self._next()

    def _next(self):
        kind, value = self.events.get(timeout=3600)
        if kind == 'failed':
            raise RuntimeError(value)
        if kind == 'finished':
            self.result = value
            return {'current': None, 'done': True}
        public = {key: value[key] for key in ('messages', 'tools', 'tool_results')}
        return {'current': {'query': json.dumps(public, ensure_ascii=False), 'instance_id': value['task_id'],
                            'instance_index': 0, 'response_schema': ACTION_SCHEMA}, 'done': False}

    def _run(self, parameters):
        try:
            from past_bench import cli
            from past_bench.runner import agent_orchestrator
            from past_bench.runtime.protocol import (BootstrapResponse, StartSessionResponse, StepResponse,
                                                     ToolCallAction)
            from past_bench.models.content import TextBlock, ToolUseBlock
            from past_bench.models.message import Message
            outer = self

            class Client:
                def start(self, **kwargs):
                    pass
                def stop(self):
                    pass
                def bootstrap(self, request):
                    return BootstrapResponse(agent_name=request.agent_name, already_present=True)
                def start_session(self, request):
                    self.request = request
                    return StartSessionResponse(session_id=request.session_id, agent_name=request.agent_name,
                                                adapter='skill-rsi-public-actions')
                def step(self, request):
                    start = self.request
                    outer.events.put(('query', {'task_id': start.task_id,
                        'messages': [m.model_dump(mode='json') for m in start.initial_messages] if request.step_id == 0 else [],
                        'tools': [t.model_dump(mode='json') for t in start.tools] if request.step_id == 0 else [],
                        'tool_results': [t.model_dump(mode='json') for t in request.tool_results]}))
                    action = outer.actions.get(timeout=start.timeout_seconds)
                    if action is None:
                        raise RuntimeError('framework host disconnected')
                    calls = [ToolCallAction(tool_use_id=uuid4().hex, **item) for item in action['tool_calls']]
                    if any(call.name not in {tool.name for tool in start.tools} for call in calls):
                        raise ValueError('action uses an unavailable native tool')
                    blocks = [ToolUseBlock(id=c.tool_use_id, name=c.name, input=c.arguments) for c in calls]
                    if action['final_output'] is not None:
                        blocks.append(TextBlock(text=action['final_output']))
                    return StepResponse(status='acting' if calls else 'finished', tool_calls=calls,
                        assistant_message=Message(role='assistant', content=blocks), final_output=action['final_output'])
                def close_session(self, request):
                    pass

            agent_orchestrator.create_runtime_client = lambda *a, **k: Client()
            registry = Path(sys.argv[1]).resolve() / 'registry.yaml'
            registry.write_text(json.dumps({'agents': {'skill-rsi': {'adapter': 'openai_compat_chat',
                'default_model': {'model_id': 'framework-managed'}}}}))
            trace_dir = Path(sys.argv[1]).resolve() / 'native'
            trace_dir.mkdir()
            result = cli._run_single_task(task_dir=parameters['task'], config_path=parameters['config'],
                model='framework-managed', api_key=None, base_url=None, trace_dir=str(trace_dir),
                port_offset=parameters['port_offset'], no_judge=not parameters['judge'], judge_model=None,
                trials=1, sandbox=parameters['sandbox'], agent='skill-rsi', runtime='local', registry=str(registry))
            (Path(sys.argv[1]).resolve() / 'result.json').write_text(json.dumps(result, ensure_ascii=False))
            if result.get('error') or len(result.get('trials', [])) != 1 or result['trials'][0].get('error'):
                print(json.dumps({'error': result.get('error'),
                    'trial_errors': [r.get('error') for r in result.get('trials', [])]}, ensure_ascii=False), file=sys.stderr)
                raise RuntimeError('original PAST-Bench runtime or grader failed; see worker.stderr')
            trial = result['trials'][0]
            outer.events.put(('finished', {'instance_outcomes': [{'instance_id': result['task_id'],
                'instance_index': 0, 'reward': trial['task_score'], 'success': trial['passed'],
                'raw_metric_name': 'task_score', 'raw_metric_value': trial['task_score'],
                'raw_metric_higher_is_better': True}]}))
        except BaseException as exc:
            traceback.print_exc(file=sys.stderr)
            self.events.put(('failed', type(exc).__name__))

    def step(self, action):
        if not isinstance(action, dict) or set(action) != {'tool_calls', 'final_output'}:
            raise ValueError('expected native tool_calls and final_output')
        if not isinstance(action['tool_calls'], list) or not (
            action['final_output'] is None or isinstance(action['final_output'], str)
        ) or not action['tool_calls'] and action['final_output'] is None:
            raise ValueError('action requires tools or an explicit final answer')
        for call in action['tool_calls']:
            if (not isinstance(call, dict) or set(call) != {'name', 'arguments'}
                    or not isinstance(call['name'], str) or not isinstance(call['arguments'], dict)):
                raise ValueError('invalid native tool action')
        self.actions.put(action)
        response = self._next()
        response.update(action=action, observation={'content': 'Native tool feedback follows in the next query.',
                                                   'instance_complete': response['done']})
        return response

    def score(self):
        if self.result is None:
            raise ValueError('cannot score an unfinished original task')
        return self.result

    def close(self):
        self.actions.put(None)
        if self.thread:
            self.thread.join(timeout=5)


def main():
    session, output = Session(), sys.stdout
    sys.stdout = sys.stderr
    try:
        for raw in sys.stdin:
            try:
                request = json.loads(raw)
                if request['method'] not in {'reset', 'step', 'score'}:
                    raise ValueError('unknown method')
                result = getattr(session, request['method'])(**request['params'])
                payload = {'result': result}
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                payload = {'error': type(exc).__name__}
            print(json.dumps(payload, ensure_ascii=False, allow_nan=False), file=output, flush=True)
    finally:
        session.close()


if __name__ == '__main__':
    main()
