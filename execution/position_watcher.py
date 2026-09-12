"""Fast deterministic stop/target interfaces; no LLM or broker dependency."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from execution.models import parse_timestamp
from execution.order_state import ExecutionState


@dataclass(frozen=True)
class ExitRequest:
    trade_id: str
    symbol: str
    reason: str
    requested_quantity: float
    reference_price: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FastPositionWatcher:
    """Convert fresh prices into exit requests without an LLM decision."""

    @staticmethod
    def evaluate(
        *,
        trade_id: str,
        symbol: str,
        latest_price: float,
        stop_price: float,
        target_price: float,
        confirmed_position_quantity: float,
    ) -> ExitRequest | None:
        if confirmed_position_quantity <= 0:
            return None
        if latest_price <= stop_price:
            reason = "STOP_HIT"
        elif latest_price >= target_price:
            reason = "TARGET_HIT"
        else:
            return None
        return ExitRequest(
            trade_id=trade_id,
            symbol=symbol.upper(),
            reason=reason,
            requested_quantity=confirmed_position_quantity,
            reference_price=latest_price,
        )


def cap_exit_quantity(requested: float, confirmed_position_quantity: float) -> float:
    """An exit may never exceed the broker-confirmed filled position."""

    if requested <= 0 or confirmed_position_quantity <= 0:
        return 0.0
    return min(float(requested), float(confirmed_position_quantity))


@dataclass(frozen=True)
class ProtectiveExitDecision:
    state: str
    action: str
    exit_request: ExitRequest | None = None
    warnings: tuple[str, ...] = ()


class ProtectiveExitStateMachine:
    """Plan exits only after a broker fill is confirmed; never submits them."""

    @staticmethod
    def evaluate(
        *,
        trade_id: str,
        symbol: str,
        entry_state: str,
        confirmed_filled_quantity: float,
        broker_position_quantity: float,
        latest_price: float | None,
        quote_timestamp: str | None,
        stop_price: float,
        target_price: float,
        now: datetime,
        max_quote_age_seconds: int,
    ) -> ProtectiveExitDecision:
        state = ExecutionState(entry_state)
        if state in {ExecutionState.REJECTED, ExecutionState.CANCELED}:
            return ProtectiveExitDecision(state, "NO_POSITION")
        if state in {
            ExecutionState.CREATED,
            ExecutionState.VALIDATED,
            ExecutionState.REVIEW_PENDING,
            ExecutionState.REVIEWED,
            ExecutionState.SUBMISSION_PENDING,
            ExecutionState.SUBMITTED,
            ExecutionState.UNKNOWN,
        }:
            return ProtectiveExitDecision(state, "RECONCILE_BEFORE_EXIT")
        if broker_position_quantity <= 0:
            return ProtectiveExitDecision(
                state,
                "RECONCILE_BEFORE_EXIT",
                warnings=("MANUAL_OR_EXTERNAL_STATE_CHANGE",),
            )
        timestamp = parse_timestamp(quote_timestamp)
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        age = (
            (current.astimezone(timezone.utc) - timestamp).total_seconds()
            if timestamp is not None else None
        )
        if latest_price is None or age is None or age < -30 or age > max_quote_age_seconds:
            return ProtectiveExitDecision(state, "STALE_PRICE_BLOCKED")
        quantity = cap_exit_quantity(
            confirmed_filled_quantity, broker_position_quantity
        )
        signal = FastPositionWatcher.evaluate(
            trade_id=trade_id,
            symbol=symbol,
            latest_price=latest_price,
            stop_price=stop_price,
            target_price=target_price,
            confirmed_position_quantity=quantity,
        )
        return ProtectiveExitDecision(
            ExecutionState.EXIT_PENDING if signal is not None else state,
            "REQUEST_PROTECTIVE_EXIT" if signal is not None else "MONITOR",
            signal,
        )
