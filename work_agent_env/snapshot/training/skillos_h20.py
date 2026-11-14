"""Deploy/collect/train/install controller for the actual four-H20 SkillOS path.

All effects are explicit function calls; importing and planning are CPU-only.
Original source204 performs FSDP optimization, native save/load and HF export.
"""
import asyncio
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import subprocess

from integrations.benchmark_tasks import digest, validate_task_set
from training.skillos_h20_config import commands, paths, run_id as validate_run_id
from training.skillos_h20_data import check_groups, prepare
from training.skillos_h20_runtime import Runtime
from training.skillos_h20_services import Services
from training.skillos_h20_sources import Sources
from training.skillos_h20_state import atomic, checked, file_ref, read, State


class Controller:
    def __init__(self, config, name, *, project_root):
        validate_run_id(name)
        self.config, self.name, self.project = config, name, Path(project_root)
        self.root = Path(config['runs_root']) / name
        self.services = Services(config, name, project_root=self.project)

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # Serialize all runs sharing this deployment's H20 device pool. Other
        # workloads are detected by the idle check, never stopped or adopted.
        global_lock = Path(self.config['control_root']) / '.controller.lock'
        global_lock.parent.mkdir(parents=True, exist_ok=True)
        with global_lock.open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def hf_reader(self):
        return Sources(self.config).load('runtime/weight-sync-manager-v3/cpu_fingerprint.py', extra=True).hf_fingerprint

    def genesis(self):
        """Actual independent CPU tensor inventory; never a fabricated worker proof."""
        target = self.root / 'genesis'
        if target.exists():
            raise ValueError('Genesis already exists; do not recreate/reset an existing native run')
        model = Path(self.config['model_path'])
        deployment = read(model / 'deployment_source.json')
        if deployment['revision'] != self.config['model_revision'] or deployment.get('weights_lfs_verified') is not True:
            raise ValueError('Complete pinned base weights are required')
        inventory = {}
        for name, info in deployment['files'].items():
            reference = file_ref(model / name)
            if reference['sha256'] != info['sha256'] or (model / name).stat().st_size != info['bytes']:
                raise ValueError('Base model differs from pinned LFS/file inventory')
            inventory[name] = reference
        tensors = self.hf_reader()(model)
        if len(tensors) != 399:
            raise ValueError('Unexpected pinned Qwen3-8B tensor inventory')
        target.mkdir(parents=True, exist_ok=False)
        atomic(target / 'model-inventory.json', inventory, new=True)
        atomic(target / 'hf-reference.json', tensors, new=True)
        lineage = {'kind': 'initial_pinned_qwen3_8b', 'run_id': self.name, 'domain': 'Toolathlon',
            'training_seed': 42, 'global_step': 0, 'update_index': 0, 'policy_version': 'V000',
            'model_path': str(model), 'model_revision': self.config['model_revision'],
            'model_inventory': file_ref(target / 'model-inventory.json'),
            'deployment_source': file_ref(model / 'deployment_source.json'),
            'hf_reference': file_ref(target / 'hf-reference.json'), 'applied_update_uuids': []}
        atomic(target / 'lineage.json', lineage, new=True)
        return target / 'lineage.json'

    def current(self):
        path = self.root / 'installed_policy.json'
        if not path.is_file():
            raise ValueError('No installed policy. Bootstrap this run with actual role fingerprints first')
        value = read(path)
        checked(value['native_lineage'])
        checked(value['role_bindings'])
        return value

    async def fixed_executor(self, task_set):
        from integrations.executors import ExecutorOptions
        from integrations.orchestration.executor import DownstreamExecutor
        from scripts.run_skill_pipeline import load_credentials
        executor = read(self.config['executor_config'])
        load_credentials(self.project / executor['executor_credentials_file'])
        return await DownstreamExecutor.create(project_root=self.project,
            options=ExecutorOptions(**executor['executor']), tasks=task_set['tasks'])

    def bootstrap(self, task_set):
        with self.lock():
            if (self.root / 'installed_policy.json').exists():
                raise ValueError('Run already bootstrapped')
            lineage_path = self.genesis()
            fixed = asyncio.run(self.fixed_executor(task_set))
            self.services.start('V000', self.config['model_path'])
            bindings = self.services.bind(lineage=read(lineage_path), fixed_executor_id=fixed.executor_id)
            result = {'policy_version': 'V000', 'global_step': 0, 'native_lineage': file_ref(lineage_path),
                      'role_bindings': file_ref(bindings)}
            atomic(self.root / 'installed_policy.json', result, new=True)
            return result

    def collect(self, admission_file):
        from integrations.skill_generator.roles import SkillOSRoles
        from integrations.skill_generator.skillos.bridge import collect
        from integrations.skill_generator.skillos.bridge import make_plan
        with self.lock():
            current = self.current()
            lineage = checked(current['native_lineage'])
            bindings = checked(current['role_bindings'])
            admission = read(admission_file)
            task_set = validate_task_set(checked(admission['task_set']))
            # Preserve the reviewed source record/seed ordering exactly.
            groups = [{'group_id': group['group_id'],
                       'ordered_task_ids': group['source_record']['ordered_task_ids'],
                       'rollout_seeds': group['rollout_seeds']} for group in admission['groups']]
            plan = make_plan(task_set, groups=groups, run_id=self.name, engineering=False,
                policy_version=lineage['policy_version'], update_index=lineage['global_step'])
            check_groups(admission, plan, self.config)
            Services.assert_bindings(current['role_bindings'])
            output = self.root / ('collection-%04d' % lineage['global_step'])
            async def run():
                fixed = await self.fixed_executor(task_set)
                roles = SkillOSRoles(checked(bindings['roles_config']))
                try:
                    Runtime.check_role_bindings(bindings, self.config, lineage, {
                        'fixed_executor_id': fixed.executor_id, 'roles_config_sha256': digest(roles.config)})
                    return await collect(upstream=Sources(self.config).upstream, plan=plan, task_set=task_set,
                        directory=output, roles=roles, fixed_executor=fixed,
                        role_bindings=current['role_bindings'], role_guard=lambda: Services.assert_bindings(current['role_bindings']))
                finally:
                    await roles.close()
            batch, summary = asyncio.run(run())
            return {'collection': str(output), 'summary': summary, 'actual_parameter_updates': 0}

    def prepare(self, collection, admission_file):
        with self.lock():
            current = self.current()
            return prepare(self.config, run_id=self.name, collection=collection, admission_file=admission_file,
                lineage_file=current['native_lineage']['path'], role_binding_file=current['role_bindings']['path'],
                project_root=self.project)

    def run_stage(self, context_file, stage):
        with self.lock():
            runtime = Runtime(context_file)
            if runtime.config != self.config or runtime.name != self.name:
                raise ValueError('Controller/worker deployment differs')
            runtime.load_contract()
            if stage == 'install':
                return self.install(runtime)
            if stage not in ('actor', 'validate', 'export'):
                raise ValueError('Unknown training lifecycle stage')
            spec = commands(self.config, self.name, runtime.step, project_root=self.project)[stage]
            if stage == 'actor':
                # Storage admission is CPU-only and precedes dispatch. A known
                # capacity failure must not create an uncertain optimizer event.
                runtime.build().checkpoint.admission(runtime.arguments().checkpoint_root)
                # Collection returned/drained before preparation. Only these
                # project's registered service process groups are stopped.
                self.services.stop()
            state = runtime.state
            state.begin(stage, command=spec)
            log_path = Path(runtime.paths['work']) / (stage + '-launcher.log')
            try:
                with log_path.open('xb') as log:
                    result = subprocess.run(spec['argv'], cwd=self.project,
                        env={**os.environ, **spec['environment']}, stdout=log, stderr=subprocess.STDOUT)
                if result.returncode:
                    raise RuntimeError('Native ' + stage + ' exited ' + str(result.returncode) + '; inspect the durable log')
                receipt_path = self.stage_receipt(runtime, stage)
                state.finish(stage, file_ref(receipt_path))
                return read(receipt_path)
            except BaseException as error:
                state.unknown(stage, type(error).__name__)
                raise

    def retire(self):
        """Explicit working-state retirement using the original retention plan.

        Never touches task evidence, the newest installed checkpoint, another
        run's state, a partial checkpoint, or final archives.
        """
        with self.lock():
            current = self.current()
            lineage = checked(current['native_lineage'])
            step = lineage['global_step']
            if step < 1:
                raise ValueError('No published native checkpoint to preserve')
            context = paths(self.config, self.name, step - 1)['context']
            runtime = Runtime(context)
            modules = runtime.build()
            latest = Path(lineage['checkpoint_path'])
            checkpoint_root = Path(self.config['storage_root']) / 'checkpoints'
            exports = checkpoint_root.parent / 'serving-policies'
            with modules.checkpoint.guard(checkpoint_root) as guard:
                rows = guard.scan()
                for path, owner, _ in rows:
                    if owner['run'] != self.name or path.name.endswith('.partial'):
                        raise ValueError('Cannot retire another run or an incomplete checkpoint')
                    guard.verify(path)
                    modules.checkpoint.loadable_receipt(path)
                policy = runtime.sources.load('snapshots/2026-10-29/source204/mainline_contract.py',
                    names={'retention_plan'})
                old = sorted(rows, key=lambda row: row[1]['sequence'])[:-1] if len(rows) == 2 else []
                reclaim_exports = 0
                targets = []
                for path, owner, _ in old:
                    targets.append(path)
                    paired = exports / path.name
                    if paired.exists():
                        manifest = read(paired / 'export-complete.json')
                        if (manifest['source']['source_checkpoint'] != str(path)
                                or manifest['source']['source_complete_sha256'] != file_ref(path / 'complete.json')['sha256']):
                            raise ValueError('Working export/native lineage mismatch')
                        reclaim_exports += sum(p.stat().st_size for p in paired.rglob('*') if p.is_file())
                        targets.append(paired)
                budget = guard.budget.snapshot()
                facts = [{'path': str(path), 'sequence': owner['sequence'], 'kind': owner['kind'], 'bytes': size,
                          'partial': False, 'load_validated': True} for path, owner, size in rows]
                plan = policy.retention_plan(facts, str(latest), 110000000000,
                    shutil.disk_usage(checkpoint_root).free - 40000000000,
                    export_bytes=budget['nonnative_bytes'] - reclaim_exports + 40000000000)
                transaction = self.root / ('retention-before-%04d' % step)
                transaction.mkdir(exist_ok=False)
                atomic(transaction / 'intent.json', {'installed': current, 'plan': plan,
                    'targets': [str(path) for path in targets]}, new=True)
                frozen_inventory = guard.budget._tree()
                self._assert_no_readers(targets)
                for path in targets:
                    if path == latest or path.parent not in (checkpoint_root, exports) or path.is_symlink():
                        raise ValueError('Unowned retention target')
                    for file in path.rglob('*'):
                        if file.is_symlink() or ('final' in file.name.lower()) or ('archive' in file.name.lower()):
                            raise ValueError('Linked/final/archive retention target')
                        if file.is_file() and file.suffix in ('.json', '.log'):
                            destination = transaction / 'metadata' / path.name / file.relative_to(path)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(file, destination)
                            if file_ref(file)['sha256'] != file_ref(destination)['sha256']:
                                raise ValueError('Retirement metadata copy changed')
                    atomic(transaction / (path.name + '-hashes.json'), {
                        str(file.relative_to(path)): file_ref(file)['sha256'] for file in path.rglob('*') if file.is_file()}, new=True)
                self._fsync_tree(transaction)
                self._assert_no_readers(targets)
                if guard.budget._tree() != frozen_inventory:
                    raise ValueError('Checkpoint/export inventory changed during retirement')
                for path in targets:
                    shutil.rmtree(path)
                atomic(transaction / 'complete.json', {'plan': plan, 'latest_preserved': str(latest)}, new=True)
                return read(transaction / 'complete.json')

    @staticmethod
    def _fsync_tree(directory):
        for path in directory.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        folders = sorted((p for p in directory.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
        for folder in folders + [directory]:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _assert_no_readers(targets):
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                for descriptor in (process / 'fd').iterdir():
                    try:
                        path = Path(os.readlink(descriptor))
                        if path.is_absolute() and any(path == target or path.is_relative_to(target) for target in targets):
                            raise ValueError('Working checkpoint/export still has an active reader')
                    except FileNotFoundError:
                        pass
            except (FileNotFoundError, PermissionError):
                continue

    def stage_receipt(self, runtime, stage):
        work = Path(runtime.paths['work'])
        if stage == 'actor':
            path = work / 'actor/actor_result.json'
            result = read(path)
            if (result['optimizer_steps_this_invocation'] != 1 or result.get('gpu_update_verified') is not True
                    or result['global_step_after'] != runtime.step + 1
                    or result['policy_version_before'] != f'V{runtime.step:03d}'
                    or result['policy_version_after'] != f'V{runtime.step+1:03d}'
                    or result['batch_sha256'] != runtime.context['batch']['sha256']):
                raise ValueError('Native actor did not prove exactly the bound update')
            for rank in range(4):
                local = read(work / 'actor' / f'rank{rank}-actor.json')
                if local['optimizer_steps_this_invocation'] != 1 or local['status'] != result['status']:
                    raise ValueError('Incomplete native rank completion')
            runtime.build().checkpoint.verify_saved(runtime.paths['partial'])
            history = runtime.sources.load('snapshots/2026-10-29/source204/mainline_contract.py',
                names={'canonical', 'update_uuid', 'assert_history'}, namespace={
                    'json': __import__('json'), 'hashlib': __import__('hashlib')})
            history.assert_history(checked(runtime.context['app']),
                                   read(Path(runtime.paths['partial']) / 'application_state.json'))
            return path
        if stage == 'validate':
            path = work / 'validation/checkpoint_receipt.json'
            result = read(path)
            if (result['status'] != 'published_fresh_native_load_exact'
                    or result['checkpoint_path'] != runtime.paths['checkpoint']
                    or result['global_step'] != runtime.step + 1):
                raise ValueError('Invalid fresh native publication receipt')
            runtime.build().checkpoint.loadable_receipt(runtime.paths['checkpoint'])
            with runtime.build().checkpoint.guard(runtime.arguments().checkpoint_root) as guard:
                guard.verify(runtime.paths['checkpoint'])
            return path
        path = work / 'export/serving_export_receipt.json'
        export = read(path)
        if (export['status'] != 'bf16_hf_exported_pending_worker_install'
                or export['weights_path'] != runtime.paths['export'] or export['checkpoint_path'] != runtime.paths['checkpoint']
                or export['policy_version'] != f'V{runtime.step+1:03d}'):
            raise ValueError('Export/accepted native checkpoint identity differs')
        verify_export(export)
        return path

    def install(self, runtime):
        state = runtime.state
        if state.get()['phase'] != 'EXPORTED':
            raise ValueError('Installation requires a completed verified export')
        export = checked(state.get()['receipts']['export'])
        verify_export(export)
        state.begin('install', command={'kind': 'start pinned Curator export and frozen judges; compare actual worker tensors'})
        try:
            weights = Path(export['weights_path'])
            reference = self.hf_reader()(weights)
            if len(reference) != 399:
                raise ValueError('Unexpected exported Curator tensor inventory')
            work = Path(runtime.paths['work'])
            reference_path = work / 'export/hf-reference.json'
            atomic(reference_path, reference, new=True)
            native = checked(state.get()['receipts']['validate'])
            app = read(Path(native['checkpoint_path']) / 'application_state.json')
            lineage = {**native, 'run_id': self.name, 'domain': 'Toolathlon', 'update_index': runtime.step + 1,
                'training_seed': 42, 'hf_reference': file_ref(reference_path),
                'applied_update_uuids': app['applied_update_uuids']}
            lineage_path = work / 'lineage-after.json'
            atomic(lineage_path, lineage, new=True)
            self.services.start(lineage['policy_version'], weights)
            fixed = checked(runtime.context['role_bindings'])['fixed_executor_id']
            bindings = self.services.bind(lineage=lineage, fixed_executor_id=fixed)
            new_binding = read(bindings)
            previous = checked(runtime.context['role_bindings'])
            for name in ('selfjudge', 'contentjudge'):
                if checked(new_binding['proofs'][name])['tensors'] != checked(previous['proofs'][name])['tensors']:
                    raise ValueError('Frozen judge changed during installation')
            result = {'schema_version': 'workagent-skillos-native-install-v1',
                'policy_version': lineage['policy_version'], 'global_step': lineage['global_step'],
                'native_lineage': file_ref(lineage_path), 'export': state.get()['receipts']['export'],
                'role_bindings': file_ref(bindings), 'actual_worker_parameters_verified': True}
            install_path = work / 'installed.json'
            atomic(install_path, result, new=True)
            state.finish('install', file_ref(install_path))
            atomic(self.root / 'installed_policy.json', result)
            return result
        except BaseException as error:
            state.unknown('install', type(error).__name__)
            raise


def verify_export(export):
    root = Path(export['weights_path'])
    if file_ref(root / 'export-complete.json')['sha256'] != export['export_manifest_sha256']:
        raise ValueError('Export manifest changed')
    manifest = read(root / 'export-complete.json')
    if (manifest['source']['source_checkpoint'] != export['checkpoint_path']
            or file_ref(Path(export['checkpoint_path']) / 'complete.json')['sha256'] != export['source_complete_sha256']):
        raise ValueError('Export/native parent changed')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()} - {'export-complete.json'}
    if actual != set(manifest['files']):
        raise ValueError('Export file set differs')
    for name, info in manifest['files'].items():
        if (file_ref(root / name)['sha256'] != info['sha256'] or (root / name).stat().st_size != info['bytes']):
            raise ValueError('Export payload changed')
