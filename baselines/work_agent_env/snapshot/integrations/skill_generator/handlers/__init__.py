"""Strict SkillOS tool transport on isolated in-memory snapshots."""
from ..vendor.reward_toolchain import SkillRepo, parse_calls


def apply_operations(skills, response):
    repo = SkillRepo(skills)
    calls = parse_calls(response)
    results = [repo.apply(call) for call in calls]
    if any(not item["executed"] or item["error"] for item in results):
        raise ValueError("Skill operation batch rejected: " + str([item["error"] for item in results]))
    return repo.skills, results
