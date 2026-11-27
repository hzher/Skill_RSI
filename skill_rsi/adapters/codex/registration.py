"""Project-scoped registration for managed Responses launches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from ...runtime import project_identity
from ..base import host_record, read_optional
from ..install import _Bridge, _apply, _lock, _scope_options, _settings, _settings_path


LAUNCH_OPTIONS = ('api', 'baseline_tools', 'native_plugin', 'native_plugin_install',
                  'query_source', 'model', 'args', 'timeout', 'profile', 'observe', 'public_actions')


def validate_api(api, project):
    if not isinstance(api, dict) or set(api) - {'upstream', 'credential_env', 'timeout', 'constraints',
                                              'request_max_retries', 'stream_max_retries', 'proxy_mode'}:
        raise ValueError('api accepts upstream, credential_env, timeout, constraints, retry limits and proxy_mode')
    upstream = api.get('upstream')
    if not isinstance(upstream, str):
        raise ValueError('api.upstream requires an HTTP(S) URL')
    parsed = urlsplit(upstream)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('api.upstream requires an HTTP(S) URL without embedded credentials or query')
    parsed.port  # Validate malformed ports before configuration or plugin writes.
    if api.get('proxy_mode', 'environment') not in {'environment', 'direct'}:
        raise ValueError('api.proxy_mode must be environment or direct')
    name = api.get('credential_env')
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)):
        raise ValueError('api.credential_env requires an environment variable name')
    timeout = api.get('timeout', 900)
    if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
        raise ValueError('api.timeout must be in (0, 86400] seconds')
    for field in ('request_max_retries', 'stream_max_retries'):
        if field in api and (type(api[field]) is not int or not 0 <= api[field] <= 8):
            raise ValueError('api.' + field + ' must be an integer in [0, 8]')
    _scope_options(project, {}, {'constraints': api.get('constraints', [])})
    return dict(api)


def install(adapter, project, state_dir, **options):
    project, state_dir = Path(project).resolve(strict=True), Path(state_dir).resolve()
    if not project.is_dir():
        raise ValueError('Expected a project directory')
    target = host_record(project, state_dir, adapter.name)
    with _lock(_settings_path(project, '.skill-rsi-install.lock')):
        previous = read_optional(target) or {}
        defaults = previous.get('launch_options', {})
        selected = {key: options.get(key, defaults[key]) if key in defaults else options[key]
                    for key in LAUNCH_OPTIONS if key in options or key in defaults}
        selected['api'] = validate_api(selected.get('api'), project)
        selected.setdefault('native_plugin', not selected.get('public_actions', False))
        selected.setdefault('native_plugin_install', selected['native_plugin'])
        selected.setdefault('baseline_tools', not selected.get('public_actions', False))
        for key in ('native_plugin', 'native_plugin_install', 'baseline_tools', 'observe', 'public_actions'):
            if key in selected and type(selected[key]) is not bool:
                raise ValueError(f'{key} must be true or false')
        if selected['native_plugin_install'] and not selected['native_plugin']:
            raise ValueError('native_plugin_install requires native_plugin')
        if selected.get('public_actions') and (selected['native_plugin'] or selected['baseline_tools']):
            raise ValueError('public_actions requires disabled native plugins and baseline tools')
        if 'args' in selected and (not isinstance(selected['args'], list) or
                not all(isinstance(arg, str) for arg in selected['args'])):
            raise ValueError('args must be an argv list')
        command = options.get('command', previous.get('command', adapter.command))
        if not isinstance(command, str) or not command.strip() or '\x00' in command:
            raise ValueError('command requires a native CLI executable name or path')
        if os.sep in command:
            command = str((project / Path(command).expanduser()).resolve())
        identity = {'adapter_version': adapter.version, 'bridge_python': sys.executable,
                    'command': command, 'transport': 'gateway', 'launch_options': selected}
        owner = previous.get('registration_id')
        if not owner or any(previous.get(key) != value for key, value in identity.items()):
            owner = uuid4().hex
        entry = {**identity, 'registration_id': owner, 'host': adapter.name, 'project': str(project),
                 'project_id': project_identity(project), 'observe': selected.get('observe', previous.get('observe', False)),
                 'phase': 'registered', 'activation_boundary': 'managed_gateway_launch',
                 'native_host_version': adapter.probe(project, command=command).get('host_version'),
                 'journal': str(target.parent / 'journal'),
                 **_scope_options(project, previous, options)}
        updates = []
        if previous and previous.get('transport') != 'gateway':
            # Remove only the previous registration's exact owned MCP/hook entries.
            bridge = _Bridge(adapter.name, project, state_dir, previous['registration_id'], sys.executable)
            updates.extend(_settings(bridge, previous, remove=True))
        updates.append((target, (json.dumps(entry, ensure_ascii=False, indent=2) + '\n').encode()))
        _apply(updates)
        return entry


def uninstall(adapter, project, state_dir):
    project = Path(project).resolve(strict=True)
    target = host_record(project, state_dir, adapter.name)
    with _lock(_settings_path(project, '.skill-rsi-install.lock')):
        entry = read_optional(target)
        if not entry:
            return {'host': adapter.name, 'phase': 'absent'}
        if entry.get('transport') != 'gateway':
            raise ValueError('Expected a gateway registration')
        target.unlink()
        (target.parent / 'gateway.json').unlink(missing_ok=True)
        return {'host': adapter.name, 'phase': 'unregistered', 'registration_id': entry['registration_id']}
