"""Risk-normalized reward with explicit execution friction."""
from dataclasses import dataclass, asdict
from typing import Mapping, Any
import config


@dataclass(frozen=True)
class RewardConfig:
    version: str = '1.0'
    entry_slippage_bps: float = config.SHADOW_ENTRY_SLIPPAGE_BPS
    exit_slippage_bps: float = config.SHADOW_EXIT_SLIPPAGE_BPS
    commission_per_share: float = config.RL_COMMISSION_PER_SHARE
    drawdown_penalty: float = 0.20
    mae_penalty: float = 0.10
    transaction_cost_penalty: float = 1.0
    overtrading_penalty: float = 0.02
    holding_penalty_per_hour: float = 0.002


class RiskNormalizedReward:
    def __init__(self, config: RewardConfig | None = None):
        self.config = config or RewardConfig()

    def entry_fill(self, row: Mapping[str, Any]):
        ask = _n(row.get('ask')) or _n(row.get('price'))
        return ask * (1 + self.config.entry_slippage_bps/10_000) if ask else None

    def exit_fill(self, row: Mapping[str, Any]):
        bid = _n(row.get('outcome_bid')) or _n(row.get('exit_price')) or _n(row.get('final_price'))
        return bid * (1 - self.config.exit_slippage_bps/10_000) if bid else None

    def calculate(self, row: Mapping[str, Any], *, entries_today=0):
        entry, exit_price, stop = self.entry_fill(row), self.exit_fill(row), _n(row.get('stop'))
        if entry is None or exit_price is None or stop is None or entry <= stop:
            return 0.0, {'outcome_status': 'UNAVAILABLE', 'r_multiple': None}
        initial_risk = entry-stop
        gross_r = (exit_price-entry)/initial_risk
        spread = max(0.0, (_n(row.get('ask')) or entry)-(_n(row.get('bid')) or entry))
        friction_r = (spread + entry*self.config.entry_slippage_bps/10_000
                      + exit_price*self.config.exit_slippage_bps/10_000
                      + 2*self.config.commission_per_share)/initial_risk
        mae_r = max(0.0, -(_n(row.get('mae_r')) or 0.0))
        drawdown_r = max(0.0, _n(row.get('drawdown_r')) or 0.0)
        hold_hours = max(0.0, (_n(row.get('holding_seconds')) or 0.0)/3600)
        reward = (gross_r - self.config.transaction_cost_penalty*friction_r
                  - self.config.mae_penalty*mae_r
                  - self.config.drawdown_penalty*drawdown_r
                  - self.config.holding_penalty_per_hour*hold_hours
                  - self.config.overtrading_penalty*max(0, entries_today-1))
        return float(reward), {'outcome_status': row.get('outcome_status', 'SIMULATED'),
                               'r_multiple': gross_r, 'friction_r': friction_r,
                               'reward_config': asdict(self.config)}


def _n(value):
    try:
        value = float(value)
        return value if value == value else None
    except (TypeError, ValueError):
        return None
