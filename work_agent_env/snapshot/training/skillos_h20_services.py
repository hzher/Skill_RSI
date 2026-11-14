"""Future H20 service lifecycle and actual loaded-weight proof collection.

Planning/import never starts a process. Only start()/stop() have process effects.
"""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.parse import urlparse
import uuid

from integrations.benchmark_tasks.contracts import digest
from training.skillos_h20_config import run_id as validate_run_id
from training.skillos_h20_sources import Sources
from training.skillos_h20_state import atomic, checked, file_ref, read


def process_identity(pid):
    proc = Path('/proc') / str(pid)
    try:
        stat = (proc / 'stat').read_text().split(') ', 1)[1].split()
        return {'pid': pid, 'starttime': stat[19], 'uid': proc.stat().st_uid, 'state': stat[0]}
    except FileNotFoundError:
        return None


class Services:
    def __init__(self, config, run_id, *, project_root):
        validate_run_id(run_id)
        self.config, self.name, self.project = config, run_id, Path(project_root)
        self.root = Path(config['services_root']) / run_id
        self.receipt = self.root / 'services.json'

    def roles(self, policy_version, weights):
        config = deepcopy(read(self.config['roles_file']))
        devices = self.config['gpu']['physical_ids']
        for name, local in {'curator': [0], 'selfjudge': [1], 'contentjudge': [2, 3]}.items():
            role = config['roles'][name]
            role['gpus'] = [devices[i] for i in local]
            role['model_path'] = str((self.project / role['model_path']).absolute())
            role['served_model'] = 'skillos-' + name + '-' + self.name + '-' + (policy_version if name == 'curator' else 'frozen')
        config['roles']['curator']['model_path'] = str(Path(weights).absolute())
        config['roles']['curator']['model_revision'] = policy_version
        return config

    def command(self, name, role):
        port = urlparse(role['base_url']).port
        if role['backend'] == 'vllm':
            return ['bash', str(self.project / 'scripts/generator_python.sh'), '-m', 'vllm.entrypoints.openai.api_server',
                '--model', role['model_path'], '--served-model-name', role['served_model'], '--host', '127.0.0.1',
                '--port', str(port), '--dtype', 'bfloat16', '--generation-config', 'vllm',
                '--max-model-len', str(role['context_limit']), '--max-num-seqs', '1',
                '--max-num-batched-tokens', '2048', '--enforce-eager', '--gpu-memory-utilization', str(role['gpu_memory_utilization']),
                '--worker-cls', 'training.skillos_h20_worker_model.H20PolicyFingerprintWorker',
                '--logprobs-mode', 'raw_logprobs']
        return [str(self.project / '.runtime/skillos-content-venv/bin/python'),
                str(self.project / 'scripts/serve_skillos_content.py'), '--model', role['model_path'],
                '--served-model', role['served_model'], '--port', str(port)]

    def stop(self):
        if not self.receipt.exists():
            return
        values = read(self.receipt)
        for name, service in values['services'].items():
            identity = process_identity(service['pid'])
            if identity is None or identity['state'] == 'Z':
                continue
            if identity['uid'] != os.getuid() or identity['starttime'] != service['starttime']:
                raise ValueError('Recorded service PID identity changed; no signal sent')
            os.killpg(service['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 120
        while any((current := process_identity(service['pid'])) is not None and current['state'] != 'Z'
                  and current['starttime'] == service['starttime'] for service in values['services'].values()):
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned service has not stopped; no forced kill or actor launch')
            time.sleep(.5)

    @staticmethod
    def assert_bindings(reference):
        binding = checked(reference)
        for name, proof_ref in binding['proofs'].items():
            proof = checked(proof_ref)
            current = process_identity(proof['process']['pid'])
            if (current is None or current['state'] == 'Z' or current['uid'] != os.getuid()
                    or current['starttime'] != proof['process']['starttime']):
                raise ValueError('Actual role process changed before request: ' + name)
            checked(proof['worker'])

    def start(self, policy_version, weights):
        roles = self.roles(policy_version, weights)
        self.root.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex
        directory = self.root / (policy_version + '-' + suffix)
        directory.mkdir()
        deployment = directory / 'deployment.json'
        atomic(deployment, self.config, new=True)
        role_config = directory / 'roles.json'
        atomic(role_config, roles, new=True)
        # Refuse occupied service ports, including old services not owned here.
        for role in roles['roles'].values():
            target = urlparse(role['base_url'])
            with socket.socket() as probe:
                probe.settimeout(.5)
                if probe.connect_ex((target.hostname, target.port)) == 0:
                    raise ValueError('Role port is occupied; do not adopt/stop an unowned service')
        self.require_idle_h20()
        services = {}
        atomic(self.receipt, {'roles': file_ref(role_config), 'services': services})
        for name, role in roles['roles'].items():
            environment = {**os.environ, 'CUDA_VISIBLE_DEVICES': ','.join(map(str, role['gpus'])),
                'SKILLOS_H20_DEPLOYMENT': str(deployment), 'SKILLOS_ROLE': name,
                'SKILLOS_POLICY_VERSION': policy_version if name == 'curator' else 'frozen',
                'SKILLOS_FINGERPRINT_PATH': str(directory / (name + '-worker.json')),
                'PYTHONPATH': str(self.project) + (':' + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')}
            compatibility = str(self.project / '.runtime/generator-cuda/compat')
            environment['LD_LIBRARY_PATH'] = compatibility + (':' + environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
            environment['VLLM_USE_V1'] = '1'
            with (directory / (name + '.log')).open('xb') as stream:
                process = subprocess.Popen(self.command(name, role), cwd=self.project, env=environment,
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            identity = process_identity(process.pid)
            if identity is None:
                raise RuntimeError('Role process exited before identity capture')
            services[name] = {**identity, 'command': self.command(name, role), 'gpus': role['gpus'],
                              'worker_proof': str(directory / (name + '-worker.json'))}
            atomic(self.receipt, {'roles': file_ref(role_config), 'services': services})
        self._wait_ready(roles, services)
        return role_config

    def require_idle_h20(self):
        # Called only by explicit future startup, never by command planning.
        visible = ','.join(map(str, self.config['gpu']['physical_ids']))
        rows = subprocess.check_output(['nvidia-smi', '-i', visible,
            '--query-gpu=index,uuid,name,memory.total,memory.used', '--format=csv,noheader,nounits'], text=True)
        devices = []
        for line in rows.splitlines():
            index, uuid, name, memory, used = [value.strip() for value in line.split(',')]
            if (int(index) not in self.config['gpu']['physical_ids'] or 'H20' not in name
                    or int(memory) < self.config['gpu']['minimum_memory_mib'] or int(used) >= 512):
                raise ValueError('Selected H20 device is missing/occupied/too small; no process stopped')
            devices.append(uuid)
        if len(set(devices)) != 4 or len(devices) != 4:
            raise ValueError('Expected four complete distinct H20 devices')
        processes = subprocess.check_output(['nvidia-smi', '-i', visible, '--query-compute-apps=gpu_uuid,pid',
                                            '--format=csv,noheader'], text=True)
        if any(line.split(',')[0].strip() in devices for line in processes.splitlines() if ',' in line):
            raise ValueError('Selected H20 is in use; no process stopped')

    def _wait_ready(self, roles, services):
        import httpx
        deadline = time.monotonic() + self.config['training']['phase_timeout_seconds']
        with httpx.Client(timeout=5, trust_env=False) as client:
            pending = set(services)
            while pending:
                for name in list(pending):
                    service, role = services[name], roles['roles'][name]
                    current = process_identity(service['pid'])
                    if current is None or current['state'] == 'Z' or current['starttime'] != service['starttime']:
                        raise RuntimeError('Role process exited during startup: ' + name)
                    try:
                        response = client.get(role['base_url'] + '/models')
                        response.raise_for_status()
                        model = next(row for row in response.json()['data'] if row['id'] == role['served_model'])
                        if Path(model['root']).resolve() != Path(role['model_path']).resolve():
                            raise ValueError('Served role model root differs')
                        if name == 'contentjudge' and model.get('structured_outputs') != 'xgrammar_after_thinking':
                            raise ValueError('ContentJudge lacks actual structured decoding')
                        if name != 'contentjudge' and not Path(service['worker_proof']).is_file():
                            continue
                        pending.remove(name)
                    except (httpx.HTTPError, StopIteration):
                        pass
                if pending:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Roles not ready: ' + ','.join(sorted(pending)))
                    time.sleep(1)

    def bind(self, *, lineage, fixed_executor_id):
        import httpx
        source = Sources(self.config)
        reference_reader = source.load('runtime/weight-sync-manager-v3/cpu_fingerprint.py', extra=True)
        record = read(self.receipt)
        roles = checked(record['roles'])
        directory = Path(record['roles']['path']).parent
        references = {}
        for name, role in roles['roles'].items():
            references[name] = reference_reader.hf_fingerprint(Path(role['model_path']))
        expected = checked(lineage['hf_reference'])
        if references['curator'] != expected:
            raise ValueError('Serving weights differ from native lineage')
        proofs = {}
        with httpx.Client(timeout=1800, trust_env=False) as client:
            for name, role in roles['roles'].items():
                process = record['services'][name]
                current = process_identity(process['pid'])
                if current is None or current['state'] == 'Z' or current['starttime'] != process['starttime']:
                    raise ValueError('Role PID changed before fingerprint binding')
                if name == 'contentjudge':
                    response = client.get(role['base_url'] + '/fingerprint')
                    response.raise_for_status()
                    raw_path = directory / 'contentjudge-worker.json'
                    atomic(raw_path, response.json(), new=True)
                    raw = response.json()
                else:
                    raw_path = Path(process['worker_proof'])
                    raw = read(raw_path)
                if (Path(raw['loaded_model_path']).resolve() != Path(role['model_path']).resolve()
                        or raw['tensors'] != references[name]):
                    raise ValueError('Actual loaded role weights differ from independent HF tensors: ' + name)
                proof = {'role': name, 'model_revision': role['model_revision'],
                    'actual_parameters_verified': True, 'tensors': raw['tensors'],
                    'worker': file_ref(raw_path), 'process': current, 'model_path': role['model_path']}
                path = directory / (name + '-verified.json')
                atomic(path, proof, new=True)
                proofs[name] = file_ref(path)
        initial = directory / 'initial-hf-reference.json'
        atomic(initial, references['selfjudge'], new=True)
        bindings = {'schema_version': 'workagent-skillos-role-bindings-v1', 'policy_version': lineage['policy_version'],
            'fixed_executor_id': fixed_executor_id, 'roles_config_sha256': digest(roles),
            'roles_config': record['roles'], 'proofs': proofs, 'initial_hf_reference': file_ref(initial),
            'content_schema_enforced': True, 'role_versions': {name: role['model_revision']
                for name, role in roles['roles'].items()}}
        path = directory / 'role-bindings.json'
        atomic(path, bindings, new=True)
        return path
