"""Deterministic, analysis-only position sizing and risk validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import TypeAlias

import config

DecimalLike: TypeAlias = Decimal | int | float | str


def _decimal(value: DecimalLike, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite number")
    return result


@dataclass(frozen=True)
class RiskLimits:
    max_position_percent: Decimal = Decimal(str(config.MAX_POSITION_PERCENT))
    max_risk_per_trade_percent: Decimal = Decimal(
        str(config.MAX_RISK_PER_TRADE_PERCENT)
    )
    max_daily_loss_percent: Decimal = Decimal(str(config.MAX_DAILY_LOSS_PERCENT))
    max_simultaneous_positions: int = config.MAX_SIMULTANEOUS_POSITIONS
    max_trades_per_day: int = config.MAX_TRADES_PER_DAY

    def __post_init__(self) -> None:
        for name in (
            "max_position_percent",
            "max_risk_per_trade_percent",
            "max_daily_loss_percent",
        ):
            value = getattr(self, name)
            if value <= 0 or value > 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1")
        if self.max_simultaneous_positions < 1:
            raise ValueError("max_simultaneous_positions must be at least 1")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be at least 1")


@dataclass(frozen=True)
class RiskRequest:
    account_equity: DecimalLike
    entry_price: DecimalLike
    stop_price: DecimalLike
    daily_realized_pnl: DecimalLike | None
    open_positions: int
    trades_today: int
    available_buying_power: DecimalLike | None = None
    requested_shares: int | None = None


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reasons: tuple[str, ...]
    max_position_dollars: Decimal
    max_risk_dollars: Decimal
    risk_per_share: Decimal | None
    max_shares: int
    theoretical_position_value: Decimal
    theoretical_dollar_risk: Decimal
    binding_limit: str | None

    def to_dict(self) -> dict[str, object]:
        def money(value: Decimal | None) -> str | None:
            return None if value is None else format(value.quantize(Decimal("0.01")), "f")

        return {
            "approved": self.approved,
            "reasons": list(self.reasons),
            "max_position_dollars": money(self.max_position_dollars),
            "max_risk_dollars": money(self.max_risk_dollars),
            "risk_per_share": money(self.risk_per_share),
            "max_shares": self.max_shares,
            "theoretical_position_value": money(self.theoretical_position_value),
            "theoretical_dollar_risk": money(self.theoretical_dollar_risk),
            "binding_limit": self.binding_limit,
            "analysis_only": True,
        }


class RiskManager:
    """Validate a long-equity idea and calculate its maximum theoretical size."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def maximum_position_dollars(self, account_equity: DecimalLike) -> Decimal:
        equity = _decimal(account_equity, "account_equity")
        if equity <= 0:
            raise ValueError("account_equity must be greater than zero")
        return equity * self.limits.max_position_percent

    def maximum_risk_dollars(self, account_equity: DecimalLike) -> Decimal:
        equity = _decimal(account_equity, "account_equity")
        if equity <= 0:
            raise ValueError("account_equity must be greater than zero")
        return equity * self.limits.max_risk_per_trade_percent

    def evaluate(self, request: RiskRequest) -> RiskResult:
        reasons: list[str] = []
        zero = Decimal("0")

        try:
            equity = _decimal(request.account_equity, "account_equity")
            entry = _decimal(request.entry_price, "entry_price")
            stop = _decimal(request.stop_price, "stop_price")
        except ValueError as exc:
            return RiskResult(False, (str(exc),), zero, zero, None, 0, zero, zero, None)

        if equity <= 0:
            reasons.append("account equity must be greater than zero")
        if entry <= 0:
            reasons.append("entry price must be greater than zero")
        if stop <= 0:
            reasons.append("stop price must be greater than zero")

        risk_per_share = entry - stop
        if risk_per_share <= 0:
            reasons.append("stop price must be below entry price for a long trade")

        if request.open_positions < 0:
            reasons.append("open position count cannot be negative")
        elif request.open_positions >= self.limits.max_simultaneous_positions:
            reasons.append("maximum simultaneous positions reached")

        if request.trades_today < 0:
            reasons.append("trades_today cannot be negative")
        elif request.trades_today >= self.limits.max_trades_per_day:
            reasons.append("maximum trades per day reached")

        daily_pnl: Decimal | None = None
        if request.daily_realized_pnl is None:
            reasons.append("today's realized P&L is unavailable")
        else:
            try:
                daily_pnl = _decimal(request.daily_realized_pnl, "daily_realized_pnl")
            except ValueError as exc:
                reasons.append(str(exc))

        max_position = (
            equity * self.limits.max_position_percent if equity > 0 else zero
        )
        max_risk = (
            equity * self.limits.max_risk_per_trade_percent if equity > 0 else zero
        )
        max_daily_loss = (
            equity * self.limits.max_daily_loss_percent if equity > 0 else zero
        )
        if daily_pnl is not None and daily_pnl <= -max_daily_loss:
            reasons.append("daily loss limit reached")

        buying_power: Decimal | None = None
        if request.available_buying_power is None:
            reasons.append("unleveraged buying power is unavailable")
        else:
            try:
                buying_power = _decimal(
                    request.available_buying_power, "available_buying_power"
                )
                if buying_power < 0:
                    reasons.append("available buying power cannot be negative")
            except ValueError as exc:
                reasons.append(str(exc))

        max_shares = 0
        binding_limit: str | None = None
        if equity > 0 and entry > 0 and stop > 0 and risk_per_share > 0:
            caps: dict[str, int] = {
                "position_percent": int(
                    (max_position / entry).to_integral_value(rounding=ROUND_FLOOR)
                ),
                "risk_per_trade": int(
                    (max_risk / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
                ),
            }
            if buying_power is not None and buying_power >= 0:
                caps["unleveraged_buying_power"] = int(
                    (buying_power / entry).to_integral_value(rounding=ROUND_FLOOR)
                )
            binding_limit, max_shares = min(caps.items(), key=lambda item: item[1])
            if max_shares < 1:
                reasons.append("configured limits permit fewer than one share")

        theoretical_value = entry * max_shares if entry > 0 else zero
        theoretical_risk = (
            risk_per_share * max_shares if risk_per_share > 0 else zero
        )

        if request.requested_shares is not None:
            if request.requested_shares <= 0:
                reasons.append("requested shares must be greater than zero")
            elif entry > 0 and risk_per_share > 0:
                requested_value = entry * request.requested_shares
                requested_risk = risk_per_share * request.requested_shares
                if requested_value > max_position:
                    reasons.append("requested position exceeds maximum position size")
                if requested_risk > max_risk:
                    reasons.append("requested risk exceeds maximum risk per trade")
                if buying_power is not None and requested_value > buying_power:
                    reasons.append("requested position exceeds unleveraged buying power")

        return RiskResult(
            approved=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            max_position_dollars=max_position,
            max_risk_dollars=max_risk,
            risk_per_share=risk_per_share if risk_per_share > 0 else None,
            max_shares=max_shares,
            theoretical_position_value=theoretical_value,
            theoretical_dollar_risk=theoretical_risk,
            binding_limit=binding_limit,
        )
