"""Independent testing rounds use explicitly scripted models over actual local HTTP."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest
from support import Provider, bundle, pipeline, proposal, seed, skill
from skillrl.agents import native_exposures
from skillrl.benchmarks import WorkBuddyBenchmark, CommandEvaluator
from skillrl.native import MessagesBridge
from skillrl.storage import load, writer, PendingStage, contained, begin, seal

class FrameworkRounds(unittest.TestCase):
    def test_round_01_add_then_future_consumer(self):
        with TemporaryDirectory() as tmp, Provider([proposal(), proposal('noop')]) as server:
            run = pipeline(tmp, bundle(tmp, count=2), server)
            report = run.run()
            self.assertTrue(report['complete'])
            self.assertEqual(report['active_skills'], 1)
            self.assertEqual(report['reviews'][0]['skills'], [])
            row = report['reviews'][1]['skills'][0]
            self.assertTrue(row['body_proven'])
            self.assertEqual(row['use_state'], 'used')
            self.assertEqual(row['overall_score'], 1)
            self.assertIsNone(row['causal_reward'])

    def test_round_02_autonomous_skip_and_catalog(self):
        with TemporaryDirectory() as tmp, Provider(load_skill=False) as server:
            run = pipeline(tmp, bundle(tmp), server)
            seed(run.root, ['sum-check', 'unrelated-check'])
            report = run.run()
            self.assertTrue(report['complete'])
            request = next(r for r in server.requests if r['model'] == 'executor')
            self.assertEqual(request['tool_choice'], 'auto')
            self.assertEqual(len(json.loads(request['messages'][1]['content'])['catalog']), 2)
            self.assertTrue(all(r['use_state'] is None for r in report['reviews'][0]['skills']))
            self.assertFalse(any(r['model'] == 'judge' for r in server.requests))

    def test_round_03_update_and_full_parent_read(self):
        updated = skill()
        updated['validation'].append('Recompute the aggregate from original values before delivery.')
        with TemporaryDirectory() as tmp, Provider([proposal('update', ['sum-check'], [updated])]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            lib = seed(run.root, ['sum-check'])
            old = lib.state()['active']['sum-check']
            self.assertTrue(run.run()['complete'])
            new = lib.state()['active']['sum-check']
            self.assertEqual(old['skill_id'], new['skill_id'])
            self.assertNotEqual(old['raw_sha256'], new['raw_sha256'])
            self.assertIn('sum-check', load(run.root / 'episodes/task-1/curator/result.json')['read_parents'])

    def test_round_04_archive_preserves_version(self):
        with TemporaryDirectory() as tmp, Provider([proposal('archive', ['sum-check'], [])]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            lib = seed(run.root, ['sum-check'])
            old = lib.state()['active']['sum-check']['raw_sha256']
            self.assertTrue(run.run()['complete'])
            self.assertEqual(lib.state()['active'], {})
            self.assertTrue((lib.root / 'raw_versions' / (old + '.md')).exists())

    def test_round_05_merge_all_parent_reads(self):
        with TemporaryDirectory() as tmp, Provider([proposal('merge', ['sum-check', 'second-check'], [skill('combined-check')])]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            lib = seed(run.root, ['sum-check', 'second-check'])
            self.assertTrue(run.run()['complete'])
            self.assertEqual(set(lib.state()['active']), {'combined-check'})
            self.assertEqual(load(run.root / 'episodes/task-1/curator/result.json')['read_parents'], ['second-check', 'sum-check'])

    def test_round_06_split_two_stages(self):
        with TemporaryDirectory() as tmp, Provider([proposal('split', ['sum-check'], [skill('input-check'), skill('output-check')])]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            lib = seed(run.root, ['sum-check'])
            self.assertTrue(run.run()['complete'])
            self.assertEqual(set(lib.state()['active']), {'input-check', 'output-check'})

    def test_round_07_atomic_cycle_rejection(self):
        with TemporaryDirectory() as tmp, Provider([proposal(children=[skill('loop-check', ['loop-check'])])]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            report = run.run()
            self.assertFalse(report['complete'])
            self.assertEqual(report['active_skills'], 0)
            self.assertEqual(report['episodes'][0]['curation'], 'no_submission')
            self.assertIn('cycle', str(load(run.root / 'episodes/task-1/curator/messages.json')))
            self.assertTrue(list((run.root / 'episodes/task-1/curator').glob('candidate-*.json')))

    def test_round_08_objective_changes_leave_curation_identical(self):
        observations, inputs = [], []
        for expected in (6, 999):
            with TemporaryDirectory() as tmp, Provider() as server:
                run = pipeline(tmp, bundle(tmp, expected=expected), server)
                seed(run.root, ['sum-check'])
                report = run.run()
                observations.append(load(run.root / 'episodes/task-1/curation-observation.json'))
                inputs.append([r['messages'] for r in server.requests if r['model'] == 'curator'])
                self.assertEqual(report['reviews'][0]['task_reward'], int(expected == 6))
                if expected != 6:
                    self.assertEqual(report['reviews'][0]['skills'][0]['training_credit'], 0)
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(inputs[0], inputs[1])

    def test_round_09_429_not_replayed(self):
        with TemporaryDirectory() as tmp, Provider(reject_role='executor') as server:
            run = pipeline(tmp, bundle(tmp), server)
            self.assertFalse(run.run()['complete'])
            self.assertEqual(len(server.requests), 1)
            run.run()
            self.assertEqual(len(server.requests), 1)
            failure = load(next((run.root / 'episodes/task-1/actor/requests').glob('*/failure.json')))
            self.assertEqual(failure['status'], 429)
            self.assertEqual(failure['retry_after'], '120')
            self.assertFalse((run.root / 'episodes/task-1/workspace/output/result.json').exists())

    def test_round_10_resume_and_single_writer(self):
        with TemporaryDirectory() as tmp, Provider([proposal()]) as server:
            run = pipeline(tmp, bundle(tmp), server)
            self.assertTrue(run.run()['complete'])
            count = len(server.requests)
            self.assertTrue(run.run()['complete'])
            self.assertEqual(len(server.requests), count)
            with writer(run.root):
                with self.assertRaises(PendingStage):
                    run.run()
            begin(Path(tmp) / 'pending', {'x': 1})
            with self.assertRaises(PendingStage):
                begin(Path(tmp) / 'pending', {'x': 1})

    def test_round_11_workbuddy_native_verifier_contract(self):
        with TemporaryDirectory() as tmp:
            bench = bundle(tmp)
            hook = Path(tmp) / 'verifier.py'
            hook.write_text('import sys,json,pathlib\nr=json.load(sys.stdin)\nassert r["task_id"]=="task-1"\nassert pathlib.Path(r["workspace"]).is_dir()\nprint(json.dumps({"score":1,"details":{"native_hook":True}}))\n')
            evaluator = CommandEvaluator([sys.executable, str(hook)])
            adapter = WorkBuddyBenchmark(bench.manifest, image='original-image:pinned', evaluator=evaluator)
            workspace = Path(tmp) / 'workspace'
            environment = adapter.prepare(adapter.tasks()[0], workspace)
            self.assertEqual(environment.image, 'original-image:pinned')
            self.assertEqual(adapter.evaluate(adapter.tasks()[0], workspace, {'environment': {}})['score'], 1)
            with self.assertRaises(ValueError):
                WorkBuddyBenchmark(bench.manifest, image=None, evaluator=evaluator)
            with self.assertRaises(ValueError):
                contained(workspace, '../private-answers/task-1.json')

    def test_round_12_invalid_judge_null_and_no_resampling(self):
        with TemporaryDirectory() as tmp, Provider(invalid_effect=True) as server:
            run = pipeline(tmp, bundle(tmp), server)
            seed(run.root, ['sum-check'])
            report = run.run()
            row = report['reviews'][0]['skills'][0]
            self.assertEqual(row['status'], 'invalid_or_unresolved_review')
            self.assertIsNone(row['overall_score'])
            self.assertFalse(report['complete'])
            self.assertTrue(list((run.root / 'evaluation').rglob('effect-candidate.json')))
            self.assertEqual(len([r for r in server.requests if r['model'] == 'judge']), 2)

    def test_round_13_native_bridge_real_http(self):
        import urllib.request
        with TemporaryDirectory() as tmp, Provider() as server:
            bridge = MessagesBridge(server.model('executor'), Path(tmp) / 'native')
            port = bridge.start()
            try:
                body = {'model': 'executor', 'max_tokens': 1024, 'system': 'Public task.',
                    'messages': [{'role': 'user', 'content': json.dumps({'task': {'id': 'task-1'}, 'catalog': []})}],
                    'tools': [{'name': 'Read', 'description': 'Read a file', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}], 'stream': True}
                request = urllib.request.Request('http://127.0.0.1:' + str(port) + '/' + bridge.token + '/v1/messages?beta=true',
                    json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    stream = response.read().decode()
                self.assertIn('message_stop', stream)
                self.assertIn('Read', stream)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(server.requests[0]['tool_choice'], 'auto')
            finally:
                bridge.close()

    def test_round_14_exact_native_Read_body_proof(self):
        with TemporaryDirectory() as tmp:
            lib = seed(tmp, ['sum-check'])
            entry = lib.state()['active']['sum-check']
            body = (lib.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
            entry['content'] = body
            rendered = '\n'.join(str(i + 1) + '\t' + line for i, line in enumerate(body.splitlines()))
            execution = {'events': [{'type': 'tool', 'name': 'Read', 'arguments': {'file_path': '/opt/skillrl-skills/sum-check.md'},
                'result': rendered, 'call_id': 'read-1', 'error': False}, {'type': 'tool', 'name': 'Write', 'error': False}], 'backend': 'claude-code'}
            path = Path(tmp) / 'actor/native-requests/key/provider'
            begin(path, {'messages': [{'role': 'tool', 'tool_call_id': 'read-1', 'content': rendered}]})
            seal(path, {'complete': True})
            self.assertTrue(native_exposures(execution, [entry], Path(tmp) / 'actor')[0]['body_proven'])
            execution['events'][0]['result'] = rendered[:-10]
            self.assertFalse(native_exposures(execution, [entry], Path(tmp) / 'actor')[0]['body_proven'])

    def test_round_15_workbuddy_export_keeps_private_grader_out(self):
        import io
        import tarfile
        from skillrl.workbuddy import export
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / 'dataset/code/example'
            (source / 'environment').mkdir(parents=True)
            (source / 'instruction.md').write_bytes(b'Original instruction.\r\n')
            (source / 'private-grader.txt').write_text('private oracle must stay at source')
            with tarfile.open(source / 'environment/workspace.tar.gz', 'w:gz') as archive:
                entry = tarfile.TarInfo('input.json')
                data = b'{"values":[1,2,3]}'
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            destination = Path(tmp) / 'export'
            rows = export(Path(tmp) / 'dataset', [{'id': 'task-1', 'path': 'code/example'}], destination)
            self.assertEqual(rows[0]['instruction'], 'Original instruction.\r\n')
            self.assertFalse((destination / 'task-1/private-grader.txt').exists())
            self.assertEqual(load(destination / 'task-1/input.json'), {'values': [1, 2, 3]})
            with tarfile.open(source / 'environment/workspace.tar.gz', 'w:gz') as archive:
                link = tarfile.TarInfo('bad-link')
                link.type = tarfile.SYMTYPE
                link.linkname = '../../private-grader.txt'
                archive.addfile(link)
            with self.assertRaises(ValueError):
                export(Path(tmp) / 'dataset', [{'id': 'task-1', 'path': 'code/example'}], Path(tmp) / 'bad-export')

    def test_round_16_delayed_grade_does_not_block_next_task(self):
        import threading
        with TemporaryDirectory() as tmp, Provider() as server:
            bench = bundle(tmp, count=2)
            released = threading.Event()
            original_evaluate = bench.evaluate
            original_reply = server.reply
            def delayed(task, workspace, execution):
                if task.id == 'task-1':
                    self.assertTrue(released.wait(3), 'Next task waited for objective grade')
                return original_evaluate(task, workspace, execution)
            def reply(data):
                if data['model'] == 'executor':
                    if json.loads(data['messages'][1]['content'])['task']['id'] == 'task-2':
                        released.set()
                return original_reply(data)
            bench.evaluate = delayed
            server.reply = reply
            self.assertTrue(pipeline(tmp, bench, server).run()['complete'])
            self.assertTrue(released.is_set())

    def test_round_17_budget_limit_prevents_tool_delivery(self):
        from skillrl.budget import Budget
        with TemporaryDirectory() as tmp, Provider() as server:
            messages = [{'role': 'system', 'content': 'Test'}, {'role': 'user', 'content': json.dumps({'task': {'id': 'task-1'}, 'catalog': []})}]
            budget = Budget(input_tokens=1)
            with self.assertRaises(RuntimeError):
                budget.call(server.model('executor'), messages, [], Path(tmp) / 'request')
            self.assertTrue((Path(tmp) / 'request/result.json').exists())
            self.assertEqual(len(server.requests), 1)

    def test_round_18_cli_and_skill_mounts_both_survive(self):
        from skillrl.environments import DockerEnvironment
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            workspace.mkdir()
            environment = DockerEnvironment(workspace, 'pinned-image')
            environment.cli_mount = Path(tmp) / 'claude'
            environment.cli_mount.write_text('fixture')
            environment.skill_mount = Path(tmp) / 'skills'
            environment.skill_mount.mkdir()
            with patch.object(environment, 'command', side_effect=['owned-cid', '']) as command:
                environment.start()
            create = command.call_args_list[0].args[0]
            self.assertTrue(any('target=/opt/skillrl-claude,readonly' in arg for arg in create))
            self.assertTrue(any('target=/opt/skillrl-skills,readonly' in arg for arg in create))

    def test_round_19_review_fixture_covers_native_Bash(self):
        with Provider() as server:
            context = {'events': [{'type': 'model'}, {'type': 'tool', 'name': 'Bash'}]}
            response = server.reply({'model': 'judge', 'messages': [{'role': 'user', 'content': 'Finding\n' + json.dumps(context)}]})
            self.assertEqual(json.loads(response['content'])['event_indices'], [1])

    def test_round_20_custom_benchmark_business_action_contract(self):
        with TemporaryDirectory() as tmp:
            lib = seed(tmp, ['sum-check'])
            entry = lib.state()['active']['sum-check']
            entry['content'] = (lib.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
            rendered = entry['content']
            execution = {'backend': 'claude-code', 'events': [
                {'type': 'tool', 'name': 'Read', 'arguments': {'file_path': '/opt/skillrl-skills/sum-check.md'},
                 'result': rendered, 'call_id': 'read-1', 'error': False},
                {'type': 'tool', 'name': 'BrowserClick', 'error': False}]}
            request = Path(tmp) / 'actor/native-requests/key/provider'
            begin(request, {'messages': [{'role': 'tool', 'tool_call_id': 'read-1', 'content': rendered}]})
            seal(request, {'message': {'role': 'assistant', 'content': 'test'}})
            result = native_exposures(execution, [entry], Path(tmp) / 'actor', ['BrowserClick'])
            self.assertEqual(result[0]['proof'][0]['subsequent_actions'], [1])

    def test_round_21_standard_null_tool_calls_is_complete_text(self):
        with TemporaryDirectory() as tmp, Provider() as server:
            server.reply = lambda payload: {'role': 'assistant', 'content': 'Complete text response.', 'tool_calls': None}
            result = server.model('executor').call([{'role': 'user', 'content': 'test'}], [], Path(tmp) / 'request')
            self.assertEqual(result['message']['content'], 'Complete text response.')

    def test_round_22_failed_request_unknown_usage_is_null(self):
        with TemporaryDirectory() as tmp, Provider(reject_role='executor') as server:
            report = pipeline(tmp, bundle(tmp), server).run()
            cost = report['cost']['executor']
            self.assertIsNone(cost['input_tokens'])
            self.assertIsNone(cost['output_tokens'])
            self.assertEqual(cost['request_attempts'], 1)
            self.assertEqual(cost['complete_responses'], 0)

    def test_round_23_invalid_complete_response_usage_is_counted(self):
        with TemporaryDirectory() as tmp, Provider() as server:
            server.reply = lambda payload: {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'bad-json', 'type': 'function', 'function': {'name': 'Read', 'arguments': 'invalid-json'}}]}
            report = pipeline(tmp, bundle(tmp), server).run()
            cost = report['cost']['executor']
            self.assertEqual(cost['input_tokens'], 100)
            self.assertEqual(cost['output_tokens'], 20)
            self.assertEqual(cost['invalid_complete_responses'], 1)
            self.assertEqual(cost['complete_responses'], 0)
