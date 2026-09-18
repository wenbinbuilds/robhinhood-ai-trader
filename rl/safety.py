"""Final fail-closed authorization for RL proposals."""
from dataclasses import dataclass
import config
from execution.execution_guard import read_kill_switch
from rl.actions import EntryAction


@dataclass(frozen=True)
class SafetyDecision:
    approved: bool
    final_action: str
    reasons: tuple[str, ...]


class SafetyOverride:
    @staticmethod
    def evaluate(proposal, *, target='SHADOW', geometry=None, risk=None,
                 portfolio=None, market_snapshot=None):
        reasons = []
        if target != 'SHADOW':
            reasons.append('RL_LIVE_EXECUTION_PROHIBITED')
        if config.MODE != 'SHADOW_TRADING': reasons.append('MODE_NOT_SHADOW_TRADING')
        if config.LIVE_TRADING_ENABLED or config.ROBINHOOD_EXECUTION_ENABLED:
            reasons.append('LIVE_EXECUTION_CONFIGURATION_INVALID')
        # The blocked kill switch is required. It prohibits real routing; shadow
        # simulation remains local and cannot mutate broker state.
        if not read_kill_switch(config.LIVE_KILL_SWITCH_PATH).trading_blocked:
            reasons.append('LIVE_KILL_SWITCH_NOT_BLOCKED')
        if EntryAction(proposal) == EntryAction.ENTER:
            if geometry is not None and not geometry.valid: reasons.append('INVALID_GEOMETRY')
            if risk is not None and not risk.approved: reasons.append('RISK_REJECTED')
            if portfolio is not None and not portfolio.approved: reasons.append('PORTFOLIO_REJECTED')
            if market_snapshot is not None and market_snapshot.quote_status.value != 'FRESH':
                reasons.append('MARKET_DATA_NOT_FRESH')
        return SafetyDecision(not reasons, EntryAction(proposal).name if not reasons else 'BLOCKED', tuple(reasons))
