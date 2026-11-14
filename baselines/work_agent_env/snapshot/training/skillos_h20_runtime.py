"""H20 GPU actor/fresh validation/export, reusing source204 algorithm bodies.

Construction and contract checks are CPU-only. GPU code is entered exclusively
by an explicitly launched worker after the durable lifecycle stage is SENT.
"""
import ast
from contextlib import contextmanager
import datetime
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import traceback
from types import SimpleNamespace

from integrations.benchmark_tasks.contracts import digest
from training.skillos_h20_config import paths
from training.skillos_h20_sources import Sources, SOURCE, SP
from training.skillos_h20_state import checked, file_ref, read, State, update_uuid


class Runtime:
    def __init__(self, context_file):
        self.context_file = Path(context_file).absolute()
        self.context = read(self.context_file)
        if self.context['schema_version'] != 'workagent-skillos-h20-context-v1':
            raise ValueError('Invalid H20 native context')
        self.config = checked(self.context['config'])
        self.name, self.step = self.context['run_id'], self.context['step']
        self.paths = paths(self.config, self.name, self.step)
        if self.context['paths'] != self.paths or str(self.context_file) != self.paths['context']:
            raise ValueError('Native context moved or paths changed')
        for reference in self.context['source_files']:
            if file_ref(reference['path']) != reference:
                raise ValueError('Runtime source changed after batch preparation')
        self.sources = Sources(self.config)
        self.upstream = self.sources.upstream
        self.state = State(self.paths['work'])
        self.stage = None
        self.model = Path(self.config['model_path'])
        self.modules = None

    @staticmethod
    def require(value, message):
        if not value:
            raise ValueError(message)

    @staticmethod
    def check_role_bindings(bindings, config, lineage, identity):
        if (bindings.get('schema_version') != 'workagent-skillos-role-bindings-v1'
                or bindings['policy_version'] != lineage['policy_version']
                or bindings['fixed_executor_id'] != identity['fixed_executor_id']
                or bindings['roles_config_sha256'] != identity['roles_config_sha256']
                or bindings.get('content_schema_enforced') is not True):
            raise ValueError('Actual role bindings/structured ContentJudge required')
        for name in ('curator', 'selfjudge', 'contentjudge'):
            proof = checked(bindings['proofs'][name])
            if proof.get('role') != name or proof.get('actual_parameters_verified') is not True:
                raise ValueError('Role proof does not verify actual loaded parameters')
        curator = checked(bindings['proofs']['curator'])
        selfjudge = checked(bindings['proofs']['selfjudge'])
        expected = checked(lineage['hf_reference'])
        if curator['tensors'] != expected:
            raise ValueError('Sampled Curator does not match native policy weights')
        base = checked(bindings['initial_hf_reference'])
        if len(base) != 399 or selfjudge['tensors'] != base or len(expected) != 399:
            raise ValueError('Frozen SelfJudge/base tensor identity mismatch')
        if checked(bindings['proofs']['contentjudge'])['model_revision'] != read(config['roles_file'])['roles']['contentjudge']['model_revision']:
            raise ValueError('Frozen ContentJudge revision differs')

    def load_contract(self, args=None):
        from training.skillos_h20_data import check_groups, check_sample_sources
        if (Path(self.paths['batch']).stat().st_size > 2 * 1024**3
                or Path(self.paths['app']).stat().st_size >= 255 * 1024**2):
            raise ValueError('Original native batch/application metadata size bound exceeded')
        batch, app, lineage = (checked(self.context[key]) for key in ('batch', 'app', 'lineage'))
        if (self.context['batch'] != file_ref(self.paths['batch']) or self.context['app'] != file_ref(self.paths['app'])
                or batch['training_admitted'] is not True or batch['engineering'] is not False
                or batch.get('all_infrastructure_valid') is not True or batch['status'] != 'sealed_for_native_actor'
                or app['global_step'] != app['update_index'] or app['global_step'] != self.step
                or app['run_id'] != self.name or app['domain'] != 'Toolathlon'
                or app['training_seed'] != 42 or len(app['applied_update_uuids']) != self.step
                or len(set(app['applied_update_uuids'])) != self.step
                or app['policy_version'] != f'V{self.step:03d}' or app['next_policy_version'] != f'V{self.step+1:03d}'
                or app['phase'] != 'BATCH_SEALED' or app['sealed_batch_sha256'] != self.context['batch']['sha256']
                or app['source_actor_checkpoint'] != self.context['lineage']
                or batch['source_actor_checkpoint'] != self.context['lineage']):
            raise ValueError('Native batch/application state/lineage mismatch')
        identity = checked(self.context['collection_identity'])
        if (batch['source_binding'] != identity or identity['upstream_commit'] != self.config['source_commit']
                or identity['upstream_manifest_sha256'] != digest(self.upstream.manifest)):
            raise ValueError('Collection identity changed')
        check_groups(checked(self.context['admission']), batch['plan'], self.config)
        self.check_role_bindings(checked(self.context['role_bindings']), self.config, lineage, identity)
        rows, totals, summary = self.upstream.actor_contract.validate_batch(batch, app['policy_version'], 32, False)
        self.upstream.actor_contract.validate_application(app, app['policy_version'], self.context['batch']['sha256'])
        self.upstream.actor_contract.verify_reward_sources(batch, rows)
        check_sample_sources(self.upstream, batch)
        if summary != self.context['summary']:
            raise ValueError('Native batch summary changed')
        repos = app['skill_repositories']
        if (len(repos) != 256 or {r['group_rollout'] for r in repos} != {
                r['group_id'] + '/' + str(r['rollout']) for r in rows}
                or any(r['last_committed_position'] != 9 or hashlib.sha256(r['canonical_content'].encode()).hexdigest() != r['sha256']
                       for r in repos)):
            raise ValueError('Incomplete final SkillRepo application state')
        for repo in repos:
            gid, slot = repo['group_rollout'].rsplit('/', 1)
            last = next(row for row in rows if row['group_id'] == gid and row['rollout'] == int(slot) and row['position'] == 9)
            if checked(last['source_record'])['repository_after_sha256'] != repo['sha256']:
                raise ValueError('Application SkillRepo differs from last committed position')
        source = read(self.model / 'deployment_source.json')
        if source['revision'] != self.config['model_revision'] or source.get('weights_lfs_verified') is not True:
            raise ValueError('Incomplete/wrong base model checkpoint')
        for name, metadata in source['files'].items():
            if (self.model / name).stat().st_size != metadata['bytes']:
                raise ValueError('Base model file size changed')
            if not name.endswith(('.safetensors', '.bin')) and file_ref(self.model / name)['sha256'] != metadata['sha256']:
                raise ValueError('Base model/tokenizer configuration changed')
        self.check_lineage(lineage, app)
        return batch, rows, totals, summary, app

    def check_lineage(self, lineage, app):
        self.require(lineage['run_id'] == self.name and lineage['domain'] == 'Toolathlon'
            and lineage['global_step'] == self.step and lineage['policy_version'] == app['policy_version']
            and lineage['applied_update_uuids'] == app['applied_update_uuids'], 'Native policy ancestry mismatch')
        if self.step == 0:
            self.require(lineage['kind'] == 'initial_pinned_qwen3_8b' and lineage['model_path'] == str(self.model)
                and lineage['model_revision'] == self.config['model_revision'], 'Wrong native genesis')
            inventory = checked(lineage['model_inventory'])
            for name, reference in inventory.items():
                self.require(reference['path'] == str(self.model / name) and file_ref(self.model / name) == reference,
                             'Base model file changed after initial full hash verification')
            if file_ref(self.model / 'deployment_source.json') != lineage['deployment_source']:
                raise ValueError('Base checkpoint source metadata changed')
        else:
            checkpoint = Path(lineage['checkpoint_path'])
            self.require(checkpoint.parent == Path(self.config['storage_root']) / 'checkpoints'
                and checkpoint.name == f'{self.name}--step{self.step}'
                and lineage['status'] == 'published_fresh_native_load_exact', 'Invalid native resume boundary')
            self.require(file_ref(checkpoint / 'complete.json')['sha256'] == lineage['complete_manifest_sha256']
                and file_ref(checkpoint / 'application_state.json')['sha256'] == lineage['application_state_sha256'],
                'Native resume manifests changed')
            parent = read(checkpoint / 'application_state.json')
            self.require(parent['applied_update_uuids'] == app['applied_update_uuids'], 'Lost update UUID prefix')
            previous = State(paths(self.config, self.name, self.step - 1)['work']).get()
            self.require(previous['phase'] == 'INSTALLED', 'Previous weights not confirmed installed')
            installed = checked(previous['receipts']['install'])
            self.require(installed['policy_version'] == app['policy_version']
                and installed['native_lineage'] == self.context['lineage'], 'Installed/native policy mismatch')

    def arguments(self):
        lineage = checked(self.context['lineage'])
        return SimpleNamespace(batch=self.paths['batch'], model=str(self.model),
            run_dir=str(Path(self.paths['work']) / 'actor'), application_state=self.paths['app'],
            checkpoint_root=str(Path(self.config['storage_root']) / 'checkpoints'),
            policy_version=f'V{self.step:03d}', expected_groups=32, engineering=False,
            resume_checkpoint=lineage.get('checkpoint_path') if self.step else None, check_contract=False)

    def effect_gate(self, run_id, action):
        self.require(run_id in (None, self.name), 'Effect run identity changed')
        state = self.state.get()
        self.require(self.stage in State.NEXT and state['phase'] == State.NEXT[self.stage][1],
                     'Worker requires an explicitly issued lifecycle stage')
        self.require(state['context'] == file_ref(self.context_file), 'Worker context differs from launch')
        for reference in self.context['source_files']:
            self.require(file_ref(reference['path']) == reference, 'Runtime changed at effect boundary')
        return {'update_index': self.step, 'phase_timeout_seconds': self.config['training']['phase_timeout_seconds'],
                'prior_boundary': {'source': self.context['lineage']}}

    def actor_admission(self, args):
        if vars(args) != vars(self.arguments()):
            raise ValueError('Actor arguments differ from bound local H20 context')
        return self.effect_gate(self.name, 'actor')

    def device_guard(self):
        ids = self.config['gpu']['physical_ids']
        visible = ','.join(map(str, ids))
        self.require(os.environ.get('CUDA_VISIBLE_DEVICES') == visible, 'Wrong H20 device binding')
        self.require(os.environ.get('WORLD_SIZE') == '4', 'Original actor requires four local ranks')
        result = subprocess.check_output(['nvidia-smi', '-i', visible,
            '--query-gpu=index,uuid,name,memory.total,memory.used', '--format=csv,noheader,nounits'], text=True)
        parsed = []
        for line in result.splitlines():
            index, uuid, name, total, used = [value.strip() for value in line.split(',')]
            self.require(int(index) in ids and 'H20' in name and int(total) >= self.config['gpu']['minimum_memory_mib']
                and int(used) < self.config['gpu']['idle_memory_limit_mib'], 'H20 card unavailable/occupied/too small')
            parsed.append(uuid)
        self.require(len(parsed) == len(set(parsed)) == 4, 'Expected four distinct full H20 GPUs')
        apps = subprocess.check_output(['nvidia-smi', '-i', visible, '--query-compute-apps=gpu_uuid,pid',
                                        '--format=csv,noheader'], text=True)
        self.require(all(line.split(',')[0].strip() not in parsed for line in apps.splitlines() if ',' in line),
                     'Selected H20 has another GPU process; no signals sent')

    def directory(self, path):
        path = Path(path).absolute()
        self.require(path.resolve() == path and path.is_dir() and path.stat().st_uid == os.getuid(),
                     'Missing/linked/foreign local runtime directory')
        self.require(any(path == Path(self.config[k]) or path.is_relative_to(Path(self.config[k]))
            for k in ('model_path', 'storage_root', 'control_root', 'runs_root')), 'Directory outside bound roots')

    def validation_arguments(self):
        self.effect_gate(self.name, 'native_validation')
        self.device_guard()
        return SimpleNamespace(validate_checkpoint=self.paths['partial'], model=str(self.model),
            run_dir=str(Path(self.paths['work']) / 'validation'),
            checkpoint_root=str(Path(self.config['storage_root']) / 'checkpoints'))

    def export_arguments(self):
        self.effect_gate(self.name, 'native_export')
        self.require(os.environ.get('CUDA_VISIBLE_DEVICES') == '', 'FSDP policy export must stay CPU gloo')
        return SimpleNamespace(checkpoint=self.paths['checkpoint'], target=self.paths['export'],
            run_dir=str(Path(self.paths['work']) / 'export'), tokenizer_model=str(self.model),
            checkpoint_root=str(Path(self.config['storage_root']) / 'checkpoints'))

    def build(self):
        if self.modules is not None:
            return self.modules
        sources = self.sources
        metrics = sources.load(SOURCE + 'actor_backend/stage_metrics.py')
        storage = SimpleNamespace(MODEL8=self.model, directory=self.directory,
            current_work=lambda: Path(self.paths['work']), current_collection=lambda: Path(self.paths['work']) / 'collection')
        core = sources.load(SOURCE + 'actor_backend/core.py', dependencies={'data_storage': storage, 'stage_metrics': metrics})
        guard, budget = sources.storage()
        binding = SimpleNamespace(guard_class=lambda: guard.CheckpointGuard, engineering_authority=lambda: None)
        bounded = sources.load(SOURCE + 'actor_backend/bounded_io.py')
        dependencies = {'data_storage': storage, 'core': core, 'stage_metrics': metrics,
                        'storage_binding': binding, 'bounded_io': bounded}
        checkpoint = sources.load(SOURCE + 'actor_backend/checkpoint.py', dependencies=dependencies,
                                  transform=sources.validate_boundary, namespace={'runtime': self})
        dependencies['checkpoint'] = checkpoint
        gate = SimpleNamespace(effect_gate=self.effect_gate, update_uuid=update_uuid)
        namespace = {**vars(core), **vars(metrics), **vars(sources.upstream.actor_math),
            'runtime': self, 'json': json, 'os': os, 'datetime': datetime, 'Path': Path,
            'hashlib': hashlib, 'time': time, 'traceback': traceback, 'idle_guard': self.device_guard}
        transaction = SimpleNamespace(gate=lambda _: self.effect_gate(self.name, 'actor'), checked=checked)
        def raw(path, cap, expected):
            self.require(Path(path).stat().st_size <= cap and file_ref(path)['sha256'] == expected,
                         'Resume source application changed')
            return Path(path).read_bytes()
        resume = sources.load(SOURCE + 'actor_backend/bridge.py', names={'resume_world4'},
            dependencies={'core': core, 'checkpoint': checkpoint}, namespace={**vars(core),
                'json': json, 'hashlib': hashlib, 'datetime': datetime, 'stage': metrics.stage,
                'need': self.require, 'read': read, 'ref': file_ref, 'checked': checked, 'RUN': self.name,
                'I': SimpleNamespace(raw=raw)})
        actor = sources.load(SOURCE + 'actor_backend/actor_update.py', names={'run'},
            dependencies={**dependencies, 'formal_gate': gate, 'transaction_gate': transaction, 'bridge': resume},
            namespace=namespace, transform=sources.actor_boundary)
        dependencies['actor_update'] = SimpleNamespace(idle_guard=self.device_guard)
        # checkpoint.main imports idle_guard only when the validation worker runs.
        checkpoint.__dict__['__builtins__']['__import__'] = self._imports(dependencies)
        export = sources.load(SOURCE + 'actor_backend/policy_export.py', dependencies=dependencies,
            namespace={'runtime': self}, transform=sources.export_boundary)
        self.modules = SimpleNamespace(core=core, checkpoint=checkpoint, actor=actor, export=export,
                                       guard=guard, budget=budget, resume=resume)
        return self.modules

    @staticmethod
    def _imports(dependencies):
        import builtins
        def imported(name, globals=None, locals=None, fromlist=(), level=0):
            if name in dependencies:
                return dependencies[name]
            return builtins.__import__(name, globals, locals, fromlist, level)
        return imported

    def run(self, stage):
        if stage not in ('actor', 'validate', 'export'):
            raise ValueError('Unknown native GPU lifecycle stage')
        self.stage = stage
        self.effect_gate(self.name, stage)
        self.load_contract()
        modules = self.build()
        if stage == 'actor':
            return modules.actor.run(self.arguments())
        if stage == 'validate':
            return modules.checkpoint.main()
        return modules.export.main()
