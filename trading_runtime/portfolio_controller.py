"""Final canonical capital/duplicate check. No alpha or geometry optimization."""
from trading_runtime.contracts import PortfolioDecision
from risk.risk_manager import RiskRequest


class PortfolioController:
    def __init__(self, portfolio, risk_manager):
        self.portfolio, self.risk_manager = portfolio, risk_manager

    def evaluate(self, plan, fill):
        # Caller retains this same lock through fill commit (no check/use race).
        with self.portfolio.lock:
            state = self.portfolio.state
            result = self.risk_manager.evaluate(RiskRequest(
                account_equity=state.equity, entry_price=fill, stop_price=plan.stop_price,
                daily_realized_pnl=state.daily_pnl, open_positions=len(state.open_positions),
                trades_today=state.trades_today, available_buying_power=state.cash,
                requested_shares=plan.quantity))
            reasons = list(result.reasons)
            duplicate = self.portfolio.has_symbol(plan.symbol)
            if duplicate:
                reasons.append('DUPLICATE_POSITION')
            if any(p.episode_id == plan.episode_id for p in state.open_positions + state.closed_positions):
                reasons.append('EPISODE_ALREADY_EXECUTED')
            return PortfolioDecision(plan.episode_id, plan.symbol, not reasons,
                plan.quantity if not reasons else 0, tuple(reasons),
                'FAIL' if duplicate else 'PASS', 'EXISTING_LIMITS_ONLY'), result
