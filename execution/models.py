"""Immutable common trade plans and credential-free execution results."""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class TradePlanError(ValueError):
    """A coordinator/risk result cannot form a valid common trade plan."""


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TradePlanError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise TradePlanError(f"{name} must be a finite number")
    return number


@dataclass(frozen=True)
class TradePlan:
    """Risk-sized V1 plan consumed unchanged by either execution adapter."""

    trade_id: str
    symbol: str
    side: str
    strategy: str
    decision_timestamp: str
    decision_price: float
    entry_type: str
    entry_price: float
    quantity: int
    notional: float
    stop_price: float
    target_price: float
    risk_per_share: float
    maximum_expected_loss: float
    risk_reward_ratio: float
    coordinator_score: float
    technical_score: float
    news_score: float
    sector_score: float
    market_score: float
    thesis: str
    invalidation_condition: str
    market_data_timestamp: str
    asset_type: str = "EQUITY"
    risk_manager_approved: bool = True
    coordinator_confidence: float = 0.0
    technical_context: Mapping[str, Any] = field(default_factory=dict)
    news_context: Mapping[str, Any] = field(default_factory=dict)
    sector_context: Mapping[str, Any] = field(default_factory=dict)
    market_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = self.symbol.upper().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise TradePlanError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        if not self.trade_id:
            raise TradePlanError("trade_id is required")
        if self.side != "BUY" or self.asset_type != "EQUITY":
            raise TradePlanError("V1 supports long equities only")
        if self.entry_type != "MARKET":
            raise TradePlanError("V1 supports only the internal MARKET entry intent")
        if not self.risk_manager_approved:
            raise TradePlanError("deterministic risk approval is required")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise TradePlanError("quantity must be a positive risk-sized integer")
        for name in (
            "decision_price", "entry_price", "notional", "stop_price",
            "target_price", "risk_per_share", "maximum_expected_loss",
            "risk_reward_ratio", "coordinator_score", "technical_score",
            "news_score", "sector_score", "market_score",
        ):
            _finite(getattr(self, name), name)
        if not (0 < self.stop_price < self.entry_price < self.target_price):
            raise TradePlanError("long plan requires stop < entry < target")
        if self.risk_per_share <= 0 or self.maximum_expected_loss <= 0:
            raise TradePlanError("risk values must be positive")
        if parse_timestamp(self.decision_timestamp) is None:
            raise TradePlanError("decision_timestamp is invalid")
        if parse_timestamp(self.market_data_timestamp) is None:
            raise TradePlanError("market_data_timestamp is invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_trade_plan(
    coordinator: Mapping[str, Any],
    risk_result: Mapping[str, Any],
    market_data: Mapping[str, Any],
    *,
    now: datetime,
    trade_id: str | None = None,
) -> TradePlan:
    """Create an immutable plan; quantity comes only from RiskManager output."""

    if coordinator.get("decision") != "TRADE_CANDIDATE":
        raise TradePlanError("coordinator did not produce TRADE_CANDIDATE")
    if risk_result.get("approved") is not True:
        raise TradePlanError("deterministic risk manager rejected the plan")
    quantity = risk_result.get("max_shares")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise TradePlanError("risk manager did not produce a positive quantity")
    entry = _finite(coordinator.get("entry"), "entry_price")
    stop = _finite(coordinator.get("stop"), "stop_price")
    target = _finite(coordinator.get("target"), "target_price")
    decision_price = _finite(market_data.get("current_price"), "decision_price")
    risk_per_share = entry - stop
    risk_reward = (target - entry) / risk_per_share if risk_per_share > 0 else 0.0
    quote_at = market_data.get("quote_as_of")
    if parse_timestamp(quote_at) is None:
        raise TradePlanError("market_data_timestamp is invalid")
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    def score(name: str, context_name: str, default: float = 0.5) -> float:
        direct = coordinator.get(name)
        if direct is not None:
            return _finite(direct, name)
        context = coordinator.get(context_name)
        if isinstance(context, Mapping) and context.get("score") is not None:
            return _finite(context.get("score"), name)
        if (
            name == "technical_score"
            and isinstance(context, Mapping)
            and context.get("technical_score") is not None
        ):
            return _finite(context.get("technical_score"), name)
        return default

    return TradePlan(
        trade_id=trade_id or str(uuid.uuid4()),
        symbol=str(coordinator.get("symbol", "")),
        side="BUY",
        strategy=str(coordinator.get("setup_name") or "INTRADAY_MOMENTUM_V1"),
        decision_timestamp=current.astimezone(timezone.utc).isoformat(),
        decision_price=decision_price,
        entry_type="MARKET",
        entry_price=entry,
        quantity=quantity,
        notional=round(entry * quantity, 4),
        stop_price=stop,
        target_price=target,
        risk_per_share=round(risk_per_share, 4),
        maximum_expected_loss=round(risk_per_share * quantity, 4),
        risk_reward_ratio=round(risk_reward, 4),
        coordinator_score=_finite(coordinator.get("combined_score"), "coordinator_score"),
        technical_score=score("technical_score", "technical_context"),
        news_score=score("news_score", "news_context"),
        sector_score=score("sector_score", "sector_context"),
        market_score=score("market_score", "market_context"),
        thesis=str(coordinator.get("thesis") or ""),
        invalidation_condition=str(coordinator.get("invalidation_condition") or ""),
        market_data_timestamp=str(quote_at),
        coordinator_confidence=_finite(
            coordinator.get("confidence", 0.0), "coordinator_confidence"
        ),
        technical_context=(
            dict(coordinator.get("technical_context", {}))
            if isinstance(coordinator.get("technical_context"), Mapping) else {}
        ),
        news_context=(
            dict(coordinator.get("news_context", {}))
            if isinstance(coordinator.get("news_context"), Mapping) else {}
        ),
        sector_context=(
            dict(coordinator.get("sector_context", {}))
            if isinstance(coordinator.get("sector_context"), Mapping) else {}
        ),
        market_context=(
            dict(coordinator.get("market_context", {}))
            if isinstance(coordinator.get("market_context"), Mapping) else {}
        ),
    )


@dataclass(frozen=True)
class ExecutionResult:
    trade_id: str
    symbol: str
    requested_action: str
    order_type: str
    requested_quantity: int
    requested_price: float | None
    status: str
    timestamp: str
    reconciliation_state: str
    robinhood_order_id: str | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
