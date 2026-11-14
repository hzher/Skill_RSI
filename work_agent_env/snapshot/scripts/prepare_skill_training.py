"""Validate pair manifests against actual experience and the existing lineage firewall."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.firewall import EvaluationFirewall
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from training.markdown_data import prepare_group, read_pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    editor = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"), SkillLibrary(ROOT / ".runtime/skill_library"))
    guard = EvaluationFirewall.load(ROOT / "evaluation/toolathlon_verified.manifest.json", ROOT / "training/seed_registry.json")
    pairs = read_pairs(args.pairs)
    for pair in pairs:
        prepare_group(pair, editor=editor, policy_id="dataset-render-v1", training=True, firewall=guard)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        for pair in pairs:
            stream.write(json.dumps(pair, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"reviewed_pairs": len(pairs), "output": str(args.output), "new_admissions": 0,
                      "model_requests": 0, "parameter_updates": 0}))


if __name__ == "__main__":
    main()
