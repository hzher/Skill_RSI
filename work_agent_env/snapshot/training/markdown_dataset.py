"""Real torch Dataset for the native verl Markdown agent loop; no NeMo recipe dependency."""
import os
from pathlib import Path

import torch
from torch.utils.data import Dataset

from evaluation.firewall import EvaluationFirewall
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from training.markdown_contract import native_prompt
from training.markdown_data import prepare_group, read_pairs
from training.skill_contract import TrainingRejected


class MarkdownSkillDataset(Dataset):
    def __init__(self, data_files, tokenizer, processor=None, config=None, **kwargs):
        if processor is not None:
            raise TrainingRejected("Markdown training currently requires a text-only Generator")
        root = Path(os.environ["WORKENV_PROJECT_ROOT"])
        firewall = EvaluationFirewall.load(Path(os.environ["WORKENV_VERIFIED_MANIFEST"]),
                                          Path(os.environ["WORKENV_TRAINING_REGISTRY"]))
        editor = SkillEditor(ExperienceStore(root / ".runtime/experience"), SkillLibrary(root / ".runtime/skill_library"))
        self.rows = []
        paths = [data_files] if isinstance(data_files, str) else data_files
        for path in paths:
            for pair in read_pairs(path):
                group = prepare_group(pair, editor=editor, policy_id="dataset-render-v1", training=True, firewall=firewall)
                messages = native_prompt(group["request"])
                # Prompt tokenization is legitimate; response token IDs/logprobs are never reconstructed.
                ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                    **dict(config.get("apply_chat_template_kwargs", {})))
                if len(ids) > config.max_prompt_length:
                    raise TrainingRejected("Reviewed Generator prompt exceeds the token budget")
                self.rows.append({"raw_prompt": messages, "skill_pair": pair, "index": len(self.rows),
                                  "agent_name": "markdown_skill", "data_source": "skill_generator",
                                  "__skill_batch_size__": torch.zeros(1, dtype=torch.long)})
        if not self.rows:
            raise TrainingRejected("No reviewed training pairs")

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.rows[index]
