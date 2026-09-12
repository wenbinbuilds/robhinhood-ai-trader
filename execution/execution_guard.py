"""Deterministic, fail-closed gates for future Robinhood execution."""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config
from execution.models import TradePlan, parse_timestamp


@dataclass(frozen=True)
class KillSwitchStatus:
    trading_blocked: bool
    status: str


def read_kill_switch(path: str | Path) -> KillSwitchStatus:
    """Read only; missing, unreadable, or malformed always means blocked."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return KillSwitchStatus(True, "KILL_SWITCH_MISSING")
    except (OSError, json.JSONDecodeError):
        return KillSwitchStatus(True, "KILL_SWITCH_MALFORMED")
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("trading_blocked"), bool
    ):
        return KillSwitchStatus(True, "KILL_SWITCH_MALFORMED")
    return KillSwitchStatus(
        bool(payload["trading_blocked"]),
        "BLOCKED" if payload["trading_blocked"] else "UNBLOCKED",
    )


@dataclass(frozen=True)
class ExecutionContext:
    mode: str
    account_equity: float | None
    agentic_account_count: int
    account_state: str | None
    market_status: str | None
    is_regular_session: bool | None
    snapshot_timestamp: str | None
    quote_timestamp: str | None
    bid: float | None
    ask: float | None
    symbol_tradable: bool | None
    existing_position: bool
    conflicting_order: bool
    open_positions: int
    trades_today: int
    daily_realized_pnl: float | None
    daily_loss_blocked: bool
    risk_result: Mapping[str, Any]
    kill_switch: KillSwitchStatus
    live_trading_enabled: bool = config.LIVE_TRADING_ENABLED
    robinhood_execution_enabled: bool = config.ROBINHOOD_EXECUTION_ENABLED
    confirmation_token: str | None = config.LIVE_CONFIRMATION_TOKEN
    expected_confirmation_value: str | None = config.LIVE_CONFIRMATION_EXPECTED_VALUE
    unresolved_execution_state: bool = False


@dataclass(frozen=True)
class ExecutionGuardResult:
    allowed: bool
    reasons: tuple[str, ...]
    checked_at: str
    checks: Mapping[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutionGuard:
    def evaluate(
        self,
        plan: TradePlan,
        context: ExecutionContext,
        *,
        now: datetime,
    ) -> ExecutionGuardResult:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        failures: list[str] = []
        checks: dict[str, bool] = {}

        def check(name: str, condition: bool, reason: str) -> None:
            checks[name] = bool(condition)
            if not condition:
                failures.append(reason)

        check("known_mode", context.mode in {
            "ANALYSIS_ONLY", "SHADOW_TRADING", "REVIEW_ONLY", "LIVE_AUTONOMOUS"
        }, "UNKNOWN_MODE")
        check("live_mode", context.mode == "LIVE_AUTONOMOUS", "MODE_NOT_LIVE")
        check("live_enabled", context.live_trading_enabled, "LIVE_TRADING_DISABLED")
        check(
            "robinhood_enabled",
            context.robinhood_execution_enabled,
            "ROBINHOOD_EXECUTION_DISABLED",
        )
        confirmation_ok = (
            isinstance(context.confirmation_token, str)
            and bool(context.confirmation_token)
            and isinstance(context.expected_confirmation_value, str)
            and bool(context.expected_confirmation_value)
            and hmac.compare_digest(
                context.confirmation_token, context.expected_confirmation_value
            )
        )
        check("confirmation", confirmation_ok, "LIVE_CONFIRMATION_INVALID")
        check("kill_switch", not context.kill_switch.trading_blocked, context.kill_switch.status)
        check("agentic_account", context.agentic_account_count == 1, "AGENTIC_ACCOUNT_AMBIGUOUS")
        check("account_active", context.account_state == "active", "ACCOUNT_NOT_ACTIVE")

        if context.market_status is None or context.is_regular_session is None:
            failures.append("MARKET_STATUS_UNKNOWN")
            checks["market_open"] = False
        else:
            check(
                "market_open",
                context.market_status == "OPEN" and context.is_regular_session is True,
                "MARKET_CLOSED",
            )

        def fresh(value: str | None, max_age: int) -> bool:
            timestamp = parse_timestamp(value)
            if timestamp is None:
                return False
            age = (current - timestamp).total_seconds()
            return -30 <= age <= max_age

        check(
            "snapshot_fresh",
            fresh(context.snapshot_timestamp, config.SNAPSHOT_MAX_AGE_SECONDS),
            "STALE_SNAPSHOT",
        )
        check(
            "quote_fresh",
            fresh(context.quote_timestamp, config.MAX_QUOTE_AGE_SECONDS),
            "STALE_QUOTE",
        )
        check(
            "plan_fresh",
            fresh(plan.decision_timestamp, config.MAX_TRADE_PLAN_AGE_SECONDS),
            "EXPIRED_TRADE_PLAN",
        )
        check(
            "market_data_matches_plan",
            fresh(plan.market_data_timestamp, config.MAX_QUOTE_AGE_SECONDS),
            "STALE_PLAN_MARKET_DATA",
        )
        check(
            "risk_approved",
            plan.risk_manager_approved and context.risk_result.get("approved") is True,
            "RISK_REJECTED",
        )
        check("asset_type", plan.asset_type in config.LIVE_ALLOWED_ASSET_TYPES, "ASSET_TYPE_DISABLED")
        check("long_only", plan.side == "BUY" and not config.LIVE_ALLOW_SHORTING, "SIDE_DISABLED")
        check("symbol_tradable", context.symbol_tradable is True, "SYMBOL_NOT_TRADABLE")
        check("duplicate_position", not context.existing_position, "DUPLICATE_POSITION")
        check("duplicate_order", not context.conflicting_order, "DUPLICATE_ORDER")
        check("known_execution_state", not context.unresolved_execution_state, "RECONCILIATION_REQUIRED")
        check(
            "max_positions",
            0 <= context.open_positions < config.MAX_LIVE_OPEN_POSITIONS,
            "MAX_POSITIONS",
        )
        check(
            "max_trades",
            0 <= context.trades_today < config.MAX_LIVE_TRADES_PER_DAY,
            "MAX_TRADES",
        )
        check("daily_loss_state", not context.daily_loss_blocked, "DAILY_LOSS_LIMIT")

        equity = context.account_equity
        equity_ok = isinstance(equity, (int, float)) and equity > 0
        check("account_equity", equity_ok, "ACCOUNT_EQUITY_UNAVAILABLE")
        if equity_ok:
            equity_value = float(equity)
            check(
                "position_size",
                plan.notional <= equity_value * config.MAX_LIVE_POSITION_PERCENT,
                "MAX_POSITION_SIZE",
            )
            check(
                "risk_size",
                plan.maximum_expected_loss
                <= equity_value * config.MAX_LIVE_RISK_PER_TRADE_PERCENT,
                "MAX_RISK_PER_TRADE",
            )
            pnl = context.daily_realized_pnl
            daily_ok = (
                isinstance(pnl, (int, float))
                and float(pnl) > -equity_value * config.MAX_LIVE_DAILY_LOSS_PERCENT
            )
            check("daily_pnl", daily_ok, "DAILY_LOSS_LIMIT")

        spread_ok = False
        if (
            isinstance(context.bid, (int, float))
            and isinstance(context.ask, (int, float))
            and context.bid > 0
            and context.ask >= context.bid
        ):
            midpoint = (float(context.ask) + float(context.bid)) / 2
            spread_ok = (float(context.ask) - float(context.bid)) / midpoint <= config.MAX_ALLOWED_SPREAD_PERCENT
        check("spread", spread_ok, "SPREAD_TOO_WIDE_OR_UNKNOWN")

        safe_limits = (
            config.MAX_LIVE_POSITION_PERCENT <= config.MAX_POSITION_PERCENT
            and config.MAX_LIVE_RISK_PER_TRADE_PERCENT <= config.MAX_RISK_PER_TRADE_PERCENT
            and config.MAX_LIVE_DAILY_LOSS_PERCENT <= config.MAX_DAILY_LOSS_PERCENT
            and config.MAX_LIVE_OPEN_POSITIONS <= config.MAX_SIMULTANEOUS_POSITIONS
            and config.MAX_LIVE_TRADES_PER_DAY <= config.MAX_TRADES_PER_DAY
        )
        check("conservative_limits", safe_limits, "LIVE_RISK_CONFIGURATION_UNSAFE")
        check("options_disabled", not config.LIVE_ALLOW_OPTIONS, "OPTIONS_ENABLED")
        check("crypto_disabled", not config.LIVE_ALLOW_CRYPTO, "CRYPTO_ENABLED")
        check("margin_disabled", not config.LIVE_ALLOW_MARGIN_BORROWING, "MARGIN_ENABLED")
        check("overnight_disabled", not config.LIVE_ALLOW_OVERNIGHT, "OVERNIGHT_ENABLED")

        reasons = tuple(dict.fromkeys(failures))
        return ExecutionGuardResult(
            allowed=not reasons,
            reasons=reasons,
            checked_at=current.isoformat(),
            checks=checks,
        )
