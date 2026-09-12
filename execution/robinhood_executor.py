"""Future live Robinhood adapter, disabled by repository defaults.

The MCP order schemas are deliberately not invented here. A future operator
must provide an EquityOrderSchemaAdapter built from the then-current connected
server metadata. Tests inject fakes; production defaults inject nothing.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import config
from execution.execution_guard import (
    ExecutionContext,
    ExecutionGuard,
    read_kill_switch,
)
from execution.models import ExecutionResult, TradePlan
from execution.order_state import (
    ExecutionAuditLog,
    ExecutionState,
    ExecutionStateStore,
)
from execution.reconciliation import (
    BrokerReconciler,
    NormalizedBrokerState,
    ReconciliationResult,
)


class RobinhoodExecutionClient(Protocol):
    """Exact published MCP tool names, implemented only by a future adapter."""

    def get_accounts(self) -> Sequence[Mapping[str, Any]]: ...
    def get_portfolio(self, account_reference: Any) -> Mapping[str, Any]: ...
    def get_equity_positions(self, account_reference: Any) -> Sequence[Mapping[str, Any]]: ...
    def get_equity_orders(self, account_reference: Any) -> Sequence[Mapping[str, Any]]: ...
    def get_equity_quotes(self, symbols: Sequence[str]) -> Mapping[str, Any]: ...
    def get_equity_tradability(self, symbol: str) -> Mapping[str, Any]: ...
    def review_equity_order(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def place_equity_order(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def cancel_equity_order(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class EquityOrderSchemaAdapter(Protocol):
    """Must be created from current MCP metadata before future enablement."""

    schema_verified: bool

    def build_review_request(
        self, plan: TradePlan, account_reference: Any
    ) -> Mapping[str, Any]: ...

    def review_accepted(self, response: Mapping[str, Any]) -> bool: ...

    def build_place_request(
        self,
        plan: TradePlan,
        account_reference: Any,
        review_response: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def normalize_submission(
        self, response: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LiveExecutionInputs:
    mode: str
    snapshot_timestamp: str | None
    market_status: str | None
    is_regular_session: bool | None
    trades_today: int
    daily_realized_pnl: float | None
    trading_date: str
    confirmation_token: str | None = None


class RobinhoodExecutor:
    """Review/submit state machine that is unreachable under current defaults."""

    def __init__(
        self,
        client: RobinhoodExecutionClient,
        *,
        schema_adapter: EquityOrderSchemaAdapter | None = None,
        guard: ExecutionGuard | None = None,
        reconciler: BrokerReconciler | None = None,
        state_store: ExecutionStateStore | None = None,
        audit_log: ExecutionAuditLog | None = None,
        kill_switch_path: str | Path | None = None,
    ) -> None:
        self.client = client
        self.schema_adapter = schema_adapter
        self.guard = guard or ExecutionGuard()
        self.reconciler = reconciler or BrokerReconciler()
        self.state_store = state_store or ExecutionStateStore(
            config.LIVE_EXECUTION_STATE_PATH
        )
        self.audit_log = audit_log or ExecutionAuditLog(
            config.EXECUTION_AUDIT_LOG_PATH
        )
        self.kill_switch_path = Path(
            kill_switch_path or config.LIVE_KILL_SWITCH_PATH
        )

    def execute(
        self,
        plan: TradePlan,
        inputs: LiveExecutionInputs,
        *,
        now: datetime,
    ) -> ExecutionResult:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        early_reasons = self._configuration_rejections(inputs)
        if early_reasons:
            return self._reject(plan, current, early_reasons, "NOT_ATTEMPTED")

        try:
            broker = self._read_broker_state(plan)
        except Exception as exc:
            return self._reject(
                plan,
                current,
                (f"BROKER_RECONCILIATION_FAILED:{type(exc).__name__}",),
                "UNKNOWN",
            )
        local_intent = self.state_store.intents.get(plan.trade_id)
        reconciliation = self.reconciler.reconcile(
            plan, broker, local_intent=local_intent
        )
        if (
            local_intent is not None
            and local_intent.state == ExecutionState.UNKNOWN
            and not reconciliation.submission_proven_absent
        ):
            return self._reject(
                plan,
                current,
                ("RECONCILIATION_REQUIRED",),
                reconciliation.status,
                reconciliation=reconciliation,
            )
        if local_intent is not None and local_intent.state == ExecutionState.UNKNOWN:
            self.state_store.transition(
                plan.trade_id,
                ExecutionState.VALIDATED,
                now=current,
                reconciliation_required=False,
            )

        portfolio_value = self._number(
            broker.portfolio.get("portfolio_value", broker.portfolio.get("total_value"))
        )
        quote = broker.quote
        daily_loss_now = (
            portfolio_value is not None
            and inputs.daily_realized_pnl is not None
            and inputs.daily_realized_pnl
            <= -portfolio_value * config.MAX_LIVE_DAILY_LOSS_PERCENT
        )
        if daily_loss_now:
            self.state_store.mark_daily_loss_blocked(inputs.trading_date, now=current)
        daily_blocked = daily_loss_now or self.state_store.is_daily_loss_blocked(
            inputs.trading_date
        )
        context = ExecutionContext(
            mode=config.MODE,
            account_equity=portfolio_value,
            agentic_account_count=reconciliation.agentic_account_count,
            account_state=reconciliation.account_state,
            market_status=inputs.market_status,
            is_regular_session=inputs.is_regular_session,
            snapshot_timestamp=inputs.snapshot_timestamp,
            quote_timestamp=(
                str(quote.get("quote_as_of")) if quote.get("quote_as_of") else None
            ),
            bid=self._number(quote.get("bid")),
            ask=self._number(quote.get("ask")),
            symbol_tradable=(
                broker.tradability.get("tradable") is True
                or broker.tradability.get("is_tradable") is True
            ),
            existing_position=reconciliation.existing_position,
            conflicting_order=reconciliation.conflicting_order,
            open_positions=sum(
                1 for item in broker.positions
                if (self._number(item.get("quantity")) or 0) > 0
            ),
            trades_today=inputs.trades_today,
            daily_realized_pnl=inputs.daily_realized_pnl,
            daily_loss_blocked=daily_blocked,
            risk_result={"approved": plan.risk_manager_approved},
            kill_switch=read_kill_switch(self.kill_switch_path),
            live_trading_enabled=config.LIVE_TRADING_ENABLED,
            robinhood_execution_enabled=config.ROBINHOOD_EXECUTION_ENABLED,
            confirmation_token=config.LIVE_CONFIRMATION_TOKEN,
            expected_confirmation_value=config.LIVE_CONFIRMATION_EXPECTED_VALUE,
            unresolved_execution_state=(
                local_intent is not None and local_intent.reconciliation_required
            ),
        )
        guard_result = self.guard.evaluate(plan, context, now=current)
        if not guard_result.allowed:
            return self._reject(
                plan,
                current,
                guard_result.reasons,
                reconciliation.status,
                guard_result=guard_result.to_dict(),
                reconciliation=reconciliation,
            )
        if self.schema_adapter is None or self.schema_adapter.schema_verified is not True:
            return self._reject(
                plan,
                current,
                ("ORDER_SCHEMA_UNAVAILABLE",),
                reconciliation.status,
                guard_result=guard_result.to_dict(),
                reconciliation=reconciliation,
            )

        intent = self.state_store.create_intent(
            plan.trade_id, plan.symbol, plan.quantity, now=current
        )
        if intent.state not in {ExecutionState.CREATED, ExecutionState.VALIDATED}:
            return self._reject(
                plan, current, ("DUPLICATE_EXECUTION_INTENT",), intent.state
            )
        self.state_store.transition(plan.trade_id, ExecutionState.VALIDATED, now=current)
        review_request = self.schema_adapter.build_review_request(
            plan, reconciliation.account_reference
        )
        self.state_store.transition(
            plan.trade_id, ExecutionState.REVIEW_PENDING, now=current
        )
        try:
            review = self.client.review_equity_order(review_request)
        except Exception as exc:
            self.state_store.transition(
                plan.trade_id,
                ExecutionState.REJECTED,
                now=current,
                last_error=f"REVIEW_FAILED:{type(exc).__name__}",
            )
            return self._reject(
                plan,
                current,
                (f"REVIEW_FAILED:{type(exc).__name__}",),
                reconciliation.status,
                guard_result=guard_result.to_dict(),
                reconciliation=reconciliation,
            )
        if not self.schema_adapter.review_accepted(review):
            self.state_store.transition(
                plan.trade_id, ExecutionState.REJECTED, now=current,
                last_error="REVIEW_REJECTED",
            )
            return self._reject(
                plan, current, ("REVIEW_REJECTED",), reconciliation.status,
                guard_result=guard_result.to_dict(), reconciliation=reconciliation,
                review=self._safe_review_summary(review, accepted=False),
            )
        self.state_store.transition(plan.trade_id, ExecutionState.REVIEWED, now=current)
        place_request = self.schema_adapter.build_place_request(
            plan, reconciliation.account_reference, review
        )
        self.state_store.transition(
            plan.trade_id, ExecutionState.SUBMISSION_PENDING, now=current
        )
        try:
            raw_submission = self.client.place_equity_order(place_request)
        except Exception as exc:
            self.state_store.transition(
                plan.trade_id,
                ExecutionState.UNKNOWN,
                now=current,
                reconciliation_required=True,
                last_error=f"SUBMISSION_OUTCOME_UNKNOWN:{type(exc).__name__}",
            )
            return self._result(
                plan,
                current,
                status="UNKNOWN",
                reconciliation_state="RECONCILIATION_REQUIRED",
                errors=("SUBMISSION_OUTCOME_UNKNOWN_DO_NOT_RETRY",),
                guard_result=guard_result.to_dict(),
                reconciliation=reconciliation,
                review=self._safe_review_summary(review, accepted=True),
            )
        submission = self.schema_adapter.normalize_submission(raw_submission)
        order_id = (
            str(submission.get("order_id")) if submission.get("order_id") else None
        )
        submitted_state = str(submission.get("state", "SUBMITTED")).upper()
        filled_quantity = self._number(submission.get("filled_quantity")) or 0.0
        if order_id is None or filled_quantity > plan.quantity:
            self.state_store.transition(
                plan.trade_id,
                ExecutionState.UNKNOWN,
                now=current,
                reconciliation_required=True,
                last_error="UNSAFE_SUBMISSION_RESPONSE",
            )
            return self._result(
                plan,
                current,
                status="UNKNOWN",
                reconciliation_state="RECONCILIATION_REQUIRED",
                errors=("UNSAFE_SUBMISSION_RESPONSE_DO_NOT_RETRY",),
                guard_result=guard_result.to_dict(),
                reconciliation=reconciliation,
                review=self._safe_review_summary(review, accepted=True),
            )
        state = {
            "PARTIALLY_FILLED": ExecutionState.PARTIALLY_FILLED,
            "FILLED": ExecutionState.FILLED,
        }.get(submitted_state, ExecutionState.SUBMITTED)
        self.state_store.transition(
            plan.trade_id,
            state,
            now=current,
            robinhood_order_id=order_id,
            confirmed_filled_quantity=filled_quantity,
            reconciliation_required=False,
        )
        return self._result(
            plan,
            current,
            status=state,
            reconciliation_state=reconciliation.status,
            robinhood_order_id=order_id,
            warnings=tuple(
                self._safe_review_summary(review, accepted=True)["warnings"]
            ),
            guard_result=guard_result.to_dict(),
            reconciliation=reconciliation,
            review=self._safe_review_summary(review, accepted=True),
            submission={
                "order_id": order_id,
                "state": submitted_state,
                "filled_quantity": filled_quantity,
            },
        )

    def _configuration_rejections(
        self, inputs: LiveExecutionInputs
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if config.MODE not in {
            "ANALYSIS_ONLY", "SHADOW_TRADING", "REVIEW_ONLY", "LIVE_AUTONOMOUS"
        }:
            failures.append("UNKNOWN_MODE")
        if inputs.mode != config.MODE:
            failures.append("MODE_CONTEXT_MISMATCH")
        if config.MODE != "LIVE_AUTONOMOUS":
            failures.append("MODE_NOT_LIVE")
        if config.LIVE_TRADING_ENABLED is not True:
            failures.append("LIVE_TRADING_DISABLED")
        if config.ROBINHOOD_EXECUTION_ENABLED is not True:
            failures.append("ROBINHOOD_EXECUTION_DISABLED")
        configured_token = config.LIVE_CONFIRMATION_TOKEN
        expected = config.LIVE_CONFIRMATION_EXPECTED_VALUE
        if not (
            isinstance(expected, str)
            and expected
            and isinstance(configured_token, str)
            and configured_token
            and hmac.compare_digest(expected, configured_token)
            and isinstance(inputs.confirmation_token, str)
            and inputs.confirmation_token
            and hmac.compare_digest(configured_token, inputs.confirmation_token)
        ):
            failures.append("LIVE_CONFIRMATION_INVALID")
        kill_switch = read_kill_switch(self.kill_switch_path)
        if kill_switch.trading_blocked:
            failures.append(kill_switch.status)
        return tuple(dict.fromkeys(failures))

    def _read_broker_state(self, plan: TradePlan) -> NormalizedBrokerState:
        accounts = self.client.get_accounts()
        agentic = [item for item in accounts if item.get("is_agentic_account") is True]
        reference = agentic[0].get("account_reference") if len(agentic) == 1 else None
        if reference is None:
            return NormalizedBrokerState(accounts, {}, [], [], {}, {})
        return NormalizedBrokerState(
            accounts=accounts,
            portfolio=self.client.get_portfolio(reference),
            positions=self.client.get_equity_positions(reference),
            orders=self.client.get_equity_orders(reference),
            quote=self.client.get_equity_quotes([plan.symbol]),
            tradability=self.client.get_equity_tradability(plan.symbol),
        )

    def _reject(
        self,
        plan: TradePlan,
        now: datetime,
        reasons: tuple[str, ...],
        reconciliation_state: str,
        *,
        guard_result: Mapping[str, Any] | None = None,
        reconciliation: ReconciliationResult | None = None,
        review: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        return self._result(
            plan,
            now,
            status="REJECTED",
            reconciliation_state=reconciliation_state,
            errors=reasons,
            guard_result=guard_result,
            reconciliation=reconciliation,
            review=review,
        )

    def _result(
        self,
        plan: TradePlan,
        now: datetime,
        *,
        status: str,
        reconciliation_state: str,
        robinhood_order_id: str | None = None,
        warnings: tuple[str, ...] = (),
        errors: tuple[str, ...] = (),
        guard_result: Mapping[str, Any] | None = None,
        reconciliation: ReconciliationResult | None = None,
        review: Mapping[str, Any] | None = None,
        submission: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult(
            trade_id=plan.trade_id,
            symbol=plan.symbol,
            requested_action=plan.side,
            order_type=plan.entry_type,
            requested_quantity=plan.quantity,
            requested_price=plan.entry_price,
            robinhood_order_id=robinhood_order_id,
            status=str(status),
            timestamp=now.astimezone(timezone.utc).isoformat(),
            reconciliation_state=reconciliation_state,
            warnings=warnings,
            errors=errors,
        )
        self.audit_log.append({
            "timestamp": result.timestamp,
            "trade_id": plan.trade_id,
            "symbol": plan.symbol,
            "mode": config.MODE,
            "action_attempted": "LIVE_EQUITY_ENTRY",
            "execution_guard_result": guard_result,
            "risk_result": {"approved": plan.risk_manager_approved},
            "broker_reconciliation_result": (
                reconciliation.to_dict() if reconciliation is not None else None
            ),
            "review_result": review,
            "submission_result": submission,
            "robinhood_order_id": robinhood_order_id,
            "state_transition": str(status),
            "warnings": list(warnings),
            "errors": list(errors),
        })
        return result

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result else None

    @staticmethod
    def _safe_review_summary(
        review: Mapping[str, Any], *, accepted: bool
    ) -> dict[str, Any]:
        warnings = review.get("warnings", [])
        return {
            "accepted": accepted,
            "warnings": [str(item) for item in warnings]
            if isinstance(warnings, (list, tuple)) else [],
        }
