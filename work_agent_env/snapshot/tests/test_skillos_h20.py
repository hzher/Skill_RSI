"""Simple CPU/static checks for real H20 GPU code. Never starts model/GPU work."""
import ast
import builtins
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from integrations.skill_generator.skillos.structured import AfterThinking
from training.skillos_h20_config import ROOT, load_config, commands, offline_plan
from training.skillos_h20_runtime import Runtime
from training.skillos_h20_sources import Sources, VENDOR, SOURCE
from training.skillos_h20_state import State, atomic, file_ref, update_uuid


def config(directory=None):
    value = load_config(ROOT / 'configs/skillos_h20.json')
    if directory is not None:
        for name in ('model_path', 'storage_root', 'control_root', 'runs_root', 'services_root'):
            value[name] = str(Path(directory) / name)
    return value


def runtime(config):
    value = Runtime.__new__(Runtime)
    value.config, value.name, value.step = config, 'cpu_fixture', 0
    value.model = Path(config['model_path'])
    from training.skillos_h20_config import paths
    value.paths, value.sources, value.modules = paths(config, value.name, 0), Sources(config), None
    return value


class H20Tests(unittest.TestCase):
    def test_compile_has_no_gpu_import_subprocess_or_hardware_probe(self):
        from scripts.train_skillos_h20 import compile_deployment
        original_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.split('.')[0] in ('torch', 'vllm', 'transformers'):
                raise AssertionError('Offline check imported model/GPU package: ' + name)
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guarded), patch('subprocess.Popen', side_effect=AssertionError), \
                patch('subprocess.run', side_effect=AssertionError), patch('subprocess.check_output', side_effect=AssertionError):
            result = compile_deployment(config(), 'h20_cpu_fixture')
        self.assertEqual(result['gpu_jobs_started'], 0)
        self.assertFalse(result['gpu_probed'])
        command = result['service_commands']['curator']
        self.assertIn('skillos-curator-h20_cpu_fixture-V000', command)
        self.assertIn('training.skillos_h20_worker_model.H20PolicyFingerprintWorker', command)

    def test_gpu_commands_bind_four_h20_ranks_and_export_stays_cpu(self):
        value = config()
        value['gpu']['physical_ids'] = [8, 9, 10, 11]
        result = commands(value, 'gpu_future', 1)
        for stage in ('actor', 'validate'):
            self.assertEqual(result[stage]['environment']['CUDA_VISIBLE_DEVICES'], '8,9,10,11')
            self.assertIn('--nproc-per-node=4', result[stage]['argv'])
            self.assertIn('H20', result[stage]['device'])
        self.assertEqual(result['export']['environment']['CUDA_VISIBLE_DEVICES'], '')
        self.assertIn('gloo', result['export']['device'])
        self.assertEqual(offline_plan(value, 'gpu_future')['actual_parameter_updates'], 0)

    def test_no_single_gpu_or_changed_objective_fallback(self):
        original = json.loads((ROOT / 'configs/skillos_h20.json').read_text())
        for mutate in (lambda x: x['gpu'].update(physical_ids=[4]),
                       lambda x: x['training'].update(normalize_std=True),
                       lambda x: x['training'].update(groups_per_update=1)):
            with tempfile.TemporaryDirectory() as temporary:
                value = deepcopy(original)
                mutate(value)
                path = Path(temporary) / 'bad.json'
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_actor_optimizer_body_is_original_after_deployment_preamble(self):
        tree = ast.parse((VENDOR / SOURCE / 'actor_backend/actor_update.py').read_text())
        original = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        index = next(i for i, n in enumerate(original.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'rank')
        expected = ast.dump(ast.Module(body=original.body[index:], type_ignores=[]), include_attributes=False)
        adapted, changes = Sources.actor_boundary(deepcopy(tree))
        function = next(n for n in adapted.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        index = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'rank')
        self.assertEqual(ast.dump(ast.Module(body=function.body[index:], type_ignores=[]), include_attributes=False), expected)
        self.assertEqual(len(changes), 1)

    def test_native_update_uuid_is_exactly_the_upstream_definition(self):
        expected = hashlib.sha256(json.dumps({'run_id': 'fixture', 'update_index': 1, 'batch_sha256': 'a'*64},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(update_uuid('fixture', 1, 'a'*64), expected)

    def test_unknown_optimizer_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic(root / 'context.json', {'test_fixture': True}, new=True)
            state = State(root)
            state.create(file_ref(root / 'context.json'))
            state.begin('actor', command={'test_fixture': True})
            state.unknown('actor', 'fixture interruption after dispatch')
            with self.assertRaisesRegex(ValueError, 'Cannot replay'):
                state.begin('actor', command={})
            self.assertEqual(state.get()['phase'], 'ACTOR_UNKNOWN')

    def test_full_install_barrier_and_context_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic(root / 'context.json', {'test_fixture': True}, new=True)
            atomic(root / 'receipt.json', {'test_fixture': True}, new=True)
            state = State(root)
            state.create(file_ref(root / 'context.json'))
            with self.assertRaises(ValueError):
                state.begin('install', command={})
            for stage in ('actor', 'validate', 'export', 'install'):
                state.begin(stage, command={'test_fixture': True})
                state.finish(stage, file_ref(root / 'receipt.json'))
            self.assertEqual(state.get()['phase'], 'INSTALLED')
            atomic(root / 'context.json', {'test_fixture': True, 'changed': True})
            with self.assertRaisesRegex(ValueError, 'changed'):
                state.get()

    def test_original_checkpoint_schema_requires_all_four_optimizer_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules = runtime(config(root)).build()
            directory = root / 'fixture-checkpoint'
            directory.mkdir()
            names = [f'{kind}_world_size_4_rank_{rank}.pt' for kind in ('model', 'optim', 'extra_state') for rank in range(4)]
            names += ['application_state.json', 'application_input.json', 'batch_contract.json', 'warmup.json',
                      'fsdp_config.json', 'training_contract.json']
            names += [f'rank{rank}-{kind}.json' for rank in range(4) for kind in ('boundary', 'writer')]
            files = {}
            for name in names:
                body = b'CPU schema fixture, not a real tensor shard\n'
                (directory / name).write_bytes(body)
                files[name] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
            atomic(directory / 'save_manifest.json', {'world_size': 4, 'files': files}, new=True)
            modules.checkpoint.verify_saved(directory)
            (directory / 'optim_world_size_4_rank_3.pt').unlink()
            with self.assertRaises(AssertionError):
                modules.checkpoint.verify_saved(directory)

    def test_h20_guard_only_runs_with_mocked_hardware_and_rejects_busy_cards(self):
        value = runtime(config())
        rows = '\n'.join(f'{i}, GPU-fixture-{i}, NVIDIA H20, 97871, 0' for i in (4, 5, 6, 7))
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '4,5,6,7', 'WORLD_SIZE': '4'}):
            with patch('subprocess.check_output', side_effect=[rows, '']) as probe:
                value.device_guard()
                self.assertEqual(probe.call_count, 2)
            with patch('subprocess.check_output', return_value=rows.replace('97871, 0', '97871, 4096')):
                with self.assertRaisesRegex(ValueError, 'unavailable/occupied'):
                    value.device_guard()

    def test_content_schema_does_not_constrain_thinking_tokens(self):
        calls = []
        wrapped = AfterThinking(lambda ids, scores: calls.append(list(ids[0])) or 'masked', prompt_length=2, closing_ids=[7, 8])
        self.assertEqual(wrapped([[10, 11, 1, 2, 7]], 'raw'), 'raw')
        self.assertEqual(calls, [])
        self.assertEqual(wrapped([[10, 11, 1, 2, 7, 8]], 'raw'), 'masked')
        self.assertEqual(wrapped([[10, 11, 1, 2, 7, 8, 9]], 'raw'), 'masked')
        self.assertEqual(len(calls), 2)

    def test_unowned_service_pid_is_never_signalled(self):
        from training.skillos_h20_services import Services
        with tempfile.TemporaryDirectory() as temporary:
            services = Services(config(temporary), 'fixture', project_root=ROOT)
            services.root.mkdir(parents=True)
            atomic(services.receipt, {'services': {'curator': {'pid': 123, 'starttime': 'old'}}}, new=True)
            with patch('training.skillos_h20_services.process_identity', return_value={
                    'pid': 123, 'starttime': 'new', 'uid': os.getuid(), 'state': 'S'}), \
                    patch('os.killpg', side_effect=AssertionError('Must not signal')):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    services.stop()

    def test_non_execute_cli_never_constructs_controller(self):
        from scripts.train_skillos_h20 import main
        with patch('training.skillos_h20.Controller', side_effect=AssertionError('No execution')):
            from contextlib import redirect_stdout
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--run-id', 'future_gpu', '--action', 'update']), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'planned_not_executed')
            self.assertEqual(value['commands']['actor']['world_size'], 4)

    def test_review_proposal_does_not_admit_seed_or_invent_family(self):
        from scripts.prepare_skillos_groups import proposal
        from tests.test_skillos_upstream import tasks
        from training.skillos_h20_data import check_groups
        task_set = tasks()
        groups = [{'group_id': 'fixture' + str(i), 'ordered_task_ids': [t['uid'] for t in task_set['tasks']],
                   'rollout_seeds': list(range(8))} for i in range(32)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic(root / 'tasks.json', task_set, new=True)
            atomic(root / 'groups.json', groups, new=True)
            value = proposal(config(), task_set_file=root / 'tasks.json', groups_file=root / 'groups.json', run_id='review_fixture')
            self.assertFalse(value['training_admitted'])
            self.assertFalse(value['reviewed_registry_written'])
            self.assertIsNone(next(iter(value['candidate_admission']['provenance'].values()))['task_family'])
            with self.assertRaisesRegex(ValueError, 'reviewed SkillOS group admission'):
                check_groups(value['candidate_admission'], {}, config())

    def test_content_endpoint_must_confirm_requested_schema_enforcement(self):
        import asyncio
        import httpx
        from integrations.skill_generator.skillos.bridge import RoleClient, Journal
        from integrations.skill_generator.skillos.upstream import Upstream
        from tests.test_skillos_upstream import Tokenizer
        async def scenario(root):
            upstream = Upstream()
            journal = Journal(root / 'run', {'fixture': True}, upstream.io)
            role = {'served_model': 'content', 'policy_version': 'frozen', 'context_limit': 32768,
                    'chat_template_kwargs': {'enable_thinking': True}, 'base_url': 'http://fixture/v1',
                    'backend': 'transformers'}
            def response(request):
                self.assertIn('response_format', json.loads(request.content))
                return httpx.Response(200, json={'schema_enforced': False})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                transport = RoleClient(name='contentjudge', role=role, tokenizer=Tokenizer(),
                    journal=journal, upstream=upstream, http_client=client, semaphore=asyncio.Semaphore(1))
                with self.assertRaisesRegex(ValueError, 'actually enforce'):
                    await transport.query_exact({'role': 'contentjudge', 'policy_version': 'frozen'},
                        [{'role': 'user', 'content': 'fixture'}], 42, 4096, schema={'type': 'object'})
        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(scenario(Path(temporary)))

    def test_retention_refuses_an_open_checkpoint_reader(self):
        from training.skillos_h20 import Controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'model-shard.fixture'
            path.write_bytes(b'CPU fixture')
            with path.open('rb'):
                with self.assertRaisesRegex(ValueError, 'active reader'):
                    Controller._assert_no_readers([root])


if __name__ == '__main__':
    unittest.main()
