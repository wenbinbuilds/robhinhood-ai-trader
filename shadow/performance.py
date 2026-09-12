"""Deterministic performance summaries for local shadow trades."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Iterable

from shadow.models import ShadowState, ShadowTrade


class ShadowPerformance:
    @staticmethod
    def _metrics(trades: Iterable[ShadowTrade]) -> dict[str, Any]:
        rows = list(trades)
        wins = [item.net_pnl for item in rows if item.net_pnl > 0]
        losses = [item.net_pnl for item in rows if item.net_pnl < 0]
        count = len(rows)
        return {
            "closed_trades": count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / count, 4) if count else None,
            "average_win": round(mean(wins), 4) if wins else None,
            "average_loss": round(mean(losses), 4) if losses else None,
            "average_risk_reward": round(mean(item.risk_reward_ratio for item in rows), 3) if rows else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None,
            "expectancy_per_trade": round(mean(item.net_pnl for item in rows), 4) if rows else None,
            "best_trade": max((item.to_dict() for item in rows), key=lambda item: item["net_pnl"], default=None),
            "worst_trade": min((item.to_dict() for item in rows), key=lambda item: item["net_pnl"], default=None),
            "average_holding_time_minutes": round(mean(item.holding_time_minutes for item in rows), 2) if rows else None,
        }

    def summarize(self, state: ShadowState) -> dict[str, Any]:
        metrics = self._metrics(state.closed_positions)
        grouped: dict[str, dict[str, Any]] = {}
        group_fields = {
            "symbol": lambda item: item.symbol,
            "sector": lambda item: item.sector,
            "strategy": lambda item: item.strategy,
            "catalyst_type": lambda item: item.catalyst_type,
            "market_regime": lambda item: item.market_regime,
            "confidence_bucket": lambda item: (
                "HIGH" if item.coordinator_confidence >= 0.8 else "MEDIUM" if item.coordinator_confidence >= 0.7 else "LOW"
            ),
            "time_of_day": lambda item: item.entry_timestamp[11:13] if len(item.entry_timestamp) >= 13 else "UNKNOWN",
        }
        for field, getter in group_fields.items():
            buckets: dict[str, list[ShadowTrade]] = defaultdict(list)
            for trade in state.closed_positions:
                buckets[str(getter(trade))].append(trade)
            grouped[field] = {
                key: self._metrics(rows) if len(rows) >= 2 else {"sample_size": len(rows), "metrics": None}
                for key, rows in buckets.items()
            }
        benchmark = {}
        for symbol, value in state.benchmark_session.items():
            reference = float(value.get("reference_price", 0) or 0)
            current = float(value.get("current_price", 0) or 0)
            benchmark[symbol] = {
                **value,
                "approximate_return_percent": round((current / reference - 1) * 100, 4) if reference > 0 else None,
            }
        return {
            "starting_capital": state.starting_capital,
            "current_equity": state.equity,
            "cash": state.cash,
            "total_net_pnl": round(state.equity - state.starting_capital, 4),
            "return_percent": round((state.equity / state.starting_capital - 1) * 100, 4) if state.starting_capital else None,
            "realized_pnl": state.realized_pnl,
            "unrealized_pnl": state.unrealized_pnl,
            "daily_pnl": state.daily_pnl,
            "open_positions": len(state.open_positions),
            "maximum_drawdown": state.maximum_drawdown,
            "maximum_drawdown_percent": round(state.maximum_drawdown_percent * 100, 4),
            **metrics,
            "benchmark": benchmark,
            "grouped_performance": grouped,
        }
