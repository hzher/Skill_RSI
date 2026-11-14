"""Explicit provider namespace aliases; skill bytes and strict SkillOS handlers stay intact."""
import json
import re

from integrations.benchmark_tasks.contracts import digest, strict_json

ALIASES = {"functions." + name: name for name in ("new_skill_insert", "skill_update", "skill_delete")}


def normalize_response(response):
    original_response = response
    # Qwen thinking tokens remain in the native sample. Only the final channel
    # is interpreted as operations, with this deterministic separation recorded.
    reasoning_removed = "</think>" in response
    if reasoning_removed:
        response = response.split("</think>", 1)[1].strip()
    # Accept only a whole JSON fence, without commentary or repairing its JSON.
    # Keep the sampled text/tokens intact in provenance and record this envelope.
    fence = re.fullmatch(r"\s*```json\s*\n([\s\S]*?)\n```\s*", response)
    if fence:
        response = fence.group(1)
    calls = strict_json(response)
    if not isinstance(calls, list):
        raise ValueError("Expected SkillOS operation array")
    aliases = []
    for index, call in enumerate(calls):
        if isinstance(call, dict) and isinstance(call.get("name"), str) and call["name"] in ALIASES:
            original = call["name"]
            call["name"] = ALIASES[original]
            aliases.append({"index": index, "original": original, "canonical": call["name"]})
    normalized = json.dumps(calls, ensure_ascii=False, allow_nan=False) if aliases else response
    return normalized, {"adapter": "skillos-api-envelope-v2", "aliases": aliases,
                        "json_fence_removed": fence is not None,
                        "reasoning_channel_removed": reasoning_removed,
                        "raw_sha256": digest(original_response), "normalized_sha256": digest(normalized)}
