"""Future-RL boundary. Only the deterministic baseline is implemented now."""
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class ScalpAction(IntEnum):
    WAIT = 0
    ENTER = 1
    HOLD = 2
    EXIT = 3


class ScalpPolicy(Protocol):
    def predict(self, observation) -> ScalpAction: ...


class ScalpBaselinePolicy:
    def predict_entry(self, decision):
        return ScalpAction.ENTER if decision.approved else ScalpAction.WAIT


@dataclass(frozen=True)
class ScalpPolicyComparison:
    strategy_id: str
    episode_id: str
    timestamp: str
    scalp_baseline_action: str
    scalp_rl_action: str | None = None
    # None means no scalp RL model exists; it is never silently substituted.
