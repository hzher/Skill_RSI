"""Frozen Qwen3.8 completion server in a separate Transformers 5 runtime.

Text-only Work requests use the original multimodal checkpoint without altering
weights. CUDA devices are selected by the parent launcher before torch imports.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--served-model", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gpu-memory-gib", type=int, default=32)
    args = parser.parse_args()
    import torch
    import transformers
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    from fastapi import FastAPI, HTTPException
    import uvicorn
    print(json.dumps({"event": "frozen_contentjudge_runtime", "torch": torch.__version__,
                      "transformers": transformers.__version__, "model": str(args.model),
                      "visible_cuda_devices": torch.cuda.device_count()}), flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=False)
    memory = {i: str(args.gpu_memory_gib) + "GiB" for i in range(torch.cuda.device_count())}
    memory["cpu"] = "64GiB"
    model = AutoModelForImageTextToText.from_pretrained(args.model, dtype=torch.bfloat16,
        device_map="balanced", max_memory=memory, attn_implementation="sdpa", trust_remote_code=False)
    model.eval()
    model.requires_grad_(False)
    app, lock = FastAPI(), asyncio.Lock()

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": args.served_model, "root": str(args.model),
                          "structured_outputs": "xgrammar_after_thinking"}]}

    @app.get("/v1/fingerprint")
    async def fingerprint():
        async with lock:
            def actual():
                import hashlib
                tensors = {}
                for name, tensor in model.named_parameters(remove_duplicate=False):
                    raw = tensor.detach().contiguous().reshape(-1).view(torch.uint8)
                    sha = hashlib.sha256()
                    for chunk in raw.split(64 * 1024 * 1024):
                        sha.update(chunk.cpu().numpy().tobytes())
                    tensors[name] = {"shape": list(tensor.shape), "dtype": str(tensor.dtype), "sha256": sha.hexdigest()}
                return {"loaded_model_path": str(args.model.resolve()), "tensors": tensors,
                        "structured_outputs": "xgrammar_after_thinking", "pid": __import__('os').getpid()}
            return await asyncio.to_thread(actual)

    def generate(body):
        ids = body["prompt"]
        if not ids or any(type(i) is not int or i < 0 for i in ids):
            raise ValueError("Exact prompt token IDs required")
        if body["model"] != args.served_model or body.get("temperature") != 0 or body.get("logprobs") is not None:
            raise ValueError("This frozen content endpoint only supports configured greedy judging")
        budget = body["max_tokens"]
        if type(budget) is not int or not 1 <= budget <= 16384 or len(ids) + budget > 32768:
            raise ValueError("ContentJudge context/budget exceeded")
        input_device = model.get_input_embeddings().weight.device
        inputs = torch.tensor([ids], dtype=torch.long, device=input_device)
        generation_kwargs = {}
        response_format = body.get("response_format")
        if response_format is not None:
            if response_format.get("type") != "json_schema":
                raise ValueError("Only explicit JSON-schema structured outputs are supported")
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from integrations.skill_generator.skillos.structured import schema_processor
            generation_kwargs["logits_processor"] = [schema_processor(tokenizer,
                vocab_size=model.get_output_embeddings().weight.shape[0],
                schema=response_format["json_schema"]["schema"], prompt_length=len(ids), thinking=True)]
        with torch.inference_mode():
            result = model.generate(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                max_new_tokens=budget, do_sample=False, repetition_penalty=1,
                return_dict_in_generate=True, use_cache=True, **generation_kwargs)
        tokens = result.sequences[0, len(ids):].tolist()
        text = tokenizer.decode(tokens, skip_special_tokens=True)
        return {"id": "skillos-content-" + uuid.uuid4().hex, "object": "text_completion",
            "created": int(time.time()), "model": args.served_model, "prompt_token_ids": ids,
            "schema_enforced": response_format is not None,
            "choices": [{"index": 0, "text": text, "token_ids": tokens, "logprobs": None,
                         "finish_reason": "length" if len(tokens) >= budget else "stop"}],
            "usage": {"prompt_tokens": len(ids), "completion_tokens": len(tokens), "total_tokens": len(ids) + len(tokens)}}

    @app.post("/v1/completions")
    async def completions(body: dict):
        async with lock:
            try:
                return await asyncio.to_thread(generate, body)
            except (ValueError, KeyError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
