"""Generator-only manager for the pinned upstream NeMo/verl recipe.

Import requires real verl/ray/torch. CPU contract checks do not fake these modules.
Rejected/missing captures abort the batch before optimization; no dummy reward rows.
"""
import os
from pathlib import Path
import uuid

from recipe.nemo_gym import agent_loop as upstream

from evaluation.firewall import EvaluationFirewall
from training.skill_contract import TrainingRejected
from training.rollout_handoff import prepare_rollout_batch


class SkillGeneratorAgentLoopManager(upstream.NeMoGymAgentLoopManager):
    async def _async_generate_sequences(self, prompts):
        if prompts.meta_info.get("validate", False):
            raise TrainingRejected("Independent Verified evaluation must use its separate launcher")
        step = prompts.meta_info.get("global_steps")
        if type(step) is not int or step < 0:
            raise TrainingRejected("Training rollout must identify the actual policy update step")
        key_path = Path(os.environ["WORKENV_CAPTURE_KEY_FILE"])
        key = key_path.read_bytes()
        firewall = EvaluationFirewall.load(Path(os.environ["WORKENV_VERIFIED_MANIFEST"]),
                                          Path(os.environ["WORKENV_TRAINING_REGISTRY"]))
        expected_executor = os.environ["WORKENV_FIXED_EXECUTOR_ID"]
        expected_generator = os.environ["WORKENV_GENERATOR_POLICY_ID"]
        training_run = os.environ["WORKENV_TRAINING_RUN_ID"]
        examples = upstream._build_nemo_gym_examples(prompts, self.rollout_config, validate=False)
        for row in examples:
            firewall.check_training_record(row.get("provenance", {}))
            firewall.check_training_record(row.get("downstream_provenance", {}))
            if row["provenance"]["task_id"] == row["downstream_provenance"]["task_id"]:
                raise TrainingRejected("Frozen Skill must be executed on a distinct task")
            row["generator_global_step"] = step
            row["training_run_id"] = training_run
            row["episode_id"] = {"rollout_id": "generator-" + uuid.uuid4().hex, "attempt": 0}
            row["task_id"] = {"taskset": "toolathlon_gym:" + training_run,
                              "task_id": row["downstream_provenance"]["task_id"]}
        captures = [None] * len(examples)
        iterator = self._rch.run_examples(examples=examples, head_server_config=self._head_server_config)
        for task in iterator:
            row, raw = await task
            index = row["_rowidx"]
            if type(index) is not int or not 0 <= index < len(captures) or captures[index] is not None:
                raise TrainingRejected("Duplicate or invalid rollout row identity")
            captures[index] = raw.get("skill_capture")
        if not captures or any(capture is None for capture in captures):
            raise TrainingRejected("Incomplete Generator batch; no optimizer update permitted")
        results = prepare_rollout_batch(requests=examples, captures=captures,
            capture_key=key, firewall=firewall, generator_policy_id=expected_generator,
            fixed_executor_id=expected_executor, training_run_id=training_run,
            global_step=step, prompt_limit=self.rollout_config.prompt_length,
            response_limit=self.rollout_config.response_length)
        outputs = [upstream._nemo_gym_result_to_verl(
            result=result, tokenizer=self._tokenizer,
            prompt_length=self.rollout_config.prompt_length,
            response_length=self.rollout_config.response_length,
        ) for result in results]
        output = upstream._postprocess(self, outputs, input_non_tensor_batch=prompts.non_tensor_batch, validate=False)
        output.meta_info.update(global_steps=step, timing={}, rollout_metrics=upstream._compute_rollout_metrics(results))
        output.meta_info["downstream_executions"] = [result["downstream_execution"] for result in results]
        return output
