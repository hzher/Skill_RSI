"""Exercise an execution protocol using explicit replay test doubles, without models."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Protocol


class Policy(Protocol):
    async def next_action(self, observation: dict) -> dict: ...


@dataclass
class ReplayPolicy:
    actions: list[dict]
    delay: float = 0
    index: int = 0

    async def next_action(self, observation):
        await asyncio.sleep(self.delay)
        if self.index>=len(self.actions):
            return {"finish":True}
        result=deepcopy(self.actions[self.index])
        self.index+=1
        return result


async def run_policy(policy:Policy,tools,*,max_steps:int,step_timeout:float) -> dict:
    if type(max_steps) is not int or not 1<=max_steps<=200 or type(step_timeout) not in (int,float) or not math.isfinite(step_timeout) or step_timeout<=0:
        raise ValueError("Execution budgets must be positive")
    observation={"start":True}
    errors=0
    for step in range(max_steps):
        try:
            action=await asyncio.wait_for(policy.next_action(observation),timeout=step_timeout)
        except TimeoutError:
            return {"policy_kind":"replay_test_double","real_model_calls":0,"steps":step,"tool_errors":errors,"termination_reason":"policy_timeout"}
        tools.trace.append({"type":"replay_action","step":step,"action":deepcopy(action),"is_model_output":False})
        if isinstance(action,dict) and set(action)=={"finish"} and action["finish"] is True:
            return {"policy_kind":"replay_test_double","real_model_calls":0,"steps":step+1,"tool_errors":errors,"termination_reason":"policy_finished"}
        try:
            if not isinstance(action,dict) or set(action)!={"tool","arguments"}:raise ValueError("Action must specify tool and arguments, or finish=true")
            observation={"tool_result":tools.call(action["tool"],action.get("arguments",{}))}
        except (ValueError,OSError,KeyError,TypeError) as exc:
            errors+=1
            observation={"tool_error":type(exc).__name__+": "+str(exc)}
    return {"policy_kind":"replay_test_double","real_model_calls":0,"steps":max_steps,"tool_errors":errors,"termination_reason":"step_budget_exhausted"}
