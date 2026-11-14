"""Use the real recipe dataset after training-seed provenance validation."""
import json
from pathlib import Path
import os

from recipe.nemo_gym.dataset import NeMoGymJSONLDataset

from evaluation.firewall import EvaluationFirewall
from training.skill_contract import TrainingRejected
from training.seed_contract import validate_seed_row
from training.profile_admission import validate_task_profile


class SkillGeneratorDataset(NeMoGymJSONLDataset):
    def __init__(self, data_files, tokenizer, processor=None, config=None, **kwargs):
        firewall = EvaluationFirewall.load(Path(os.environ["WORKENV_VERIFIED_MANIFEST"]),
                                          Path(os.environ["WORKENV_TRAINING_REGISTRY"]))
        paths = [data_files] if isinstance(data_files, str) else data_files
        count = 0
        for filename in paths:
            with Path(filename).open(encoding="utf-8") as source:
                for number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    validate_seed_row(row, firewall)
                    validate_task_profile(row["provenance"], firewall,
                                          resource_backend=os.environ.get("WORKENV_GENERATOR_RESOURCE_BACKEND", "shared"),
                                          root=Path(__file__).resolve().parents[1])
                    validate_task_profile(row["downstream_provenance"], firewall,
                                          resource_backend=os.environ.get("WORKENV_GENERATOR_RESOURCE_BACKEND", "shared"),
                                          root=Path(__file__).resolve().parents[1])
                    count += 1
        if count == 0:
            raise TrainingRejected("No independently admitted Generator training seeds")
        super().__init__(data_files, tokenizer, processor=processor, config=config, **kwargs)
