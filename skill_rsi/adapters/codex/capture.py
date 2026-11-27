"""Explicit API/MCP capture, using the shared runtime and unmodified wire recorder."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

from ...components.manifest import canonical, sha256
from ...evidence.archive import write
from ...runtime import Runtime, RuntimeEvent
from ..base import HostAdapter, host_record, read_optional
from .gateway import CodexGateway, RequestBoundary
from .components import native_digest
from ._native.plugins import initial_context
from ._native.plugins.registry import validate_plugins


def toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(toml(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)


class CaptureBoundary:
    """Capabilities exist only at this adapter-owned launch, never at arbitrary hooks."""
    def __init__(self, adapter, *, mcp=False):
        self.adapter = adapter
        self.mcp = mcp
        self.skills, self.plugins, self.operations = [], [], []

    def capabilities(self, session=None):
        caps = ['append_context', 'suggest', 'execute_native_component'] + (['load_plugin'] if self.mcp else [])
        return {'host': 'codex', 'adapter_version': self.adapter.version,
                'boundaries': ['capture_start'], 'nodes': {'capture_start': caps},
                'capabilities': caps, 'reload_boundary': 'capture_start'}

    def propose_native(self, context, engine):
        if engine != 'native' or context.release.form != 'plugin':
            raise ValueError('Capture supports only its declared Codex-native plugin format')
        value = json.loads(context.artifact_file(context.release.manifest['entrypoint']))
        if value.get('native_code_digest') != native_digest():
            return {'reason': 'native_implementation_digest_changed'}
        spec = value['specification']
        validate_plugins([spec])
        if context.options:
            raise ValueError('Export a new release to change a frozen native plugin specification')
        return {'reason': 'native_mcp_plugin_selected', 'action': {
            'kind': 'load_plugin', 'target': context.release.manifest['action']['target'],
            'payload': {'specification': spec, 'artifact_digest': context.release.digest},
        }}

    def deliver(self, action, key):
        payload = action['payload']
        if action['kind'] == 'append_context':
            self.skills.append(payload['text'])
        elif action['kind'] == 'load_plugin' and self.mcp:
            self.plugins.append(payload['specification'])
            self.operations.append({'operation_key': key, 'artifact_digest': payload['artifact_digest']})
        elif action['kind'] != 'display_suggestion':
            return {'status': 'skipped', 'stage': 'submitted', 'missing_reason': 'capture_action_unavailable'}
        return {'status': 'submitted', 'stage': 'submitted', 'operation_key': key,
                'delivery_scope': 'codex_launch_configuration', 'model_input_visibility': 'unknown'}


def run(adapter, output, command, project, prompt, resume, options):
    if type(options.get('public_actions', False)) is not bool:
        raise ValueError('public_actions must be boolean')
    if options.get('public_actions'):
        if any(options.get(name) for name in ('native_plugin', 'native_plugin_install', 'native_mcp', 'baseline_tools', 'components')):
            raise ValueError('public_actions requires disabled native plugins, MCP and baseline tools')
        options = {**options, 'native_plugin': False, 'native_plugin_install': False, 'baseline_tools': False}
    if command is not None:
        raise ValueError('Managed API/MCP capture requires the adapter-owned Codex command')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Capture requires an explicit public prompt')
    project, output = Path(project).resolve(), Path(output).resolve()
    if options.get('api') is not None:
        from .registration import validate_api
        validate_api(options['api'], project)
    if options.get('native_plugin_install') and (options.get('api') is None or not options.get('native_plugin', True)):
        raise ValueError('native_plugin_install requires an enabled native plugin and api gateway')
    config = {'tools': [], 'skills': {}}
    if options.get('native_mcp'):
        selected = Path(options['native_mcp']).expanduser()
        if not selected.is_absolute():
            selected = project / selected
        config.update(json.loads(selected.read_text()))
        # Plugin selection is controlled by frozen component bindings below.
        if config.get('runtime_plugins'):
            raise ValueError('Register native plugins as form=plugin components')
    runtime = Runtime(project, state_dir=options['state_dir'])
    boundary = CaptureBoundary(adapter, mcp=bool(options.get('native_mcp')))
    for item in options.get('components', []):
        if not isinstance(item, dict) or set(item) - {'source', 'mode', 'options'}:
            raise ValueError('Capture components accept source, mode and options')
        source = item['source']
        if not str(source).startswith('builtin:'):
            source = Path(source).expanduser()
            if not source.is_absolute():
                source = project / source
        release = runtime.registry.install(source)
        capabilities = boundary.capabilities()
        if options.get('api') is not None and 'capture_start' not in release.manifest['trigger']['boundaries']:
            capabilities = RequestBoundary(adapter.version).capabilities()
        runtime.enable('codex', release.component_id, release.version, form=release.form,
                       mode=item.get('mode', 'shadow'), capabilities=capabilities, options=item.get('options'))
    capture_id = uuid4().hex
    event = RuntimeEvent('codex', capture_id, 'capture_start', capture_id, {},
                         input_version=sha256(prompt.encode()), run_id=capture_id)
    decisions = runtime.dispatch(event, boundary)
    config.update(events=str(output / 'mcp-events.jsonl'), plugin_events=str(output / 'plugin-events.jsonl'),
                  runtime_plugins=boundary.plugins, component_bindings=boundary.operations,
                  native_code_digest=native_digest(), public_task=prompt)
    validate_plugins(boundary.plugins)
    addition, _ = initial_context(config, output, prompt, list(config.get('skills', {}).values()), boundary.plugins)
    optional = '\n\n'.join(boundary.skills)
    if optional:
        prompt += '\n\nOptional Skill procedures; current task instructions take precedence:\n' + optional
    prompt += addition
    if options.get('query_source') and options.get('api') is not None:
        from .query import prepare
        prompt = prepare(runtime, prompt, source=options['query_source'],
                         constraints=options['api'].get('constraints', ()), session_id=resume or capture_id,
                         capture_id=capture_id, output=output)
    write(output / 'prompt.json', {'prompt': prompt, 'sha256': sha256(prompt.encode())})
    write(output / 'components.json', {'capture_id': capture_id, 'decisions': [d.to_dict() for d in decisions]})
    args = list(options.get('args', ()))
    env = {}
    if options.get('api') is not None and options.get('native_plugin', True):
        from .native import build
        args += build(output, project=project, state_dir=options['state_dir'], capture_id=capture_id,
                      constraints=options['api'].get('constraints', ()), resume=resume,
                      observe=options.get('observe', True))
    if options.get('native_mcp'):
        write(output / 'mcp.json', config)
        module_root = str(Path(__file__).resolve().parents[3])
        server = {'command': sys.executable, 'args': ['-m', 'skill_rsi.adapters.codex.mcp', str(output / 'mcp.json')],
                  'env': {'PYTHONPATH': module_root}, 'startup_timeout_sec': 30, 'tool_timeout_sec': 90}
        args += ['-c', 'mcp_servers.skill_rsi=' + toml(server)]
    with ExitStack() as stack:
        if options.get('native_plugin_install'):
            from .native import install
            stack.callback(install(output, capture_id=capture_id, command=options.get('command') or adapter.command))
        api = options.get('api')
        if api is not None:
            credential_name = api.get('credential_env')
            key = os.environ[credential_name] if credential_name else None
            if credential_name and not key:
                raise ValueError('API capture credential is empty')
            recorder = CodexGateway(api['upstream'], output / 'wire', credential=key,
                                    project=project, state_dir=options['state_dir'], session_id=resume or capture_id,
                                    run_id=capture_id, constraints=api.get('constraints', ()),
                                    baseline_tools=options.get('baseline_tools') is True,
                                    native_stdout=output / 'stdout.jsonl',
                                    query_source=options.get('query_source'),
                                    public_actions=options.get('public_actions', False),
                                    proxy_mode=api.get('proxy_mode', 'environment'),
                                    timeout=api.get('timeout', options.get('timeout', 900)))
            thread = threading.Thread(target=recorder.serve, daemon=True)
            thread.start()
            def stop():
                recorder.server.shutdown()
                thread.join(timeout=5)
            stack.callback(stop)
            if credential_name:
                env[credential_name] = None
                env['SKILL_RSI_CAPTURE_TOKEN'] = 'local-recorder-only'
            provider = {'name': 'Skill RSI recorded Responses', 'base_url': recorder.base_url + '/v1',
                        'wire_api': 'responses',
                        'request_max_retries': api.get('request_max_retries', 3),
                        'stream_max_retries': api.get('stream_max_retries', 3), 'supports_websockets': False}
            if credential_name:
                provider['env_key'] = 'SKILL_RSI_CAPTURE_TOKEN'
            else:
                provider['requires_openai_auth'] = True
            args += ['-c', 'model_provider="skill_rsi_capture"',
                     '-c', 'model_providers.skill_rsi_capture=' + toml(provider)]
        result = HostAdapter.run(adapter, output, None, project, prompt, resume,
                                 {**options, 'args': args, '_capture_environment': env})
    loaded = output / 'mcp-loaded.json'
    if loaded.is_file():
        receipt = json.loads(loaded.read_text())
        for operation in receipt.get('components', []):
            runtime.record_receipt(operation['operation_key'], {
                'status': 'loaded', 'stage': 'delivered', 'receipt_ref': str(loaded),
                'delivery_scope': 'native_mcp_loaded', 'model_input_visibility': 'unknown',
            })
    result.update(capture_id=capture_id, adapter_version=adapter.version,
                  mcp_status='loaded' if loaded.is_file() else 'unobserved' if options.get('native_mcp') else 'disabled',
                  wire_path=str(output / 'wire') if api is not None else None)
    result['native_plugin_status'] = 'observed' if any((output / 'native-events').glob('*.json')) else 'unobserved'
    if (output / 'native-uninstall.json').is_file():
        cleanup = json.loads((output / 'native-uninstall.json').read_text())
        result['native_plugin_cleanup_status'] = 'confirmed' if all(
            row['returncode'] == 0 for row in cleanup['operations']) else 'failed'
    result['native_task_status'] = result.get('last_turn_status', 'unknown')
    native_events = list((output / 'native-events').glob('*.json'))
    write(output / 'native-health.json', {
        'schema': 'skill-rsi.codex-native-health.v1', 'observed_events': len(native_events),
        'process_closed': result.get('returncode') is not None,
        'task_status': result['native_task_status'], 'unobserved_intervals': 'unknown',
    })
    if api is not None:
        forwards = sorted((output / 'wire').glob('*/forward.json'))
        if forwards:
            write(host_record(project, options['state_dir'], 'codex').parent / 'gateway.json', {
                'project': str(project), 'adapter_version': adapter.version,
                'registration_id': (read_optional(host_record(project, options['state_dir'], 'codex')) or {}).get('registration_id'),
                'host_version': result.get('harness_version'), 'capture_id': capture_id,
                'launch_path': str(output / 'launch.json'), 'forward_path': str(forwards[-1]),
                'delivery_boundary': 'gateway_socket_write', 'active': False,
            })
    write(output / 'launch.json', result)
    write(output / 'components.json', {'capture_id': capture_id,
                                      'decisions': [d.to_dict() for d in runtime.decisions('codex') if d.run_id == capture_id]})
    return result
