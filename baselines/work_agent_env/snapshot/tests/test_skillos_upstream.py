"""CPU boundary regression against actual pinned SkillOS code; no model calls."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import json
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import httpx

from integrations.benchmark_tasks import make_task, make_task_set
from integrations.skill_generator.skillos.bridge import collect, make_plan, Journal, RoleClient
from integrations.skill_generator.skillos.upstream import Upstream


class Tokenizer:
    def encode(self, text, **kwargs):
        return list(text.encode())

    def apply_chat_template(self, messages, **kwargs):
        return list(json.dumps(messages).encode())


def tasks():
    return make_task_set([make_task(benchmark='toolathon-gym', task_id='task' + str(i),
        source={'plugin': 'test', 'revision': 'fixture'}, prompt='Write and verify report ' + str(i),
        environment={'backend': 'docker', 'config': {}}, evaluation={'backend': 'command_json', 'config': {}})
        for i in range(10)])


def groups(task_set):
    return [{'group_id': 'g1', 'ordered_task_ids': [t['uid'] for t in task_set['tasks']], 'rollout_seeds': list(range(8))}]


class FixedExecutor:
    def __init__(self, root):
        self.root, self.executor_id, self.seen = Path(root), 'fixed-api-fixture', []

    async def execute(self, *, candidate, task):
        self.seen.append((task['uid'], candidate['version']))
        return {'task': task, 'receipt': {'status': 'completed', 'error': None,
            'evaluation': {'valid': True, 'passed': True, 'reward': 1.0, 'verifier_version': 'fixture'}},
            'executor': {'fixed_executor_id': self.executor_id}, 'trajectory': [],
            'calls': [{'name': 'write', 'arguments': {}, 'output': 'saved and verified'}],
            'artifacts': [], 'costs': {}}


class FakeRole:
    def __init__(self, name, upstream, directory):
        self.name, self.upstream, self.directory = name, upstream, directory

    async def query_exact(self, context, messages, seed, budget, schema=None, expected_prompt_ids=None):
        if self.name == 'selfjudge':
            # Position zero deliberately disagrees with the rest: it must not
            # contribute to the downstream task reward.
            signal = context['rollout'] % 2 == 0
            text = json.dumps({'success': not signal if context['position'] == 0 else signal,
                               'rationale': 'fixture', 'evidence_step': 1})
        elif self.name == 'contentjudge':
            text = json.dumps({'VALID': True, 'ISSUES': [], 'EXPLANATION': 'fixture'})
        else:
            name = 'verify-artifact'
            content = '---\nname: verify-artifact\ndescription: Check saved artifacts.\n---\n# Workflow\nReopen and verify.\n'
            operations = ([{'name': 'new_skill_insert', 'arguments': {'skill_name': name, 'content': content}}]
                if context['position'] == 0 else [{'name': 'bad-tool', 'arguments': {}}]
                if context['position'] == 1 else [])
            text = json.dumps(operations)
        ids, prompt = list(text.encode()), expected_prompt_ids or [1, 2]
        path = self.directory / (self.name + '-' + str(context['rollout']) + '-' + str(context['position']) + '.json')
        self.upstream.io.save(path, {'test_fixture': True, 'context': context, 'text': text})
        return {'response': {'choices': [{'message': {'content': text}, 'finish_reason': 'stop', 'token_ids': ids}]},
            'prompt_ids': prompt, 'raw': self.upstream.io.ref(path),
            'sampled_model_raw_logprobs': [-1.0] * len(ids), 'sampled_behavior_logprobs': [-1.0] * len(ids),
            'probability_definition': 'diagnostic fixture; never training admitted'}


class UpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = Upstream()

    def test_fixed_sources_and_configuration(self):
        from scripts.run_skillos_upstream import load_config, ROOT
        config = load_config(ROOT / 'configs/skillos_upstream.json')
        self.assertEqual(config['rollouts_per_group'], 8)
        self.assertFalse(config['normalize_std'])
        self.assertGreater(len(self.upstream.manifest['files']), 200)

    def test_task_group_rejects_two_position_or_incomplete_sequences(self):
        task_set = tasks()
        bad = groups(task_set)
        bad[0]['ordered_task_ids'] = bad[0]['ordered_task_ids'][:2]
        with self.assertRaises(ValueError):
            make_plan(task_set, groups=bad, run_id='test')
        with self.assertRaises(ValueError):
            make_plan(task_set, groups=groups(task_set), run_id='test', engineering=False)

    def test_full_8x10_original_loop_reward_and_actor_contract(self):
        task_set = tasks()
        plan = make_plan(task_set, groups=groups(task_set), run_id='cpu-fixture')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            roles = SimpleNamespace(config={'roles': {n: {'model_revision': 'fixture'}
                for n in ('curator', 'selfjudge', 'contentjudge')}}, tokenizer=lambda _: Tokenizer())
            fixed = FixedExecutor(directory)
            clients = {n: FakeRole(n, self.upstream, directory / 'raw-fixtures') for n in roles.config['roles']}
            with redirect_stdout(io.StringIO()):
                batch, summary = asyncio.run(collect(upstream=self.upstream, plan=plan, task_set=task_set,
                    directory=directory / 'run', roles=roles, fixed_executor=fixed, clients=clients))
            self.assertEqual(len(fixed.seen), 80)
            self.assertEqual(summary['fragments'], 80)
            self.assertEqual(summary['rollouts'], 8)
            self.assertFalse(batch['training_admitted'])
            self.assertEqual(batch['actual_parameter_updates'], 0)
            self.assertTrue(self.upstream.actor_contract.verify_reward_sources(batch, batch['fragments']))
            first, second = batch['groups'][0]['rewards'][:2]
            self.assertAlmostEqual(first - second, 1.0)
            for slot in range(8):
                rows = [r for r in batch['fragments'] if r['rollout'] == slot]
                self.assertEqual(len({r['advantage_minus_group_mean'] for r in rows}), 1)
                self.assertTrue(all(r['response_mask'] == [1] * len(r['response_token_ids']) for r in rows))
            record = json.loads((directory / 'run/groups/g1/rollout0/position1.json').read_text())
            self.assertEqual(record['retrieved_names'], ['verify-artifact'])
            self.assertFalse(record['tool_results'][0]['executed'])
            self.assertEqual(record['auxiliary']['r_fc'], 0)
            self.assertEqual(len({v for uid, v in fixed.seen if uid.endswith(':task0')}), 1)
            # Original actor contract rejects the old standard-deviation-scaled advantages.
            bad = deepcopy(batch)
            for fragment in bad['fragments']:
                fragment['advantage_minus_group_mean'] *= 2
            with self.assertRaises(AssertionError):
                self.upstream.actor_contract.validate_batch(bad, 'V000', 1, True)

    def test_original_retrieval_and_missing_reward_rules(self):
        repo = self.upstream.reward.SkillRepo()
        self.assertEqual(self.upstream.helpers.bm25(repo, 'anything'), [])
        rewards = self.upstream.reward.group_task_rewards([[False] + [True] * 9 for _ in range(8)])
        self.assertEqual(rewards['primary'], [1.0] * 8)
        missing = self.upstream.reward.group_task_rewards([[True] + [None] * 9] + [[False] * 10 for _ in range(7)])
        self.assertTrue(missing['drop_primary_group'])

    def test_original_loss_weight_is_equal_per_complete_rollout(self):
        # Variable completion lengths must not change a complete rollout's
        # weight. Four-rank reduction is the exact source204 implementation.
        totals, rows = {}, []
        for g in range(32):
            for slot in range(8):
                fragments = [{'group_id': str(g), 'rollout': slot, 'position': p,
                              'response_mask': [1] * (p + 1)} for p in range(10)]
                rows.extend(fragments)
                totals[str(g), slot] = sum(len(f['response_mask']) for f in fragments)
                self.assertAlmostEqual(sum(self.upstream.actor_math.fragment_weight(f, totals, 32)
                                           for f in fragments), 4 / (32 * 8))
        ranks = [self.upstream.actor_math.assigned(rows, rank) for rank in range(4)]
        self.assertEqual([len(x) for x in ranks], [640] * 4)

    def test_transport_retains_actual_ids_probabilities_and_refuses_replay(self):
        async def scenario(tmp):
            journal = Journal(Path(tmp) / 'run', {'run_id': 'transport'}, self.upstream.io)
            role = {'served_model': 'curator', 'policy_version': 'V000', 'context_limit': 20480,
                    'chat_template_kwargs': {'enable_thinking': True}, 'base_url': 'http://fixture/v1', 'backend': 'vllm'}
            context = {'role': 'curator', 'policy_version': 'V000'}
            messages = [{'role': 'user', 'content': 'test'}]
            prompt = Tokenizer().apply_chat_template(messages)
            actual = [12, 13, 14]
            def response(request):
                body = json.loads(request.content)
                self.assertEqual(body['prompt'], prompt)
                self.assertNotIn('response_format', body)
                return httpx.Response(200, json={'prompt_token_ids': prompt, 'choices': [{
                    'text': '<think>fixture reasoning</think>[]', 'token_ids': actual,
                    'finish_reason': 'length', 'logprobs': {'tokens': ['token_id:' + str(t) for t in actual],
                    'token_logprobs': [-0.2, -0.3, -0.4]}}], 'usage': {'completion_tokens': 3}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                client = RoleClient(name='curator', role=role, tokenizer=Tokenizer(), journal=journal,
                    upstream=self.upstream, http_client=http, semaphore=asyncio.Semaphore(1))
                result = await client.query_exact(context, messages, 42, 4096, expected_prompt_ids=prompt)
                self.assertEqual(result['response']['choices'][0]['token_ids'], actual)
                self.assertEqual(result['response']['choices'][0]['message']['content'], '[]')
                self.assertEqual(result['response']['choices'][0]['finish_reason'], 'length')
                self.assertEqual(result['sampled_model_raw_logprobs'], [-0.2, -0.3, -0.4])
                raw = self.upstream.io.verify_ref(result['raw'])
                original = json.loads(self.upstream.io.verify_ref(raw['transport_raw'])['response_text'])
                self.assertIn('<think>fixture reasoning</think>', original['choices'][0]['text'])
                self.assertEqual(original['choices'][0]['token_ids'], actual)
                with self.assertRaisesRegex(ValueError, 'already sent'):
                    await client.query_exact(context, messages, 42, 4096)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))

    def test_transport_does_not_reconstruct_missing_token_ids(self):
        async def scenario(tmp):
            journal = Journal(Path(tmp) / 'run', {'run_id': 'missing-ids'}, self.upstream.io)
            role = {'served_model': 'curator', 'policy_version': 'V000', 'context_limit': 20480,
                    'chat_template_kwargs': {'enable_thinking': True}, 'base_url': 'http://fixture/v1', 'backend': 'vllm'}
            def response(request):
                return httpx.Response(200, json={'prompt_token_ids': json.loads(request.content)['prompt'],
                    'choices': [{'text': '[]', 'finish_reason': 'stop'}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                client = RoleClient(name='curator', role=role, tokenizer=Tokenizer(), journal=journal,
                    upstream=self.upstream, http_client=http, semaphore=asyncio.Semaphore(1))
                with self.assertRaisesRegex(ValueError, 'actual token IDs'):
                    await client.query_exact({'role': 'curator', 'policy_version': 'V000'},
                                             [{'role': 'user', 'content': 'test'}], 42, 4096)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))

    def test_unknown_executor_result_blocks_group_export(self):
        class UnknownExecutor(FixedExecutor):
            async def execute(self, **kwargs):
                execution = await super().execute(**kwargs)
                execution['receipt']['evaluation'] = None
                return execution
        task_set = tasks()
        plan = make_plan(task_set, groups=groups(task_set), run_id='unknown')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            roles = SimpleNamespace(config={'roles': {n: {'model_revision': 'fixture'}
                for n in ('curator', 'selfjudge', 'contentjudge')}}, tokenizer=lambda _: Tokenizer())
            clients = {n: FakeRole(n, self.upstream, directory / 'raw-fixtures') for n in roles.config['roles']}
            with self.assertRaisesRegex(ValueError, 'Incomplete SkillOS groups'):
                asyncio.run(collect(upstream=self.upstream, plan=plan, task_set=task_set,
                    directory=directory / 'run', roles=roles, fixed_executor=UnknownExecutor(directory), clients=clients))
            self.assertFalse((directory / 'run/collected_batch.json').exists())


if __name__ == '__main__':
    unittest.main()
