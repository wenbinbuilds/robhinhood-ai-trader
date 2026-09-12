"""Mode-aware routing between the common plan and execution adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from execution.models import ExecutionResult, TradePlan


class PlanExecutor(Protocol):
    def execute(
        self,
        plan: TradePlan,
        market_data: Mapping[str, Any],
        *,
        now: datetime,
    ) -> ExecutionResult: ...


class LivePlanExecutor(Protocol):
    def execute(self, plan: TradePlan, context: Any, *, now: datetime) -> ExecutionResult: ...


@dataclass
class ExecutionRouter:
    """Route by explicit mode without constructing the unused live adapter."""

    shadow_executor: PlanExecutor | None = None
    robinhood_executor: LivePlanExecutor | None = None

    def route(
        self,
        mode: str,
        plan: TradePlan,
        *,
        now: datetime,
        market_data: Mapping[str, Any] | None = None,
        live_context: Any = None,
    ) -> ExecutionResult:
        timestamp = now.astimezone(timezone.utc).isoformat()
        if mode == "SHADOW_TRADING":
            if self.shadow_executor is None:
                return self._blocked(plan, timestamp, "SHADOW_EXECUTOR_UNAVAILABLE")
            return self.shadow_executor.execute(plan, market_data or {}, now=now)
        if mode == "ANALYSIS_ONLY":
            return self._blocked(plan, timestamp, "ANALYSIS_ONLY")
        if mode == "REVIEW_ONLY":
            return self._blocked(plan, timestamp, "REVIEW_ONLY_NOT_ENABLED")
        if mode == "LIVE_AUTONOMOUS":
            if self.robinhood_executor is None:
                return self._blocked(plan, timestamp, "ROBINHOOD_EXECUTOR_UNAVAILABLE")
            return self.robinhood_executor.execute(plan, live_context, now=now)
        return self._blocked(plan, timestamp, "UNKNOWN_MODE")

    @staticmethod
    def _blocked(plan: TradePlan, timestamp: str, reason: str) -> ExecutionResult:
        return ExecutionResult(
            trade_id=plan.trade_id,
            symbol=plan.symbol,
            requested_action=plan.side,
            order_type=plan.entry_type,
            requested_quantity=plan.quantity,
            requested_price=plan.entry_price,
            status="REJECTED",
            timestamp=timestamp,
            reconciliation_state="NOT_ATTEMPTED",
            errors=(reason,),
        )
