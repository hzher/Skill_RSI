"""Optional Harbor installed agent using the same managed Codex gateway adapter."""
import json
import os
from pathlib import Path
import shlex

from harbor.agents.installed.codex import Codex


class SkillRSICodex(Codex):
    # The canonical trajectory is Skill RSI's source-backed archive. Re-parsing
    # an entire resumed rollout at every step would double-count its prefix.
    SUPPORTS_ATIF = False

    def __init__(self, *args, relay_url_env='SKILL_RSI_RELAY_URL', uid=None,
                 run_timeout=1800, reasoning_effort='high', **kwargs):
        self._relay_url = os.environ[relay_url_env]
        self._uid = int(uid if uid is not None else os.getuid())
        self._run_timeout = float(run_timeout)
        self._reasoning = reasoning_effort
        self._resume = None
        self._ordinal = 0
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return 'codex-skill-rsi'

    def get_version_command(self):
        return '/opt/skill-rsi-codex/bin/codex --version'

    def populate_context_post_run(self, context):
        return None

    async def install(self, environment):
        user = str(getattr(environment, 'default_user', None) or 'skillrsi')
        if user in {'root', '0'}:
            raise ValueError('The benchmark Codex agent requires a non-root user')
        quoted = shlex.quote(user)
        command = (
            f'if ! id {quoted} >/dev/null 2>&1; then '
            f'if command -v useradd >/dev/null 2>&1; then useradd -m -u {self._uid} -s /bin/bash {quoted}; '
            f'else adduser -D -u {self._uid} -s /bin/sh {quoted}; fi; fi; '
            f'test "$(id -u {quoted})" = {self._uid} && '
            f'mkdir -p /workspace /logs/agent && chown -R {quoted} /workspace && '
            '/opt/skill-rsi-python/bin/python3 --version && /opt/skill-rsi-codex/bin/codex --version')
        result = await self.exec_as_root(environment, command=command)
        if result.return_code:
            raise RuntimeError('Native Codex runtime preparation failed: ' + (result.stderr or '')[-1000:])

    async def run(self, instruction, environment, context):
        current = await self.exec_as_agent(environment, command='pwd')
        project = (current.stdout or '/workspace').strip()
        if current.return_code or not project.startswith('/'):
            raise RuntimeError('Benchmark did not provide a working directory')
        ordinal = self._ordinal
        self._ordinal += 1
        name = f'capture-{ordinal:04d}'
        public = {'instruction': self.render_instruction(instruction), 'project': project,
            'relay_url': self._relay_url, 'model': self.model_name, 'resume': self._resume,
            'timeout': self._run_timeout, 'reasoning_effort': self._reasoning,
            'output': '/logs/agent/skill-rsi/' + name, 'state_dir': '/logs/agent/skill-rsi-state'}
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        local_config = self.logs_dir / f'launch-input-{ordinal:04d}.json'
        local_config.write_text(json.dumps(public, ensure_ascii=False))
        remote = '/logs/agent/' + local_config.name
        if not getattr(environment.capabilities, 'mounted', False):
            await environment.upload_file(local_config, remote)
        env = {'PYTHONPATH': '/opt/skill-rsi', 'CODEX_HOME': '/logs/agent/codex-home',
               'SKILL_RSI_RELAY_KEY': 'local-relay-only',
               'PATH': '/opt/skill-rsi-codex/bin:/opt/skill-rsi-codex/codex-path:/usr/local/bin:/usr/bin:/bin'}
        command = shlex.join(['/opt/skill-rsi-python/bin/python3', '-m',
                              'skill_rsi.adapters.codex.container_run', '--config', remote])
        result = await self.exec_as_agent(environment, command=command, env=env,
                                           timeout_sec=int(self._run_timeout + 30))
        if not getattr(environment.capabilities, 'mounted', False):
            await environment.download_dir('/logs/agent', self.logs_dir)
        launch_path = self.logs_dir / 'skill-rsi' / name / 'launch.json'
        if launch_path.is_file():
            launch = json.loads(launch_path.read_text())
            self._resume = launch.get('session_id') or self._resume
            usage = launch.get('usage') or {}
            context.n_input_tokens = usage.get('input_tokens')
            context.n_output_tokens = usage.get('output_tokens')
            context.n_cache_tokens = usage.get('cached_input_tokens')
        sessions = self.logs_dir / 'sessions'
        if not sessions.exists() and (self.logs_dir / 'codex-home' / 'sessions').exists():
            sessions.symlink_to('codex-home/sessions', target_is_directory=True)
        if result.return_code:
            raise RuntimeError('Managed Codex did not complete; its capture and native errors are preserved')
        self.populate_context_post_run(context)
