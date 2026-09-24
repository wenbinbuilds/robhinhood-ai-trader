"""Causal slow-context/fast-trigger research engine for SCALP V2.

The engine consumes V1 diagnostic traces so V1 remains an unchanged control.
It owns no broker/provider client and no canonical ShadowPortfolio reference.
All entries and P&L produced here are explicitly hypothetical research data.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Mapping, Sequence

import config
from execution.execution_guard import read_kill_switch
from risk.risk_manager import RiskLimits, RiskManager, RiskRequest
from watcher.storage import atomic_json, event


SUPPORTED_SETUPS = frozenset({
    "MICRO_BREAKOUT", "MICRO_PULLBACK", "EMA9_CONTINUATION",
    "VWAP_RECLAIM", "MOMENTUM_BURST",
})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _utc(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        )
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class QuotePoint:
    evaluation_at: str
    exchange_at: str
    bid: float
    ask: float
    mid: float
    spread_pct: float
    quote_age_seconds: float


class CausalQuoteBuffer:
    """Exchange-time quote tape; duplicate exchange timestamps are replaced."""

    def __init__(self, window_seconds: int = config.SCALP_V2_QUOTE_BUFFER_SECONDS):
        self.window_seconds = int(window_seconds)
        self._points: dict[str, list[QuotePoint]] = defaultdict(list)

    def observe(self, trace: Mapping[str, Any]) -> QuotePoint | None:
        symbol = str(trace.get("symbol", "")).upper()
        provenance = trace.get("freshness", {}).get("provenance", {})
        evaluation = _utc(trace.get("timestamp"))
        exchange = _utc(provenance.get("exchange_timestamp"))
        bid = _number(provenance.get("bid"))
        ask = _number(provenance.get("ask"))
        mid = _number(provenance.get("mid"))
        spread = _number(provenance.get("spread_pct"))
        age = _number(trace.get("freshness", {}).get("quote_age_seconds"))
        if not symbol or None in (evaluation, exchange, bid, ask, mid, spread, age):
            return None
        if bid <= 0 or ask < bid or mid <= 0:
            return None
        point = QuotePoint(
            evaluation.isoformat(), exchange.isoformat(), bid, ask, mid, spread, age,
        )
        points = self._points[symbol]
        if points and points[-1].exchange_at == point.exchange_at:
            points[-1] = point
        elif not points or point.exchange_at > points[-1].exchange_at:
            points.append(point)
        cutoff = exchange.timestamp() - self.window_seconds
        self._points[symbol] = [
            row for row in points
            if (_utc(row.exchange_at) or exchange).timestamp() >= cutoff
        ]
        return point

    def points(self, symbol: str) -> tuple[QuotePoint, ...]:
        return tuple(self._points.get(symbol.upper(), ()))

    def _at_or_before(self, symbol: str, when: float) -> QuotePoint | None:
        points = self._points.get(symbol.upper(), ())
        stamps = [(_utc(row.exchange_at) or datetime.min.replace(
            tzinfo=timezone.utc)).timestamp() for row in points]
        index = bisect_right(stamps, when) - 1
        return points[index] if index >= 0 else None

    def features(self, symbol: str) -> dict[str, Any]:
        points = self._points.get(symbol.upper(), ())
        if not points:
            return {}
        latest = points[-1]
        latest_at = (_utc(latest.exchange_at) or datetime.min.replace(
            tzinfo=timezone.utc)).timestamp()

        def price_ago(seconds: int) -> float | None:
            point = self._at_or_before(symbol, latest_at - seconds)
            return point.mid if point else None

        def change(seconds: int) -> float | None:
            prior = price_ago(seconds)
            return latest.mid / prior - 1 if prior else None

        returns = {seconds: change(seconds) for seconds in (1, 3, 5, 10, 15, 30, 60)}

        def acceleration(seconds: int) -> float | None:
            recent = price_ago(seconds)
            older = price_ago(seconds * 2)
            if recent is None or older is None or not recent or not older:
                return None
            return (latest.mid / recent - 1) - (recent / older - 1)

        window = [
            row for row in points
            if (_utc(row.exchange_at) or datetime.min.replace(
                tzinfo=timezone.utc)).timestamp() >= latest_at - 30
        ]
        step_returns = [
            right.mid / left.mid - 1
            for left, right in zip(window, window[1:]) if left.mid
        ]
        volatility = (
            math.sqrt(sum(value * value for value in step_returns))
            if len(step_returns) >= 2 else None
        )
        oldest = window[0] if window else latest
        elapsed = latest_at - (_utc(oldest.exchange_at) or datetime.fromtimestamp(
            latest_at, timezone.utc
        )).timestamp()
        velocity = (latest.mid / oldest.mid - 1) / elapsed if elapsed > 0 else None
        previous = points[-2] if len(points) >= 2 else None
        return {
            "sample_count": len(points),
            "return_1s": returns[1], "return_3s": returns[3],
            "return_5s": returns[5], "return_10s": returns[10],
            "return_15s": returns[15], "return_30s": returns[30],
            "return_60s": returns[60],
            "acceleration_5s": acceleration(5),
            "acceleration_10s": acceleration(10),
            "acceleration_15s": acceleration(15),
            "realized_volatility_30s": volatility,
            "price_velocity_30s": velocity,
            "local_high_30s": max(row.mid for row in window),
            "local_low_30s": min(row.mid for row in window),
            "previous_mid": previous.mid if previous else None,
            "previous_exchange_at": previous.exchange_at if previous else None,
        }


@dataclass
class ArmedSetup:
    episode_id: str
    symbol: str
    setup_type: str
    armed_at: str
    trigger_price: float
    anchor_price: float
    invalidation_price: float
    context_snapshot: dict[str, Any]
    bar_timestamp: str | None
    volume_regime: str
    structural_reference: dict[str, Any]
    first_trigger_at: str | None = None
    crossed: bool = False
    retested: bool = False
    lowest_mid: float | None = None


@dataclass
class ResearchPosition:
    research_trade_id: str
    episode_id: str
    symbol: str
    setup_type: str
    opened_at: str
    entry_price: float
    stop: float
    target: float
    quantity: int = 1
    last_bid: float | None = None


@dataclass
class ResearchTrade:
    research_trade_id: str
    episode_id: str
    symbol: str
    setup_type: str
    opened_at: str
    closed_at: str
    entry_price: float
    exit_price: float
    exit_reason: str
    holding_seconds: float
    net_pnl: float
    return_pct: float


class HybridScalpResearchEngine:
    """Parallel V2 observer with isolated hypothetical state and accounting."""

    def __init__(self, *, state_path: str | Path | None = None,
                 events_path: str | Path | None = None, persist: bool = True):
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("HYBRID_SCALP is shadow research only")
        if config.LIVE_TRADING_ENABLED or config.ROBINHOOD_EXECUTION_ENABLED:
            raise ValueError("HYBRID_SCALP refuses live execution flags")
        if not read_kill_switch(config.LIVE_KILL_SWITCH_PATH).trading_blocked:
            raise ValueError("HYBRID_SCALP requires the blocked kill switch")
        self.strategy_id = config.SCALP_V2_STRATEGY_ID
        self.state_path = Path(state_path or config.SCALP_V2_STATE_PATH)
        self.events_path = Path(events_path or config.SCALP_V2_EVENT_LOG_PATH)
        self.persist = bool(persist)
        self.buffer = CausalQuoteBuffer()
        self.risk_manager = RiskManager(RiskLimits(
            max_position_percent=Decimal(str(min(
                config.MAX_POSITION_PERCENT, config.SCALP_MAX_POSITION_PERCENT,
            ))),
            max_risk_per_trade_percent=Decimal(str(min(
                config.MAX_RISK_PER_TRADE_PERCENT,
                config.SCALP_MAX_RISK_PER_TRADE_PERCENT,
            ))),
            max_daily_loss_percent=Decimal(str(min(
                config.MAX_DAILY_LOSS_PERCENT,
                config.SCALP_MAX_DAILY_LOSS_PERCENT,
            ))),
            max_simultaneous_positions=config.MAX_SIMULTANEOUS_POSITIONS,
            max_trades_per_day=max(
                config.MAX_TRADES_PER_DAY, config.SCALP_MAX_TRADES_PER_SESSION,
            ),
        ))
        self.armed: dict[str, ArmedSetup] = {}
        self.positions: dict[str, ResearchPosition] = {}
        self.trades: list[ResearchTrade] = []
        self.invalidated_episode_ids: set[str] = set()
        self.latest: dict[str, dict[str, Any]] = {}
        if self.persist:
            self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self.armed = {
                row["episode_id"]: ArmedSetup(**row)
                for row in value.get("armed", [])
            }
            self.positions = {
                row["symbol"]: ResearchPosition(**row)
                for row in value.get("open_positions", [])
            }
            self.trades = [ResearchTrade(**row) for row in value.get("closed_trades", [])]
            self.invalidated_episode_ids = set(value.get("invalidated_episode_ids", []))
        except (KeyError, TypeError):
            self.armed, self.positions, self.trades = {}, {}, []
            self.invalidated_episode_ids = set()

    def _save(self, now: datetime) -> None:
        if not self.persist:
            return
        atomic_json(self.state_path, {
            "strategy_id": self.strategy_id,
            "research_only": True,
            "updated_at": now.astimezone(timezone.utc).isoformat(),
            "armed": [asdict(row) for row in self.armed.values()],
            "open_positions": [asdict(row) for row in self.positions.values()],
            "closed_trades": [asdict(row) for row in self.trades[-1000:]],
            "invalidated_episode_ids": sorted(self.invalidated_episode_ids),
        })

    def fast_watch_symbols(self) -> list[str]:
        positions = list(self.positions)
        armed = [row.symbol for row in self.armed.values()]
        return list(dict.fromkeys(positions + armed))

    @staticmethod
    def context(trace: Mapping[str, Any]) -> tuple[str, list[str]]:
        flags = trace.get("stage_flags", {})
        setup = trace.get("setup_type")
        reasons = []
        if setup not in SUPPORTED_SETUPS:
            reasons.append("NO_SUPPORTED_STRUCTURE")
        if not flags.get("micro_bars_pass"):
            reasons.append("COMPLETED_CONTEXT_UNAVAILABLE")
        volume = trace.get("volume_expansion", {})
        if not volume.get("passed"):
            reasons.append("VOLUME_CONTEXT_NOT_ACCEPTED")
        geometry = trace.get("geometry", {}).get("structural_levels", {})
        if not any(_number(geometry.get(name)) for name in ("ema9", "vwap", "recent_low")):
            reasons.append("STRUCTURAL_REFERENCE_UNAVAILABLE")
        if reasons:
            hard = {"NO_SUPPORTED_STRUCTURE", "COMPLETED_CONTEXT_UNAVAILABLE",
                    "STRUCTURAL_REFERENCE_UNAVAILABLE"}
            status = "CONTEXT_REJECT" if hard.intersection(reasons) else "CONTEXT_NEUTRAL"
            return status, reasons
        return "CONTEXT_ACCEPT", []

    def _arm(self, trace: Mapping[str, Any], point: QuotePoint,
             features: Mapping[str, Any]) -> ArmedSetup | None:
        episode_id = str(trace.get("episode_id") or "")
        if not episode_id or episode_id in self.invalidated_episode_ids:
            return None
        existing = self.armed.get(episode_id)
        if existing is not None:
            existing.lowest_mid = min(existing.lowest_mid or point.mid, point.mid)
            return existing
        context_status, _ = self.context(trace)
        flags = trace.get("stage_flags", {})
        if context_status != "CONTEXT_ACCEPT" or not all(flags.get(name) for name in (
            "quote_fresh", "valid_bid_ask", "spread_pass",
        )):
            return None
        setup = str(trace.get("setup_type"))
        levels = trace.get("geometry", {}).get("structural_levels", {})
        lifecycle = trace.get("episode_lifecycle", {})
        ema9 = _number(levels.get("ema9"))
        vwap = _number(levels.get("vwap"))
        recent_low = _number(levels.get("recent_low"))
        anchor = _number(lifecycle.get("original_anchor_price")) or ema9 or vwap or point.mid
        if setup == "MICRO_BREAKOUT":
            trigger, invalidation = anchor, anchor * .999
        elif setup == "EMA9_CONTINUATION":
            trigger, invalidation = point.mid, (ema9 or anchor) * .999
        elif setup == "MICRO_PULLBACK":
            trigger = _number(features.get("local_high_30s")) or point.mid
            invalidation = recent_low or (ema9 or point.mid) * .999
        elif setup == "VWAP_RECLAIM":
            trigger, invalidation = vwap or anchor, (vwap or anchor) * .999
        else:
            trigger = point.mid
            invalidation = recent_low or point.mid * .998
        if invalidation >= point.ask:
            invalidation = min(point.bid * .999, point.ask * .998)
        armed = ArmedSetup(
            episode_id=episode_id, symbol=str(trace.get("symbol", "")).upper(),
            setup_type=setup, armed_at=point.evaluation_at,
            trigger_price=trigger, anchor_price=anchor,
            invalidation_price=invalidation,
            context_snapshot={
                "v1_score": trace.get("signal", {}).get("score"),
                "ema9": ema9, "ema20": lifecycle.get("fingerprint_fields", {}).get("ema20"),
                "vwap": vwap, "volume_expansion": trace.get(
                    "volume_expansion", {}
                ).get("observed"),
                "relative_strength": trace.get("signal", {}).get(
                    "components", {}
                ).get("relative_strength", {}).get("raw"),
            },
            bar_timestamp=trace.get("micro_bars", {}).get("latest_completed_bar_timestamp"),
            volume_regime=str(trace.get("volume_expansion", {}).get("status", "UNAVAILABLE")),
            structural_reference={
                "trigger_source": {
                    "MICRO_BREAKOUT": "BREAKOUT_ANCHOR",
                    "EMA9_CONTINUATION": "ARMING_QUOTE_AFTER_EMA_CONTEXT",
                    "MICRO_PULLBACK": "CAUSAL_30S_LOCAL_HIGH",
                    "VWAP_RECLAIM": "VWAP",
                    "MOMENTUM_BURST": "ARMING_QUOTE",
                }.get(setup),
                "ema9": ema9, "vwap": vwap, "recent_low": recent_low,
            },
            lowest_mid=point.mid,
        )
        self.armed[episode_id] = armed
        return armed

    def _expire_armed(self, trace: Mapping[str, Any], point: QuotePoint) -> list[str]:
        """Remove only causal structural invalidations from the research watch."""
        symbol = str(trace.get("symbol", "")).upper()
        current_episode = str(trace.get("episode_id") or "")
        current_bar = trace.get("micro_bars", {}).get("latest_completed_bar_timestamp")
        expired = []
        for episode_id, armed in list(self.armed.items()):
            if armed.symbol != symbol or symbol in self.positions:
                continue
            invalidated = point.mid <= armed.invalidation_price
            replaced = bool(
                current_episode and current_episode != episode_id
                and current_bar and armed.bar_timestamp
                and current_bar != armed.bar_timestamp
            )
            if invalidated or replaced:
                expired.append(episode_id)
                self.invalidated_episode_ids.add(episode_id)
                del self.armed[episode_id]
        return expired

    @staticmethod
    def trigger_variants(armed: ArmedSetup, point: QuotePoint,
                         features: Mapping[str, Any]) -> dict[str, bool]:
        r5 = _number(features.get("return_5s"))
        r10 = _number(features.get("return_10s"))
        accel5 = _number(features.get("acceleration_5s"))
        previous = _number(features.get("previous_mid"))
        local_high = _number(features.get("local_high_30s"))
        vol = _number(features.get("realized_volatility_30s"))
        positive = r5 is not None and r5 > 0
        accelerating = accel5 is not None and accel5 > 0
        crossed = previous is not None and previous <= armed.trigger_price < point.mid
        near_anchor = abs(point.mid / armed.anchor_price - 1) <= .003 if armed.anchor_price else False
        common = point.quote_age_seconds <= config.SCALP_MAX_QUOTE_AGE_SECONDS and (
            point.spread_pct <= config.SCALP_MAX_SPREAD_PCT
        )
        variants = {
            "BREAKOUT_CROSS": common and crossed and positive,
            "BREAKOUT_RETEST_RECLAIM": (
                common and armed.crossed and armed.retested
                and point.mid > armed.trigger_price and positive
            ),
            "EMA9_TURN_UP": common and near_anchor and positive and accelerating,
            "EMA9_POSITIVE_5S": common and near_anchor and positive,
            "PULLBACK_RECLAIM": (
                common and positive and accelerating and previous is not None
                and previous <= armed.trigger_price < point.mid
            ),
            "PULLBACK_TURN": (
                common and positive and accelerating and r10 is not None and r10 < 0
            ),
            "VWAP_RECLAIM": common and crossed and positive,
            "MOMENTUM_ACCELERATION": (
                common and positive and accelerating and vol is not None and vol > 0
                and (local_high is None or point.mid >= local_high * .9995)
            ),
        }
        return variants

    @staticmethod
    def selected_trigger(setup_type: str) -> str:
        return {
            "MICRO_BREAKOUT": "BREAKOUT_RETEST_RECLAIM",
            "EMA9_CONTINUATION": "EMA9_TURN_UP",
            "MICRO_PULLBACK": "PULLBACK_RECLAIM",
            "VWAP_RECLAIM": "VWAP_RECLAIM",
            "MOMENTUM_BURST": "MOMENTUM_ACCELERATION",
        }.get(setup_type, "UNSUPPORTED")

    @staticmethod
    def geometry(trace: Mapping[str, Any], armed: ArmedSetup,
                 point: QuotePoint, features: Mapping[str, Any]) -> dict[str, Any]:
        entry = point.ask
        stop = armed.invalidation_price
        risk = entry - stop
        volatility = _number(features.get("realized_volatility_30s")) or 0.0
        expected_move_pct = min(
            config.SCALP_MAX_EXPECTED_MOVE_PCT,
            max(config.SCALP_MIN_EXPECTED_MOVE_PCT, volatility * 2),
        )
        target = entry * (1 + expected_move_pct)
        reward = target - entry
        gross_rr = reward / risk if risk > 0 else None
        friction_pct = (
            point.spread_pct
            + config.SCALP_ENTRY_SLIPPAGE_BPS / 10_000
            + config.SCALP_EXIT_SLIPPAGE_BPS / 10_000
        )
        net_reward = reward - entry * friction_pct
        net_rr = net_reward / risk if risk > 0 else None
        extension = entry / armed.trigger_price - 1 if armed.trigger_price else None
        reasons = []
        if point.quote_age_seconds > config.SCALP_MAX_QUOTE_AGE_SECONDS:
            reasons.append("STALE_QUOTE")
        if point.spread_pct > config.SCALP_MAX_SPREAD_PCT:
            reasons.append("SPREAD_TOO_WIDE")
        if extension is None or extension > config.SCALP_V2_TRIGGER_MAX_EXTENSION_PCT:
            reasons.append("V2_TRIGGER_OVEREXTENDED")
        if risk <= 0 or risk / entry < config.SCALP_MIN_STOP_DISTANCE_PCT:
            reasons.append("V2_INVALID_STRUCTURAL_STOP")
        if expected_move_pct - friction_pct < config.SCALP_MIN_EXPECTED_NET_EDGE:
            reasons.append("V2_INSUFFICIENT_NET_EDGE")
        if gross_rr is None or gross_rr < config.SCALP_MIN_RISK_REWARD:
            reasons.append("V2_RR_BELOW_MINIMUM")
        if net_rr is None or net_rr < config.SCALP_MIN_RISK_REWARD:
            reasons.append("V2_NET_RR_BELOW_MINIMUM")
        return {
            "entry": entry, "stop": stop, "target": target,
            "risk_per_share": risk, "expected_move_pct": expected_move_pct,
            "friction_pct": friction_pct, "gross_rr": gross_rr,
            "net_rr": net_rr, "extension_from_trigger_pct": extension,
            "reasons": list(dict.fromkeys(reasons)), "passed": not reasons,
            "computed_from_current_quote": True,
        }

    def _manage_positions(self, point_by_symbol: Mapping[str, QuotePoint],
                          now: datetime) -> list[ResearchTrade]:
        closed = []
        for symbol, position in list(self.positions.items()):
            point = point_by_symbol.get(symbol)
            if point is None:
                continue
            position.last_bid = point.bid
            opened = _utc(position.opened_at)
            elapsed = (now - opened).total_seconds() if opened else 0.0
            reason = None
            if point.bid <= position.stop:
                reason = "V2_RESEARCH_STOP"
            elif point.bid >= position.target:
                reason = "V2_RESEARCH_TARGET"
            elif elapsed >= config.SCALP_V2_RESEARCH_MAX_HOLD_SECONDS:
                reason = "V2_RESEARCH_TIME_EXIT"
            if reason is None:
                continue
            exit_price = point.bid * (1 - config.SCALP_EXIT_SLIPPAGE_BPS / 10_000)
            pnl = (exit_price - position.entry_price) * position.quantity
            trade = ResearchTrade(
                position.research_trade_id, position.episode_id, symbol,
                position.setup_type, position.opened_at, now.isoformat(),
                position.entry_price, exit_price, reason, elapsed, pnl,
                exit_price / position.entry_price - 1,
            )
            self.trades.append(trade)
            closed.append(trade)
            del self.positions[symbol]
        return closed

    def _risk(self, symbol: str, geometry: Mapping[str, Any],
              now: datetime) -> dict[str, Any]:
        """Apply the V1 deterministic limits to the isolated V2 portfolio."""
        realized = sum(row.net_pnl for row in self.trades)
        unrealized = sum(
            ((row.last_bid or row.entry_price) - row.entry_price) * row.quantity
            for row in self.positions.values()
        )
        equity = config.SHADOW_STARTING_CAPITAL + realized + unrealized
        allocated = sum(
            row.entry_price * row.quantity for row in self.positions.values()
        )
        cash = config.SHADOW_STARTING_CAPITAL + realized - allocated
        scalp_buying_power = max(0.0, min(
            cash,
            equity * config.SCALP_CAPITAL_ALLOCATION_PERCENT - allocated,
        ))
        day = now.date()
        today = [
            row for row in self.trades
            if (_utc(row.closed_at) or datetime.min.replace(
                tzinfo=timezone.utc
            )).date() == day
        ]
        trades_today = len(today)
        daily_realized = sum(row.net_pnl for row in today)
        result = self.risk_manager.evaluate(RiskRequest(
            account_equity=equity,
            entry_price=geometry["entry"],
            stop_price=geometry["stop"],
            daily_realized_pnl=daily_realized,
            open_positions=len(self.positions),
            trades_today=trades_today,
            available_buying_power=scalp_buying_power,
        ))
        value = result.to_dict()
        value.update({
            "approved": result.approved and result.max_shares >= 1,
            "max_shares": result.max_shares,
            "starting_capital": config.SHADOW_STARTING_CAPITAL,
            "research_equity": equity,
            "research_buying_power": scalp_buying_power,
            "duplicate_symbol": symbol in self.positions,
        })
        if symbol in self.positions:
            value["approved"] = False
            value["reasons"] = [*value["reasons"], "duplicate research position"]
        return value

    def observe(self, traces: Sequence[Mapping[str, Any]], *, now: datetime | None = None,
                persist: bool | None = None) -> dict[str, Any]:
        started = perf_counter_ns()
        trace_times = [
            parsed for row in traces
            if (parsed := _utc(row.get("timestamp"))) is not None
        ]
        current = now or max(trace_times, default=datetime.now(timezone.utc))
        current = current.astimezone(timezone.utc)
        observations, point_by_symbol = [], {}
        for trace in traces:
            point = self.buffer.observe(trace)
            if point is None:
                continue
            symbol = str(trace.get("symbol", "")).upper()
            point_by_symbol[symbol] = point
            features = self.buffer.features(symbol)
            context_status, context_reasons = self.context(trace)
            expired_episodes = self._expire_armed(trace, point)
            armed = self._arm(trace, point, features)
            variants = self.trigger_variants(armed, point, features) if armed else {}
            if armed:
                if point.mid > armed.trigger_price:
                    armed.crossed = True
                if armed.crossed and point.mid <= armed.trigger_price * 1.001:
                    armed.retested = True
                armed.lowest_mid = min(armed.lowest_mid or point.mid, point.mid)
            selected = self.selected_trigger(armed.setup_type) if armed else "UNARMED"
            triggered = bool(armed and variants.get(selected))
            geometry = self.geometry(trace, armed, point, features) if triggered else None
            risk = (
                self._risk(symbol, geometry, current)
                if geometry and geometry["passed"] else None
            )
            decision = (
                "V2_ENTRY_READY" if risk and risk["approved"]
                else "V2_TRIGGER_RISK_BLOCKED" if risk
                else "V2_TRIGGER_GEOMETRY_BLOCKED" if triggered
                else "V2_ARMED_WAITING" if armed
                else context_status
            )
            if triggered and armed and armed.first_trigger_at is None:
                armed.first_trigger_at = point.evaluation_at
            if (decision == "V2_ENTRY_READY" and symbol not in self.positions
                    and armed is not None and geometry is not None):
                trade_id = f"V2-{armed.episode_id}-{point.exchange_at}"
                self.positions[symbol] = ResearchPosition(
                    trade_id, armed.episode_id, symbol, armed.setup_type,
                    point.evaluation_at,
                    geometry["entry"] * (1 + config.SCALP_ENTRY_SLIPPAGE_BPS / 10_000),
                    geometry["stop"], geometry["target"], risk["max_shares"],
                )
            observation = {
                "strategy_id": self.strategy_id, "research_only": True,
                "timestamp": point.evaluation_at, "exchange_timestamp": point.exchange_at,
                "symbol": symbol, "episode_id": trace.get("episode_id"),
                "setup_type": trace.get("setup_type"),
                "context_status": context_status, "context_reasons": context_reasons,
                "expired_episodes": expired_episodes,
                "armed": armed is not None,
                "armed_at": armed.armed_at if armed else None,
                "trigger_level": armed.trigger_price if armed else None,
                "anchor_price": armed.anchor_price if armed else None,
                "invalidation_price": armed.invalidation_price if armed else None,
                "bar_timestamp": armed.bar_timestamp if armed else None,
                "volume_regime": armed.volume_regime if armed else None,
                "v1_score": trace.get("signal", {}).get("score"),
                "v1_signal_pass": bool(trace.get("stage_flags", {}).get("signal_score_pass")),
                "v1_decision": trace.get("final"),
                "v1_entry": trace.get("geometry", {}).get("entry"),
                "v1_stop": trace.get("geometry", {}).get("stop"),
                "v1_target": trace.get("geometry", {}).get("target"),
                "v1_gross_rr": trace.get("geometry", {}).get("gross_rr"),
                "v1_net_rr": trace.get("geometry", {}).get("net_rr"),
                "bid": point.bid, "ask": point.ask, "mid": point.mid,
                "spread_pct": point.spread_pct,
                "quote_age_seconds": point.quote_age_seconds,
                **features,
                "distance_from_trigger_pct": (
                    point.mid / armed.trigger_price - 1 if armed else None
                ),
                "distance_from_ema9_pct": (
                    point.mid / armed.context_snapshot["ema9"] - 1
                    if armed and armed.context_snapshot.get("ema9") else None
                ),
                "distance_from_vwap_pct": (
                    point.mid / armed.context_snapshot["vwap"] - 1
                    if armed and armed.context_snapshot.get("vwap") else None
                ),
                "trigger_variants": variants, "selected_trigger": selected,
                "fast_trigger": triggered, "geometry": geometry, "risk": risk,
                "v2_decision": decision,
                "latency": {
                    "context_accepted_at": armed.armed_at if armed else None,
                    "setup_armed_at": armed.armed_at if armed else None,
                    "trigger_at": point.evaluation_at if triggered else None,
                    "armed_to_trigger_seconds": (
                        (_utc(point.evaluation_at) - _utc(armed.armed_at)).total_seconds()
                        if triggered and armed and _utc(point.evaluation_at)
                        and _utc(armed.armed_at) else None
                    ),
                    "trigger_to_entry_ready_ms": 0.0 if decision == "V2_ENTRY_READY" else None,
                },
            }
            self.latest[symbol] = observation
            observations.append(observation)
            if self.persist and (persist is not False):
                event(self.events_path, "SCALP_V2_RESEARCH_OBSERVATION", current,
                      max_bytes=config.FAST_EVENT_LOG_MAX_BYTES, **observation)
        closed = self._manage_positions(point_by_symbol, current)
        self._save(current) if persist is not False else None
        armed_symbols = sorted({row.symbol for row in self.armed.values()})
        return {
            "strategy_id": self.strategy_id, "research_only": True,
            "observations": observations,
            "armed": len(armed_symbols), "armed_symbols": armed_symbols,
            "fast_triggers": sum(row["fast_trigger"] for row in observations),
            "entry_ready": sum(row["v2_decision"] == "V2_ENTRY_READY" for row in observations),
            "open_research_positions": len(self.positions),
            "closed_research_trades": len(self.trades),
            "research_realized_pnl": sum(row.net_pnl for row in self.trades),
            "research_unrealized_pnl": sum(
                ((row.last_bid or row.entry_price) - row.entry_price) * row.quantity
                for row in self.positions.values()
            ),
            "closed_this_cycle": [asdict(row) for row in closed],
            "duration_ms": (perf_counter_ns() - started) / 1_000_000,
        }

    def on_v1_cycle(self, scalp_result: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        return self.observe(list(scalp_result.get("traces", [])), now=now)
