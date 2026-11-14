"""Real model sampling / gradient diagnostics; never admit diagnostic tokens to GRPO."""
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.benchmark_tasks.store import save
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from training.markdown_contract import native_prompt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=str(ROOT / ".runtime/models/Qwen3-8B"))
    parser.add_argument("--experience-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu", type=int, default=4)
    parser.add_argument("--backend", choices=("transformers", "vllm"), default="transformers")
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--check-backward", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--require-insert", action="store_true",
                        help="Diagnostic vLLM JSON constraint; separate from the native training sampler")
    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    args.output.mkdir(parents=True, exist_ok=False)
    receipt = {"observed_at": datetime.now(timezone.utc).isoformat(), "backend": args.backend,
               "model": args.model, "gpu": args.gpu, "training_admitted": False,
               "actual_grpo_updates": 0, "passed": False}
    receipt["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
        ("scripts/check_generator_runtime.py", "training/markdown_contract.py",
         "integrations/skill_generator/editor.py", "integrations/skill_generator/transport.py")}
    receipt["model_source"] = json.loads((Path(args.model) / "deployment_source.json").read_text())
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        receipt["versions"] = {name: importlib.metadata.version(name) for name in
                               ("torch", "vllm", "ray", "transformers", "tensordict", "verl")}
        receipt["cuda"] = {"available": torch.cuda.is_available(), "runtime": torch.version.cuda,
                           "device": torch.cuda.get_device_name(0)}
        # Actual imports, rather than signature-only checks.
        for module in ("verl.trainer.main_ppo", "training.markdown_verl"):
            importlib.import_module(module)
        receipt["native_runtime_import_verified"] = True
        editor = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"),
                             SkillLibrary(ROOT / ".runtime/skill_library"))
        request = editor.prepare(args.experience_id, generator_policy_id="deployment-diagnostic",
                                 parent=editor.library.empty_version, max_steps=30, observation_chars=1000)
        messages = native_prompt(request)
        tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=False)
        ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                            enable_thinking=True)
        receipt["prompt_tokens"] = len(ids)
        receipt["sampling"] = {"temperature": args.temperature, "top_p": 1.0,
                               "max_new_tokens": args.max_new_tokens, "seed": args.seed}
        save(args.output / "request.json", request)
        if args.backend == "vllm":
            if args.check_backward:
                raise ValueError("Backward check requires the transformers backend")
            from vllm import LLM, SamplingParams
            from vllm.sampling_params import StructuredOutputsParams
            engine = LLM(model=args.model, trust_remote_code=False, dtype="bfloat16",
                         max_model_len=max(len(ids) + args.max_new_tokens, 4096),
                         gpu_memory_utilization=0.35, enforce_eager=True, seed=args.seed,
                         max_num_seqs=4, max_num_batched_tokens=8192)
            constraint = None
            if args.require_insert:
                schema = {"type": "array", "minItems": 1, "maxItems": 3, "items": {
                    "type": "object", "properties": {"name": {"const": "new_skill_insert"},
                    "arguments": {"type": "object", "properties": {
                        "skill_name": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["skill_name", "content"], "additionalProperties": False}},
                    "required": ["name", "arguments"], "additionalProperties": False}}
                constraint = StructuredOutputsParams(json=schema)
                receipt["diagnostic_json_constraint"] = schema
            item = engine.generate([{"prompt_token_ids": ids}], SamplingParams(
                temperature=args.temperature, top_p=1.0, max_tokens=args.max_new_tokens,
                logprobs=1, structured_outputs=constraint))[0].outputs[0]
            text, response_ids = item.text, list(item.token_ids)
            logprobs = [row[token].logprob for row, token in zip(item.logprobs, response_ids, strict=True)]
            receipt["finish_reason"] = item.finish_reason
            engine.llm_engine.engine_core.shutdown()
            if torch.distributed.is_initialized():
                torch.distributed.destroy_process_group()
        else:
            torch.manual_seed(args.seed)
            model = AutoModelForCausalLM.from_pretrained(args.model, trust_remote_code=False,
                torch_dtype=torch.bfloat16, attn_implementation="sdpa").to("cuda:0")
            inputs = torch.tensor([ids], device="cuda:0")
            model.eval()
            with torch.no_grad():
                result = model.generate(inputs, attention_mask=torch.ones_like(inputs),
                    max_new_tokens=args.max_new_tokens, do_sample=True, temperature=args.temperature,
                    top_p=1.0, top_k=0, return_dict_in_generate=True, output_scores=True,
                    pad_token_id=tokenizer.eos_token_id)
                response_ids = result.sequences[0, len(ids):].tolist()
                logprobs = model.compute_transition_scores(result.sequences, result.scores,
                                                          normalize_logits=True)[0].tolist()
            text = tokenizer.decode(response_ids, skip_special_tokens=True)
            receipt["finish_reason"] = "eos" if response_ids[-1] == tokenizer.eos_token_id else "length"
            if args.check_backward:
                sequence = result.sequences.detach()
                del result
                gc.collect()
                torch.cuda.empty_cache()
                model.train()
                model.gradient_checkpointing_enable()
                labels = sequence.clone()
                labels[:, :len(ids)] = -100
                loss = model(sequence, labels=labels, use_cache=False).loss
                loss.backward()
                grads = [p.grad for p in model.parameters() if p.grad is not None]
                receipt["backward"] = {"loss": loss.item(), "gradient_tensors": len(grads),
                    "finite": all(torch.isfinite(g).all().item() for g in grads),
                    "nonzero": any(torch.count_nonzero(g).item() > 0 for g in grads),
                    "optimizer_executed": False}
                if not receipt["backward"]["finite"] or not receipt["backward"]["nonzero"]:
                    raise RuntimeError("Invalid real model gradients")
        save(args.output / "sample.json", {"kind": "deployment_diagnostic", "prompt_ids": ids,
            "response_ids": response_ids, "sampling_logprobs": logprobs, "text": text,
            "training_admitted": False})
        receipt["response_tokens"] = len(response_ids)
        candidate = editor.apply(request, text, sampling={"kind": "deployment_diagnostic"})
        save(args.output / "candidate.json", candidate)
        receipt["candidate_id"] = candidate["id"]
        receipt["skill_version"] = candidate["version"]
        receipt["operation_count"] = len(candidate["operations"])
        receipt["skill_count"] = len(editor.library.get(candidate["version"]))
        receipt["nonempty_skill_generated"] = candidate["version"] != candidate["parent"]
        receipt["passed"] = receipt["finish_reason"] in ("eos", "stop")
    except BaseException as exc:
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        raise
    finally:
        save(args.output / "receipt.json", receipt)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return 0 if receipt["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
