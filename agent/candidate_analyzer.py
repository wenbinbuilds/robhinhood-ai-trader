"""Deterministic first-pass intraday momentum candidate analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import config


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candle:
    begins_at: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    interpolated: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Candle | None:
        numbers = {
            "open": _number(value.get("open", value.get("open_price"))),
            "high": _number(value.get("high", value.get("high_price"))),
            "low": _number(value.get("low", value.get("low_price"))),
            "close": _number(value.get("close", value.get("close_price"))),
            "volume": _number(value.get("volume")),
        }
        begins_at = value.get("begins_at")
        if not isinstance(begins_at, str) or any(v is None for v in numbers.values()):
            return None
        return cls(
            begins_at=begins_at,
            open=numbers["open"],  # type: ignore[arg-type]
            high=numbers["high"],  # type: ignore[arg-type]
            low=numbers["low"],  # type: ignore[arg-type]
            close=numbers["close"],  # type: ignore[arg-type]
            volume=numbers["volume"],  # type: ignore[arg-type]
            interpolated=bool(value.get("interpolated", False)),
        )


@dataclass(frozen=True)
class CandidateData:
    symbol: str
    current_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    quote_as_of: str | None = None
    quote_retrieved_at: str | None = None
    quote_freshness_source: str | None = None
    volume: float | None = None
    relative_volume: float | None = None
    vwap: float | None = None
    ema9: float | None = None
    ema20: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    intraday_support_reference: float | None = None
    intraday_resistance_reference: float | None = None
    intraday_high: float | None = None
    intraday_low: float | None = None
    previous_close: float | None = None
    market_direction: str | None = None
    level2: Mapping[str, Any] | None = None
    candles: tuple[Candle, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CandidateData:
        raw_candles = value.get("candles", [])
        candles: list[Candle] = []
        if isinstance(raw_candles, Sequence) and not isinstance(raw_candles, (str, bytes)):
            for item in raw_candles:
                if isinstance(item, Mapping):
                    candle = Candle.from_mapping(item)
                    if candle is not None:
                        candles.append(candle)
        return cls(
            symbol=str(value.get("symbol", "")).upper().strip(),
            current_price=_number(value.get("current_price")),
            bid=_number(value.get("bid")),
            ask=_number(value.get("ask")),
            quote_as_of=value.get("quote_as_of") if isinstance(value.get("quote_as_of"), str) else None,
            quote_retrieved_at=(
                value.get("candidate_quote_retrieved_at", value.get("quote_retrieved_at"))
                if isinstance(
                    value.get("candidate_quote_retrieved_at", value.get("quote_retrieved_at")),
                    str,
                )
                else None
            ),
            quote_freshness_source=(
                str(value.get("quote_freshness_source"))
                if value.get("quote_freshness_source") is not None
                else None
            ),
            volume=_number(value.get("volume")),
            relative_volume=_number(value.get("relative_volume")),
            vwap=_number(value.get("vwap")),
            ema9=_number(value.get("ema9")),
            ema20=_number(value.get("ema20")),
            rsi14=_number(value.get("rsi14")),
            macd=_number(value.get("macd")),
            macd_signal=_number(value.get("macd_signal")),
            macd_histogram=_number(value.get("macd_histogram")),
            intraday_support_reference=_number(
                value.get("intraday_support_reference")
            ),
            intraday_resistance_reference=_number(
                value.get("intraday_resistance_reference")
            ),
            intraday_high=_number(value.get("intraday_high")),
            intraday_low=_number(value.get("intraday_low")),
            previous_close=_number(value.get("previous_close")),
            market_direction=(
                str(value.get("market_direction")).upper()
                if value.get("market_direction") is not None
                else None
            ),
            level2=value.get("level2") if isinstance(value.get("level2"), Mapping) else None,
            candles=tuple(candles),
        )


def quote_freshness(data: CandidateData, now: datetime) -> dict[str, Any]:
    """Resolve slow-cycle quote freshness without confusing it with fast quotes.

    Exchange time is authoritative when Robinhood supplies it.  The response
    receipt time is accepted only when no exchange timestamp exists and is
    explicitly reported as retrieval age.
    """

    now_utc = now.astimezone(timezone.utc)
    exchange_value = data.quote_as_of
    retrieval_value = data.quote_retrieved_at
    if isinstance(exchange_value, str) and exchange_value.strip():
        source = "exchange_timestamp"
        raw_timestamp = exchange_value
    elif isinstance(retrieval_value, str) and retrieval_value.strip():
        source = "retrieval_timestamp"
        raw_timestamp = retrieval_value
    else:
        return {
            "source": "unavailable",
            "timestamp": None,
            "age_seconds": None,
            "status": "QUOTE_TIMESTAMP_MISSING",
        }

    normalized = _timestamp(raw_timestamp)
    if normalized is None:
        return {
            "source": source,
            "timestamp": None,
            "age_seconds": None,
            "status": "QUOTE_TIMESTAMP_INVALID",
        }
    age = (now_utc - normalized).total_seconds()
    if age < 0:
        status = "QUOTE_TIMESTAMP_FUTURE"
    elif age > config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS:
        status = "QUOTE_STALE"
    else:
        status = "OK"
    return {
        "source": source,
        "timestamp": normalized,
        "age_seconds": age,
        "status": status,
    }


def completed_candles(candles: Sequence[Candle], now: datetime) -> tuple[Candle, ...]:
    """Return only five-minute bars whose end is at or before analysis time."""

    now_utc = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    result = []
    for candle in candles:
        began = _timestamp(candle.begins_at)
        if began is not None and began + timedelta(minutes=5) <= now_utc:
            result.append(candle)
    return tuple(result)


@dataclass(frozen=True)
class CandidateDecision:
    decision: str
    symbol: str
    confidence: float
    setup_name: str
    reasons: tuple[str, ...]
    unavailable_values: tuple[str, ...]
    strategy_score: float = 0.0
    max_strategy_score: float = 6.5
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_per_share: float | None = None
    risk_reward_ratio: float | None = None
    thesis: str | None = None
    invalidation_condition: str | None = None
    supporting_indicators: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "risk_per_share": self.risk_per_share,
            "risk_reward_ratio": self.risk_reward_ratio,
            "confidence": self.confidence,
            "strategy_score": self.strategy_score,
            "max_strategy_score": self.max_strategy_score,
            "setup_name": self.setup_name,
            "thesis": self.thesis,
            "invalidation_condition": self.invalidation_condition,
            "supporting_indicators": dict(self.supporting_indicators),
            "reasons": list(self.reasons),
            "unavailable_values": list(self.unavailable_values),
        }


class CandidateAnalyzer:
    """Evaluate a normalized data bundle without an LLM or broker side effects."""

    required_fields = (
        "current_price",
        "bid",
        "ask",
        "volume",
        "relative_volume",
        "vwap",
        "ema9",
        "ema20",
        "rsi14",
        "macd",
        "macd_signal",
    )

    def analyze(
        self, data: CandidateData, *, now: datetime | None = None
    ) -> CandidateDecision:
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        tracked_fields = (
            *self.required_fields,
            "macd_histogram",
            "intraday_support_reference",
            "intraday_resistance_reference",
            "intraday_high",
            "intraday_low",
            "market_direction",
            "level2",
        )
        unavailable = [name for name in tracked_fields if getattr(data, name) is None]
        missing_required = [
            name for name in self.required_fields if getattr(data, name) is None
        ]
        real_candles = completed_candles(
            tuple(c for c in data.candles if not c.interpolated), now_utc
        )
        if len(real_candles) < config.MIN_ANALYSIS_5_MINUTE_CANDLES:
            unavailable.append("sufficient_5_minute_candles")
            missing_required.append("sufficient_5_minute_candles")

        blockers: list[str] = []
        if not data.symbol:
            blockers.append("symbol is unavailable")
        if missing_required:
            blockers.append("required market data is missing or insufficient")

        freshness = quote_freshness(data, now_utc)
        if freshness["status"] != "OK":
            blockers.append(str(freshness["status"]))
            unavailable.append("quote_freshness")

        if data.current_price is not None and not 10 <= data.current_price <= 500:
            blockers.append("price is outside the configured scanner range")
        if data.bid is not None and data.ask is not None:
            if data.bid <= 0 or data.ask <= 0 or data.ask < data.bid:
                blockers.append("bid/ask data is invalid or conflicting")
            else:
                midpoint = (data.bid + data.ask) / 2
                spread_percent = (data.ask - data.bid) / midpoint
                if spread_percent > config.MAX_SPREAD_PERCENT:
                    blockers.append("bid/ask spread is too wide")
        else:
            spread_percent = None

        if blockers:
            return self._no_trade(data, 0.0, blockers, unavailable)

        assert data.current_price is not None
        assert data.bid is not None and data.ask is not None
        assert data.vwap is not None
        assert data.ema9 is not None and data.ema20 is not None
        assert data.rsi14 is not None
        assert data.macd is not None and data.macd_signal is not None
        assert data.relative_volume is not None

        evidence: list[str] = []
        conflicts: list[str] = []
        score = 0.0

        if data.current_price > data.vwap:
            evidence.append("price_above_vwap")
            score += 1.0
        else:
            conflicts.append("price_at_or_below_vwap")

        if data.ema9 > data.ema20:
            evidence.append("ema9_above_ema20")
            score += 1.0
        else:
            conflicts.append("ema9_at_or_below_ema20")

        if 50 < data.rsi14 < 70:
            evidence.append("rsi_bullish_not_overbought")
            score += 1.0
        elif 70 <= data.rsi14 <= 75:
            evidence.append("rsi_bullish_elevated")
            score += 0.5
        else:
            conflicts.append("rsi_not_in_preferred_range")

        histogram_positive = (
            data.macd_histogram is None or data.macd_histogram > 0
        )
        if data.macd > data.macd_signal and histogram_positive:
            evidence.append("positive_macd_structure")
            score += 1.0
        else:
            conflicts.append("macd_structure_not_positive")

        if data.relative_volume >= 1.2:
            evidence.append("above_average_relative_volume")
            score += 1.0
        else:
            conflicts.append("relative_volume_below_preference")

        recent_candles = real_candles[-6:]
        positive_bars = sum(c.close > c.open for c in recent_candles)
        constructive = (
            recent_candles[-1].close > recent_candles[0].open
            and positive_bars >= 4
        )
        if constructive:
            evidence.append("constructive_5_minute_price_action")
            score += 1.0
        else:
            conflicts.append("5_minute_price_action_not_constructive")

        if data.market_direction == "BULLISH":
            evidence.append("bullish_overall_market_direction")
            score += 0.5
        elif data.market_direction == "BEARISH":
            conflicts.append("bearish_overall_market_direction")

        confidence = max(0.0, min(1.0, score / 6.5 - 0.06 * len(conflicts)))
        signal_quality_failures = []
        if score < 4.0:
            signal_quality_failures.append("MINIMUM_STRATEGY_SCORE")
        if len(conflicts) > 2:
            signal_quality_failures.append("MAXIMUM_CONFLICTS")
        if confidence < config.MIN_CANDIDATE_CONFIDENCE:
            signal_quality_failures.append("MINIMUM_CONFIDENCE")

        candle_low = min(c.low for c in real_candles)
        candle_high = max(c.high for c in real_candles)
        intraday_low = data.intraday_low if data.intraday_low is not None else candle_low
        intraday_high = data.intraday_high if data.intraday_high is not None else candle_high
        support_reference = (
            data.intraday_support_reference
            if data.intraday_support_reference is not None
            else candle_low
        )
        resistance_reference = (
            data.intraday_resistance_reference
            if data.intraday_resistance_reference is not None
            else candle_high
        )
        for derived_field in (
            "intraday_support_reference",
            "intraday_resistance_reference",
            "intraday_high",
            "intraday_low",
        ):
            if derived_field in unavailable:
                unavailable.remove(derived_field)

        entry = max(data.current_price, data.ask)
        stop_candidates = [
            value
            for value in (support_reference, intraday_low, data.ema20, data.vwap)
            if value is not None and 0 < value < entry
        ]
        if not stop_candidates:
            return self._no_trade(
                data,
                confidence,
                ["no defensible stop level is available below entry"],
                unavailable,
                evidence,
                strategy_score=score,
            )
        stop = max(stop_candidates)
        risk_per_share = entry - stop
        if risk_per_share / entry < 0.002:
            return self._no_trade(
                data,
                confidence,
                ["derived stop is too close to entry for reliable sizing"],
                unavailable,
                evidence,
                strategy_score=score,
            )
        if resistance_reference is None or resistance_reference <= entry:
            return self._no_trade(
                data,
                confidence,
                ["no intraday resistance reference is available above entry"],
                unavailable,
                evidence,
                strategy_score=score,
            )

        target = resistance_reference
        risk_reward = (target - entry) / risk_per_share
        if risk_reward < config.MIN_RISK_REWARD_RATIO:
            return self._no_trade(
                data,
                confidence,
                ["intraday resistance reference does not provide sufficient reward for the risk"],
                unavailable,
                evidence,
                strategy_score=score,
            )

        indicators = {
            "current_price": data.current_price,
            "bid": data.bid,
            "ask": data.ask,
            "spread_percent": spread_percent,
            "volume": data.volume,
            "relative_volume": data.relative_volume,
            "vwap": data.vwap,
            "ema9": data.ema9,
            "ema20": data.ema20,
            "rsi14": data.rsi14,
            "macd": data.macd,
            "macd_signal": data.macd_signal,
            "macd_histogram": data.macd_histogram,
            "intraday_support_reference": support_reference,
            "intraday_resistance_reference": resistance_reference,
            "intraday_high": intraday_high,
            "intraday_low": intraday_low,
            "market_direction": data.market_direction,
            "level2": data.level2,
            "intraday_support_reference_source": (
                "provided"
                if data.intraday_support_reference is not None
                else "derived_from_5_minute_candles"
            ),
            "intraday_resistance_reference_source": (
                "provided"
                if data.intraday_resistance_reference is not None
                else "derived_from_5_minute_candles"
            ),
            "evidence": evidence,
            "signal_quality_failures": signal_quality_failures,
            "technical_confidence": round(confidence, 3),
        }
        monitor_only = bool(signal_quality_failures)
        return CandidateDecision(
            decision="WATCH" if monitor_only else "TRADE_CANDIDATE",
            symbol=data.symbol,
            direction="LONG",
            entry=round(entry, 4),
            stop=round(stop, 4),
            target=round(target, 4),
            risk_per_share=round(risk_per_share, 4),
            risk_reward_ratio=round(risk_reward, 2),
            confidence=round(confidence, 3),
            strategy_score=round(score, 2),
            setup_name=config.STRATEGY_NAME,
            thesis=(
                "Intraday momentum has valid market data and trade geometry; "
                "signal-quality warnings require live confirmation."
                if monitor_only else
                "Bullish intraday momentum is supported by multiple independent "
                "price, trend, momentum, and volume signals."
            ),
            invalidation_condition=f"Price reaches or closes below {stop:.4f}.",
            supporting_indicators=indicators,
            reasons=(
                tuple(f"SIGNAL_QUALITY_WARNING: {name}" for name in signal_quality_failures)
                + tuple(conflicts)
                if monitor_only else
                ("passed deterministic candidate analysis",)
            ),
            unavailable_values=tuple(dict.fromkeys(unavailable)),
        )

    def _no_trade(
        self,
        data: CandidateData,
        confidence: float,
        reasons: Sequence[str],
        unavailable: Sequence[str],
        evidence: Sequence[str] = (),
        *,
        strategy_score: float = 0.0,
    ) -> CandidateDecision:
        return CandidateDecision(
            decision="NO_TRADE",
            symbol=data.symbol,
            confidence=round(confidence, 3),
            strategy_score=round(strategy_score, 2),
            setup_name=config.STRATEGY_NAME,
            reasons=tuple(dict.fromkeys(reasons)),
            unavailable_values=tuple(dict.fromkeys(unavailable)),
            supporting_indicators=self._supporting_data(data, evidence),
        )

    @staticmethod
    def _supporting_data(
        data: CandidateData, evidence: Sequence[str]
    ) -> dict[str, Any]:
        spread_percent: float | None = None
        if data.bid is not None and data.ask is not None and data.bid + data.ask > 0:
            spread_percent = (data.ask - data.bid) / ((data.ask + data.bid) / 2)
        return {
            "current_price": data.current_price,
            "bid": data.bid,
            "ask": data.ask,
            "spread_percent": spread_percent,
            "volume": data.volume,
            "relative_volume": data.relative_volume,
            "vwap": data.vwap,
            "ema9": data.ema9,
            "ema20": data.ema20,
            "rsi14": data.rsi14,
            "macd": data.macd,
            "macd_signal": data.macd_signal,
            "macd_histogram": data.macd_histogram,
            "intraday_support_reference": data.intraday_support_reference,
            "intraday_resistance_reference": data.intraday_resistance_reference,
            "intraday_high": data.intraday_high,
            "intraday_low": data.intraday_low,
            "market_direction": data.market_direction,
            "level2": data.level2,
            "evidence": list(evidence),
        }
