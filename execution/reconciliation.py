"""Pure broker/local reconciliation for future live equities execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from execution.models import TradePlan
from execution.order_state import ExecutionIntent, ExecutionState


OPEN_ORDER_STATES = {
    "queued", "pending", "confirmed", "unconfirmed", "partially_filled",
    "open", "submitted", "new",
}


@dataclass(frozen=True)
class NormalizedBrokerState:
    """Schema-adapter output; account_reference is memory-only and never logged."""

    accounts: Sequence[Mapping[str, Any]]
    portfolio: Mapping[str, Any]
    positions: Sequence[Mapping[str, Any]]
    orders: Sequence[Mapping[str, Any]]
    quote: Mapping[str, Any]
    tradability: Mapping[str, Any]


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    agentic_account_count: int
    account_state: str | None
    existing_position: bool
    conflicting_order: bool
    partial_fill_quantity: float
    recently_rejected_order: bool
    recently_canceled_order: bool
    manual_or_external_state_change: bool
    submission_proven_absent: bool
    warnings: tuple[str, ...] = ()
    account_reference: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("account_reference", None)
        return value


class BrokerReconciler:
    def reconcile(
        self,
        plan: TradePlan,
        broker: NormalizedBrokerState,
        *,
        local_intent: ExecutionIntent | None = None,
    ) -> ReconciliationResult:
        agentic = [
            item for item in broker.accounts
            if isinstance(item, Mapping) and item.get("is_agentic_account") is True
        ]
        account = agentic[0] if len(agentic) == 1 else {}
        account_reference = account.get("account_reference") if account else None
        symbol = plan.symbol.upper()
        positions = [
            item for item in broker.positions
            if str(item.get("symbol", "")).upper() == symbol
            and float(item.get("quantity", 0) or 0) > 0
        ]
        symbol_orders = [
            item for item in broker.orders
            if str(item.get("symbol", "")).upper() == symbol
        ]
        open_orders = [
            item for item in symbol_orders
            if str(item.get("state", item.get("status", ""))).lower() in OPEN_ORDER_STATES
        ]
        partial = sum(
            float(item.get("filled_quantity", 0) or 0)
            for item in symbol_orders
            if str(item.get("state", item.get("status", ""))).lower()
            == "partially_filled"
        )
        rejected = any(
            str(item.get("state", item.get("status", ""))).lower() == "rejected"
            for item in symbol_orders
        )
        canceled = any(
            str(item.get("state", item.get("status", ""))).lower()
            in {"canceled", "cancelled"}
            for item in symbol_orders
        )
        matching_submission = any(
            str(item.get("client_trade_id", "")) == plan.trade_id
            or (
                local_intent is not None
                and local_intent.robinhood_order_id is not None
                and str(item.get("order_id", "")) == local_intent.robinhood_order_id
            )
            for item in symbol_orders
        )
        manual_change = False
        if local_intent is not None:
            state = ExecutionState(local_intent.state)
            expected_position = state in {
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
                ExecutionState.EXIT_PENDING,
            }
            manual_change = expected_position != bool(positions)
        warnings: list[str] = []
        if rejected:
            warnings.append("RECENTLY_REJECTED_ORDER")
        if canceled:
            warnings.append("RECENTLY_CANCELED_ORDER")
        if manual_change:
            warnings.append("MANUAL_OR_EXTERNAL_STATE_CHANGE")
        if len(agentic) != 1:
            status = "AGENTIC_ACCOUNT_AMBIGUOUS"
        elif partial > 0:
            status = "PARTIALLY_FILLED"
        elif open_orders:
            status = "CONFLICTING_ORDER"
        elif manual_change:
            status = "MANUAL_OR_EXTERNAL_STATE_CHANGE"
        else:
            status = "RECONCILED"
        return ReconciliationResult(
            status=status,
            agentic_account_count=len(agentic),
            account_state=(str(account.get("state")) if account.get("state") else None),
            existing_position=bool(positions),
            conflicting_order=bool(open_orders),
            partial_fill_quantity=partial,
            recently_rejected_order=rejected,
            recently_canceled_order=canceled,
            manual_or_external_state_change=manual_change,
            submission_proven_absent=not matching_submission and not positions and not open_orders,
            warnings=tuple(warnings),
            account_reference=account_reference,
        )
