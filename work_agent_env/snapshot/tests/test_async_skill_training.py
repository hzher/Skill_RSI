"""Diagnostic fixtures exercise scheduling and admission; no training samples or optimizer are emitted."""
import asyncio
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from integrations.benchmark_tasks import digest, load_task_set
from integrations.benchmark_tasks.store import TaskStore
from integrations.executors import ExecutorOptions
from integrations.executors.api import APIExecutor
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from integrations.orchestration import AsyncPipeline, PipelineLimits, StateConflict
from integrations.orchestration.executor import DownstreamExecutor
from training.algorithms import AlgorithmRegistry, GRPO
from training.markdown_data import prepare_group
from training.markdown_contract import native_prompt, validate_sample
from training.rewards import seal_groups
from training.skill_contract import TrainingRejected, sign_capture
from training.updates import UpdateGate
from tests.test_generic_runtime import install, Driver


class DiagnosticSampler:
    """Known scripted fixture; fabricated fixture tokens never carry native admission."""
    def __init__(self):
        self.active = self.peak = self.calls = 0

    async def sample(self, *, request, policy, rollout_id, slot):
        self.active += 1
        self.peak = max(self.active, self.peak)
        self.calls += 1
        try:
            await asyncio.sleep(.01 if slot % 2 else .03)
            content = '---\nname: Fixture report\ndescription: Diagnostic fixture only.\n---\n# Workflow\nfixture_write=' + ('true' if slot % 2 else 'false') + '\n'
            text = json.dumps([{"name": "functions.new_skill_insert", "arguments": {"skill_name": "Fixture report", "content": content}}])
            return {"schema_version": "workagent-generator-sample-v1", "kind": "diagnostic", "actor": "skill_generator",
                "request_id": request["id"], "provider_request_id": "fixture-" + rollout_id, "policy": deepcopy(policy),
                "prompt_ids": [11, 12], "response_ids": [100 + slot], "response_logprobs": [-.2],
                "text": text, "finish_reason": "stop"}
        finally:
            self.active -= 1


class AsyncTrainingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install(self.root, "seed")
        install(self.root, "downstream")
        registry = TaskStore(self.root)
        self.seed = load_task_set(registry.register("seed")["task_set"])["tasks"][0]
        self.downstream = load_task_set(registry.register("downstream")["task_set"])["tasks"][0]
        self.store = ExperienceStore(self.root / ".runtime/experience")
        self.library = SkillLibrary(self.root / ".runtime/skill_library")
        self.editor = SkillEditor(self.store, self.library)
        episode = self.root / "seed-episode"
        episode.mkdir()
        self.experience_id = self.store.capture(task=self.seed,
            receipt={"task_uid": self.seed["uid"], "task_sha256": digest(self.seed),
                     "evaluation": {"valid": True, "passed": True, "reward": 1}},
            trajectory=[{"role": "assistant", "text": "Fixture completed", "calls": []}],
            directory=episode, executor={"kind": "diagnostic"})["id"]
        self.policy = {"run_id": "unit", "step": 1, "policy_id": "unit-policy", "model_revision": "unit-model", "tokenizer_revision": "unit-tokens"}
        self.pair = {"schema_version": "workagent-training-pair-v1", "group_id": "unit-group",
            "experience_id": self.experience_id, "downstream_task": self.downstream, "parent": self.library.empty_version,
            "seed_provenance": None, "downstream_provenance": None}
        self.group = prepare_group(self.pair, editor=self.editor, policy_id=self.policy["policy_id"])
        self.sampler = DiagnosticSampler()
        outer = self
        class Fixed:
            executor_id = "fixture-fixed-executor"
            def __init__(owned): owned.active = owned.peak = 0
            async def execute(owned, *, candidate, task):
                owned.active += 1
                owned.peak = max(owned.peak, owned.active)
                try:
                    await asyncio.sleep(.02)
                    passed = 'fixture_write=true' in '\n'.join(outer.library.get(candidate['version']).values())
                    ref = outer.store.capture(task=task,
                        receipt={"task_uid": task["uid"], "task_sha256": digest(task),
                                 "evaluation": {"valid": True, "passed": passed, "reward": float(passed)}},
                        trajectory=[], directory=outer.root / "no-workspace",
                        executor={"fixed_executor_id": owned.executor_id, "skill_version": candidate["version"]})
                    return outer.store.get(ref['id'])
                finally: owned.active -= 1
        self.fixed = Fixed()

    def pipeline(self, **kwargs):
        return AsyncPipeline(directory=self.root / "pipeline", editor=self.editor, sampler=self.sampler,
            fixed_executor=self.fixed, algorithm=GRPO(group_size=4),
            limits=PipelineLimits(generator_concurrency=2, executor_concurrency=2, queue_capacity=1), **kwargs)

    async def collect(self, pipeline=None):
        return await (pipeline or self.pipeline()).collect(batch_id="batch", policy=self.policy, groups=[self.group])

    async def test_overlapping_bounded_stages_reorder_slots_and_bind_rewards(self):
        batch = await self.collect()
        self.assertFalse(batch["training_admitted"])
        self.assertEqual([row["slot"] for row in batch["rows"]], [0, 1, 2, 3])
        self.assertEqual([row["reward"] for row in batch["rows"]], [0, 1, 0, 1])
        self.assertEqual(self.sampler.peak, 2)
        self.assertEqual(self.fixed.peak, 2)
        self.assertIsNone(self.library.head())
        self.assertEqual(batch["rows"][0]["sample"]["response_ids"], [100])
        self.assertEqual(batch["rows"][0]["sample"]["response_logprobs"], [-.2])
        expected = .5 / (math.sqrt(1/3) + 1e-6)
        self.assertAlmostEqual(batch["groups"][0]["audit_advantages"][1], expected)

    async def test_repeated_batch_is_not_replayed_after_restart(self):
        await self.collect()
        before = self.sampler.calls
        with self.assertRaises(StateConflict): await self.collect()
        self.assertEqual(self.sampler.calls, before)

    async def test_unknown_result_excludes_whole_group_and_leaves_evidence(self):
        async def fail(**kwargs): raise TimeoutError("fixture timeout")
        self.fixed.execute = fail
        with self.assertRaises(TrainingRejected): await asyncio.wait_for(self.collect(), 2)
        self.assertTrue((self.root / "pipeline/failures.json").exists())
        with self.assertRaises(StateConflict): await self.collect()

    async def test_producer_failure_does_not_deadlock_a_full_queue(self):
        from unittest.mock import patch
        pipeline = self.pipeline()
        original = pipeline.journal.claim
        def claim(identity):
            if "rollout_id" in identity: raise StateConflict("fixture storage failure")
            return original(identity)
        with patch.object(pipeline.journal, "claim", side_effect=claim):
            with self.assertRaises(StateConflict): await asyncio.wait_for(self.collect(pipeline), 2)

    async def test_diagnostic_batch_cannot_authorize_an_optimizer(self):
        batch = await self.collect()
        gate = UpdateGate(self.root / "updates", run_id="unit", capture_key=b"k" * 32)
        with self.assertRaises(TrainingRejected): gate.ready(batch)

    async def test_incomplete_replayed_and_mixed_policy_groups_rejected(self):
        batch = await self.collect()
        rows = batch["rows"]
        expected = {"unit-group": [row["rollout_id"] for row in rows]}
        def seal(values):
            return seal_groups(values, expected=expected, policy=self.policy, algorithm=GRPO(), mode="diagnostic")
        with self.assertRaises(TrainingRejected): seal(rows[:-1])
        with self.assertRaises(TrainingRejected): seal([*rows, rows[0]])
        changed = deepcopy(rows)
        changed[0]["sample"]["policy"]["step"] += 1
        with self.assertRaises(TrainingRejected): seal(changed)
        changed = deepcopy(rows)
        changed[1]["sample"]["provider_request_id"] = changed[0]["sample"]["provider_request_id"]
        with self.assertRaises(TrainingRejected): seal(changed)
        changed = deepcopy(rows)
        changed[0]["reward"] = 1
        with self.assertRaises(TrainingRejected): seal(changed)

    async def test_api_usage_and_missing_logprobs_have_no_token_fallback(self):
        sample = await self.sampler.sample(request=self.group["request"], policy=self.policy, rollout_id="unit", slot=0)
        for change in ({"response_logprobs": None}, {"response_ids": []}, {"actor": "executor"}, {"kind": "api_response"}):
            with self.assertRaises(TrainingRejected):
                validate_sample({**sample, **change}, policy=self.policy, request_id=self.group["request"]["id"])

    async def test_no_training_calls_before_lineage_review(self):
        with self.assertRaises(TrainingRejected):
            await self.collect(self.pipeline(mode="training", firewall=None, capture_key=b"k" * 32))
        self.assertEqual(self.sampler.calls, 0)

    async def test_wrong_task_and_changed_executor_exclude_rewards(self):
        original = self.fixed.execute
        async def wrong_task(*, candidate, task):
            modified = deepcopy(task)
            modified.update(task_id="wrong", uid=task["benchmark"] + ":wrong")
            return await original(candidate=candidate, task=modified)
        self.fixed.execute = wrong_task
        with self.assertRaises(TrainingRejected): await self.collect()

    async def test_cancellation_joins_producers_and_consumers(self):
        task = asyncio.create_task(self.collect())
        await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.sampler.active, 0)
        self.assertEqual(self.fixed.active, 0)

    @unittest.skipUnless(os.environ.get("RUN_CONTAINER_CHECKS") == "1", "Set RUN_CONTAINER_CHECKS=1 for actual downstream Docker")
    async def test_real_docker_group_gets_distinct_original_rewards(self):
        class SkillDriver(Driver):
            async def turn(owned, system, tools, history):
                if "fixture_write=false" in system:
                    return {"text": "Diagnostic no-write action", "calls": [], "usage": {}}
                return await super().turn(system, tools, history)
        backend = APIExecutor(project_root=self.root, model_factory=SkillDriver)
        fixed = await DownstreamExecutor.create(project_root=self.root, options=ExecutorOptions("openai", model="fixture", task_timeout=120),
            tasks=[self.downstream], backend=backend)
        pipeline = AsyncPipeline(directory=self.root / "real-pipeline", editor=self.editor, sampler=self.sampler,
            fixed_executor=fixed, algorithm=GRPO(), limits=PipelineLimits(generator_concurrency=2, executor_concurrency=2, queue_capacity=1))
        batch = await self.collect(pipeline)
        self.assertEqual([row["reward"] for row in batch["rows"]], [0, 1, 0, 1])
        self.assertFalse(batch["training_admitted"])
        if os.environ.get("WORKENV_DIAGNOSTIC_EXPORT"):
            import shutil
            from integrations.benchmark_tasks.store import save
            export = Path(os.environ["WORKENV_DIAGNOSTIC_EXPORT"])
            export.mkdir(parents=True, exist_ok=False)
            save(export / "batch.json", batch)
            shutil.copytree(self.root / "real-pipeline", export / "journal-and-captures")
            shutil.copytree(self.root / ".runtime", export / "runtime-evidence")


class AlgorithmAndUpdateTests(unittest.TestCase):
    def test_zero_variance_and_unregistered_algorithm(self):
        self.assertEqual(GRPO().advantages([1, 1, 1, 1]), [0, 0, 0, 0])
        with self.assertRaises(TrainingRejected): AlgorithmRegistry().resolve("not-installed")
        class Other:
            name="other"; group_size=2; max_policy_lag=0
            def advantages(self, rewards): return [reward - sum(rewards)/2 for reward in rewards]
            def verl_overrides(self): return {"algorithm.adv_estimator": "other"}
        registry=AlgorithmRegistry()
        registry.register("other", Other)
        self.assertEqual(registry.resolve("other").advantages([0, 1]), [-.5, .5])

    def test_installation_barrier_preserves_unknown_and_rejects_wrong_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate=UpdateGate(Path(temporary),run_id="unit",capture_key=b"k"*32)
            job, _=gate.begin_install(0)
            with self.assertRaises(TrainingRejected):
                gate.installed(job,step=1,checkpoint_sha256="a"*64,checkpoint_ref="fixture",batch_id=None)
            gate.unknown(job,error="fixture interruption")
            with self.assertRaises(StateConflict): gate.begin_install(0)
            with self.assertRaises(FileNotFoundError): gate.current_policy(1,tokenizer_revision="fixture")

    def test_policy_version_only_advances_after_confirmed_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate=UpdateGate(Path(temporary),run_id="unit",capture_key=b"k"*32)
            job, _=gate.begin_install(0)
            gate.installed(job,step=0,checkpoint_sha256="a"*64,checkpoint_ref="fixture-initial",batch_id=None)
            policy=gate.current_policy(1,tokenizer_revision="fixture")
            self.assertEqual(policy['model_revision'],'a'*64)
            with self.assertRaises(TrainingRejected): gate.current_policy(2,tokenizer_revision="fixture")
            with self.assertRaises(StateConflict): gate.begin_install(0)

    def test_native_config_uses_actual_logprobs_and_separate_agent_hooks(self):
        from training.markdown_launch import load_config, trainer_config
        root=Path(__file__).resolve().parents[1]
        config=load_config(root/'configs/markdown_training.example.json')
        native=trainer_config(config,Path('/tmp/fixture-native-run'))
        rollout=native['actor_rollout_ref']['rollout']
        self.assertTrue(rollout['calculate_log_probs'])
        self.assertEqual(rollout['n'],4)
        self.assertEqual(native['algorithm']['adv_estimator'],'grpo')
        self.assertEqual(native['trainer']['resume_mode'],'disable')
        self.assertIn('MarkdownCheckpointManager',rollout['checkpoint_manager_class'])
        self.assertEqual(native['data']['train_files'],native['data']['val_files'])
        self.assertFalse(native['trainer']['val_before_train'])
        self.assertLess(native['trainer']['test_freq'],0)

    def test_deployment_tuning_cannot_replace_training_hooks(self):
        from training.markdown_launch import load_config, trainer_config
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / 'configs/generator_h20_smoke.json')
        native = trainer_config(config, Path('/tmp/fixture-h20-run'))
        self.assertEqual(native['actor_rollout_ref']['model']['override_config']['attn_implementation'], 'sdpa')
        self.assertEqual(native['actor_rollout_ref']['rollout']['n'], 4)
        for key, value in {
            'actor_rollout_ref.rollout.calculate_log_probs': False,
            'actor_rollout_ref.rollout.agent.default_agent_loop': 'other',
            'trainer.resume_mode': 'auto',
        }.items():
            body = json.loads((root / 'configs/generator_h20_smoke.json').read_text())
            body['verl_overrides'][key] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'invalid.json'
                path.write_text(json.dumps(body))
                with self.assertRaises(TrainingRejected):
                    load_config(path)
