"""Adapter from the common TradePlan to the existing local shadow engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from execution.models import ExecutionResult, TradePlan
from execution.order_state import ExecutionAuditLog
from shadow.execution import ShadowExecutionEngine
import config
from watcher.models import ExitRequest, timestamp


class ShadowExecutor:
    def __init__(
        self,
        engine: ShadowExecutionEngine,
        audit_log: ExecutionAuditLog | None = None,
    ) -> None:
        self.engine = engine
        self.audit_log = audit_log

    def execute_exit(self, request: ExitRequest, *, now: datetime):
        """Only a LOCAL shadow route exists. Duplicate/stale requests are no-ops."""
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("fast exits require SHADOW_TRADING")
        quote = request.quote
        age = quote.age_at(now)
        if (not 0 <= age <= config.FAST_QUOTE_MAX_AGE_SECONDS
                or quote.exit_price is None or quote.is_market_open is not True):
            return None
        if request.reason not in {"STOP_HIT", "TARGET_HIT", "END_OF_DAY_EXIT", "HARD_RISK_EXIT"}:
            raise ValueError("unsupported deterministic exit")
        with self.engine.portfolio.lock:
            position = next((p for p in self.engine.portfolio.state.open_positions if p.trade_id == request.trade_id), None)
            if position is None or position.symbol != request.symbol or quote.symbol != request.symbol:
                return None
            latest = timestamp(position.last_price_timestamp)
            entry = timestamp(position.entry_timestamp)
            if entry is None or quote.timestamp < entry or (latest and quote.timestamp < latest):
                return None
            warnings = [] if quote.executable_bid else ["LAST_TRADE_EXIT_FALLBACK: reliable bid unavailable"]
            trade = self.engine.close_at_price(
                request.trade_id, quote.exit_price, request.reason, now=now,
                exit_method="REALTIME_FAST_EXIT" if request.monitoring_mode == "REALTIME_FAST" else "DEGRADED_SNAPSHOT_EXIT",
                warnings=warnings,
            )
            self.engine.portfolio.save(now)
            return trade

    def execute(
        self,
        plan: TradePlan,
        market_data: Mapping[str, Any],
        *,
        now: datetime,
    ) -> ExecutionResult:
        position, detail = self.engine.open_trade_plan(plan, market_data, now=now)
        status = "FILLED" if position is not None else "REJECTED"
        reason = str(detail.get("reason", "")) if position is None else ""
        result = ExecutionResult(
            trade_id=plan.trade_id,
            symbol=plan.symbol,
            requested_action=plan.side,
            order_type=plan.entry_type,
            requested_quantity=plan.quantity,
            requested_price=plan.entry_price,
            status=status,
            timestamp=now.astimezone(timezone.utc).isoformat(),
            reconciliation_state="LOCAL_SHADOW",
            warnings=tuple(str(item) for item in detail.get("warnings", [])),
            errors=((reason,) if reason else ()),
            metadata={"local_only": True, **detail},
        )
        if self.audit_log is not None:
            self.audit_log.append({
                "timestamp": result.timestamp,
                "trade_id": plan.trade_id,
                "symbol": plan.symbol,
                "mode": "SHADOW_TRADING",
                "action_attempted": "SHADOW_EQUITY_ENTRY",
                "execution_guard_result": {
                    "allowed": position is not None,
                    "route": "LOCAL_ONLY",
                },
                "risk_result": detail.get("risk", {}),
                "broker_reconciliation_result": None,
                "review_result": None,
                "submission_result": None,
                "robinhood_order_id": None,
                "state_transition": result.status,
                "warnings": list(result.warnings),
                "errors": list(result.errors),
            })
        return result
