"""Algorithm boundary; production optimization is delegated to pinned verl.

The scalar GRPO calculation is an audit projection of the upstream outcome
advantage, not an alternate optimizer. Other estimators can register adapters.
"""
from dataclasses import dataclass
from statistics import mean, stdev

from training.skill_contract import TrainingRejected


@dataclass(frozen=True)
class GRPO:
    name: str = "grpo"
    group_size: int = 4
    epsilon: float = 1e-6
    normalize_std: bool = True
    max_policy_lag: int = 0

    def __post_init__(self):
        import math
        if type(self.group_size) is not int or self.group_size < 2:
            raise TrainingRejected("GRPO needs at least two samples per prompt")
        if type(self.epsilon) not in (int, float) or not math.isfinite(self.epsilon) or self.epsilon <= 0:
            raise TrainingRejected("Invalid GRPO epsilon")
        if self.epsilon != 1e-6:
            raise TrainingRejected("Pinned verl GRPO uses epsilon=1e-6; audit and optimizer must agree")
        if type(self.normalize_std) is not bool or self.max_policy_lag != 0:
            raise TrainingRejected("Default GRPO adapter only accepts on-policy batches")

    def advantages(self, rewards):
        if len(rewards) != self.group_size:
            raise TrainingRejected("Incomplete GRPO group")
        center = mean(rewards)
        scale = stdev(rewards) + self.epsilon if self.normalize_std else 1.0
        return [(reward - center) / scale for reward in rewards]

    def verl_overrides(self):
        return {"algorithm.adv_estimator": "grpo", "algorithm.norm_adv_by_std_in_grpo": self.normalize_std,
                "actor_rollout_ref.rollout.n": self.group_size}


class AlgorithmRegistry:
    def __init__(self):
        self._factories = {"grpo": GRPO}

    def register(self, name, factory):
        if not isinstance(name, str) or not name or name in self._factories or not callable(factory):
            raise ValueError("Duplicate or invalid algorithm adapter")
        self._factories[name] = factory

    def resolve(self, name="grpo", **options):
        if name not in self._factories:
            raise TrainingRejected("Unregistered algorithm adapter: " + str(name))
        adapter = self._factories[name](**options)
        if any(not hasattr(adapter, key) for key in ("name", "group_size", "max_policy_lag", "advantages", "verl_overrides")):
            raise TrainingRejected("Incomplete algorithm adapter")
        if adapter.name != name or not callable(adapter.advantages) or not callable(adapter.verl_overrides):
            raise TrainingRejected("Algorithm identity or methods differ from registration")
        return adapter
