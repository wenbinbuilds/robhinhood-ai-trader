"""Typed wrapper around the existing deterministic momentum analyzer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from agent.candidate_analyzer import completed_candles, quote_freshness
from agent.gate_policy import TRUE_HARD_CLASSIFICATIONS, failed_gates, policy_for

import config
from agent.candidate_analyzer import CandidateAnalyzer, CandidateData
from agent.models import TechnicalAssessment, TechnicalContext


class TechnicalAgent:
    def __init__(self, analyzer: CandidateAnalyzer | None = None) -> None:
        self.analyzer = analyzer or CandidateAnalyzer()

    def analyze(
        self, data: CandidateData, *, now: datetime | None = None
    ) -> TechnicalAssessment:
        analysis_started_at = now or datetime.now(timezone.utc)
        if analysis_started_at.tzinfo is None:
            analysis_started_at = analysis_started_at.replace(tzinfo=timezone.utc)
        analysis_started_at = analysis_started_at.astimezone(timezone.utc)
        plan = self.analyzer.analyze(data, now=analysis_started_at).to_dict()
        legacy_decision = plan.pop("decision", "NO_TRADE")
        plan["technical_disposition"] = {
            "TRADE_CANDIDATE": "QUALIFIED",
            "WATCH": "MONITORABLE",
        }.get(legacy_decision, "REJECTED")
        evidence = tuple(plan.get("supporting_indicators", {}).get("evidence", []))
        reasons = tuple(str(item) for item in plan.get("reasons", []))
        maximum = float(plan.get("max_strategy_score", 6.5) or 6.5)
        raw_score = float(plan.get("strategy_score", 0.0) or 0.0)

        if data.ema9 is None or data.ema20 is None:
            ema_structure = "UNKNOWN"
            trend = "UNKNOWN"
        elif data.ema9 > data.ema20:
            ema_structure = "BULLISH"
            trend = "UPTREND"
        elif data.ema9 < data.ema20:
            ema_structure = "BEARISH"
            trend = "DOWNTREND"
        else:
            ema_structure = "FLAT"
            trend = "SIDEWAYS"

        price_vs_vwap = "UNKNOWN"
        if data.current_price is not None and data.vwap is not None:
            price_vs_vwap = "ABOVE" if data.current_price > data.vwap else "BELOW_OR_EQUAL"

        if data.rsi14 is None:
            rsi_state = "UNKNOWN"
        elif data.rsi14 >= 75:
            rsi_state = "OVERBOUGHT"
        elif data.rsi14 > 50:
            rsi_state = "BULLISH"
        elif data.rsi14 < 30:
            rsi_state = "OVERSOLD"
        else:
            rsi_state = "WEAK"

        if data.macd is None or data.macd_signal is None:
            macd_state = "UNKNOWN"
        else:
            macd_state = "BULLISH" if data.macd > data.macd_signal else "BEARISH"

        if data.relative_volume is None:
            volume_confirmation = "UNKNOWN"
        else:
            volume_confirmation = "CONFIRMED" if data.relative_volume >= 1.2 else "WEAK"

        real = list(completed_candles(
            tuple(candle for candle in data.candles if not candle.interpolated),
            analysis_started_at,
        ))
        if len(real) < 6:
            price_action = "UNKNOWN"
        else:
            positive = sum(candle.close > candle.open for candle in real[-6:])
            price_action = (
                "CONSTRUCTIVE"
                if real[-1].close > real[0].open and positive >= 4
                else "NON_CONSTRUCTIVE"
            )

        context = TechnicalContext(
            symbol=data.symbol,
            trend=trend,
            price_vs_vwap=price_vs_vwap,
            ema_structure=ema_structure,
            rsi_state=rsi_state,
            macd_state=macd_state,
            volume_confirmation=volume_confirmation,
            price_action_state=price_action,
            technical_score=round(max(0.0, min(1.0, raw_score / maximum)), 3),
            confidence=float(plan.get("confidence", 0.0) or 0.0),
            evidence=evidence,
            conflicts=reasons if plan["technical_disposition"] != "QUALIFIED" else (),
            unavailable_fields=tuple(plan.get("unavailable_values", [])),
        )
        spread = (
            data.ask - data.bid
            if data.ask is not None and data.bid is not None
            else None
        )
        midpoint = (
            (data.ask + data.bid) / 2
            if data.ask is not None
            and data.bid is not None
            and data.ask + data.bid > 0
            else None
        )
        intraday_change = (
            data.current_price - data.previous_close
            if data.current_price is not None and data.previous_close not in {None, 0}
            else None
        )
        freshness = quote_freshness(data, analysis_started_at)
        infrastructure_reasons = {
            "QUOTE_TIMESTAMP_MISSING",
            "QUOTE_TIMESTAMP_INVALID",
            "QUOTE_TIMESTAMP_FUTURE",
            "QUOTE_STALE",
            "required market data is missing or insufficient",
            "bid/ask data is invalid or conflicting",
        }
        technical_data_valid = not any(
            reason in infrastructure_reasons for reason in reasons
        )
        validation = self._validation_audit(
            data,
            plan,
            analysis_started_at,
            spread_percent=(spread / midpoint if spread is not None and midpoint else None),
        )
        metrics = {
            "analysis_started_at": analysis_started_at.isoformat(),
            "quote_freshness_source": freshness["source"],
            "quote_freshness_status": freshness["status"],
            "quote_age_at_analysis_seconds": freshness["age_seconds"],
            "technical_data_valid": technical_data_valid,
            "technical_validation": validation,
            "analysis_candle_count": len(real),
            "latest_completed_bar_timestamp": real[-1].begins_at if real else None,
            "symbol": data.symbol,
            "current_price": data.current_price,
            "bid": data.bid,
            "ask": data.ask,
            "spread": spread,
            "spread_percent": spread / midpoint if spread is not None and midpoint else None,
            "quote_as_of": data.quote_as_of,
            "intraday_change": intraday_change,
            "intraday_change_percent": (
                intraday_change / data.previous_close
                if intraday_change is not None and data.previous_close
                else None
            ),
            "volume": data.volume,
            "relative_volume": data.relative_volume,
            "vwap": data.vwap,
            "price_vs_vwap": price_vs_vwap,
            "ema9": data.ema9,
            "ema20": data.ema20,
            "ema_relationship": ema_structure,
            "rsi14": data.rsi14,
            "macd": data.macd,
            "macd_signal": data.macd_signal,
            "macd_histogram": data.macd_histogram,
            "intraday_high": data.intraday_high,
            "intraday_low": data.intraday_low,
            "distance_to_intraday_high_percent": (
                (data.intraday_high - data.current_price) / data.current_price
                if data.intraday_high is not None and data.current_price
                else None
            ),
            "distance_from_intraday_low_percent": (
                (data.current_price - data.intraday_low) / data.current_price
                if data.intraday_low is not None and data.current_price
                else None
            ),
            "recent_5_minute_candle_structure": price_action,
            "recent_5_minute_candles": [
                {
                    "begins_at": candle.begins_at,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in real[-config.LLM_REASONING_MAX_CANDLES :]
            ],
            "preliminary_technical_score": context.technical_score,
        }
        return TechnicalAssessment(
            context=context, candidate_plan=plan, metrics=metrics
        )

    @staticmethod
    def _validation_audit(
        data: CandidateData,
        plan: dict,
        now: datetime,
        *,
        spread_percent: float | None,
    ) -> dict:
        """Describe the existing rules without changing their behavior."""

        all_real = [candle for candle in data.candles if not candle.interpolated]
        real = list(completed_candles(tuple(all_real), now))
        freshness = quote_freshness(data, now)
        required = CandidateAnalyzer.required_fields
        missing = [name for name in required if getattr(data, name) is None]
        recent = real[-6:]
        positive_bars = sum(c.close > c.open for c in recent)
        constructive = bool(
            len(real) >= 6
            and recent[-1].close > recent[0].open
            and positive_bars >= 4
        )
        histogram_ok = bool(
            data.macd is not None
            and data.macd_signal is not None
            and data.macd > data.macd_signal
            and (data.macd_histogram is None or data.macd_histogram > 0)
        )
        soft = [
            ("PRICE_VS_VWAP", {"price": data.current_price, "vwap": data.vwap}, "price > VWAP", data.current_price is not None and data.vwap is not None and data.current_price > data.vwap, 1.0),
            ("EMA_STRUCTURE", {"ema9": data.ema9, "ema20": data.ema20}, "EMA9 > EMA20", data.ema9 is not None and data.ema20 is not None and data.ema9 > data.ema20, 1.0),
            ("RSI", data.rsi14, "50 < RSI14 < 70 (+1.0), or 70 <= RSI14 <= 75 (+0.5)", data.rsi14 is not None and 50 < data.rsi14 <= 75, 1.0 if data.rsi14 is not None and 50 < data.rsi14 < 70 else 0.5 if data.rsi14 is not None and 70 <= data.rsi14 <= 75 else 0.0),
            ("MACD", {"macd": data.macd, "signal": data.macd_signal, "histogram": data.macd_histogram}, "MACD > signal and histogram > 0 when histogram is available", histogram_ok, 1.0),
            ("RELATIVE_VOLUME", data.relative_volume, "relative volume >= 1.2", data.relative_volume is not None and data.relative_volume >= 1.2, 1.0),
            ("CANDLE_STRUCTURE", {"last_6_positive": positive_bars, "last_close": recent[-1].close if recent else None, "first_open_recent_6": recent[0].open if recent else None}, "last close > first open of recent six and >= 4 of recent six bars positive", constructive, 1.0),
            ("MARKET_DIRECTION", data.market_direction, "BULLISH adds 0.5; BEARISH adds a conflict", data.market_direction == "BULLISH", 0.5 if data.market_direction == "BULLISH" else 0.0),
        ]
        rules = [
            TechnicalAgent._rule("SYMBOL_PRESENT", data.symbol, "non-empty symbol", bool(data.symbol), "HARD"),
            TechnicalAgent._rule("REQUIRED_FIELDS_PRESENT", missing, "no required fields missing", not missing, "HARD"),
            TechnicalAgent._rule("MINIMUM_CANDLES", len(real), f">= {config.MIN_ANALYSIS_5_MINUTE_CANDLES} real candles", len(real) >= config.MIN_ANALYSIS_5_MINUTE_CANDLES, "HARD"),
            TechnicalAgent._rule("QUOTE_FRESHNESS", {"source": freshness["source"], "age_seconds": freshness["age_seconds"], "status": freshness["status"]}, f"age between 0 and {config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS}s", freshness["status"] == "OK", "HARD"),
            TechnicalAgent._rule("PRICE_RANGE", data.current_price, "$10 <= price <= $500", data.current_price is not None and 10 <= data.current_price <= 500, "HARD"),
            TechnicalAgent._rule("SPREAD", spread_percent, f"spread <= {config.MAX_SPREAD_PERCENT} ({config.MAX_SPREAD_PERCENT * 100:.3f}%)", spread_percent is not None and 0 <= spread_percent <= config.MAX_SPREAD_PERCENT, "HARD"),
        ]
        for name, actual, requirement, passed, contribution in soft:
            rules.append(TechnicalAgent._rule(name, actual, requirement, passed, "SOFT", contribution, 1.0 if name != "MARKET_DIRECTION" else 0.5))

        score = float(plan.get("strategy_score", 0.0) or 0.0)
        confidence = float(plan.get("confidence", 0.0) or 0.0)
        conflicts = sum(1 for name, _actual, _required, passed, _points in soft[:6] if not passed)
        if data.market_direction == "BEARISH":
            conflicts += 1
        rules.extend([
            TechnicalAgent._rule("MINIMUM_STRATEGY_SCORE", score, ">= 4.0 points", score >= 4.0, "HARD"),
            TechnicalAgent._rule("MAXIMUM_CONFLICTS", conflicts, "<= 2 conflicts", conflicts <= 2, "HARD"),
            TechnicalAgent._rule("MINIMUM_CONFIDENCE", confidence, f">= {config.MIN_CANDIDATE_CONFIDENCE}", confidence >= config.MIN_CANDIDATE_CONFIDENCE, "HARD"),
        ])

        entry = max(data.current_price, data.ask) if data.current_price is not None and data.ask is not None else None
        stop_candidates = [value for value in (data.intraday_support_reference, data.intraday_low, data.ema20, data.vwap) if entry is not None and value is not None and 0 < value < entry]
        stop = max(stop_candidates) if stop_candidates else None
        stop_ratio = (entry - stop) / entry if entry and stop is not None else None
        resistance = data.intraday_resistance_reference
        reward_ratio = ((resistance - entry) / (entry - stop) if resistance is not None and entry is not None and stop is not None and entry > stop else None)
        stop_available = stop is not None
        stop_distance_ok = stop_ratio is not None and stop_ratio >= 0.002
        resistance_ok = resistance is not None and entry is not None and resistance > entry
        reward_ok = reward_ratio is not None and reward_ratio >= config.MIN_RISK_REWARD_RATIO
        downstream = [
            ("STOP_REFERENCE_AVAILABLE", stop, "a valid support/low/EMA20/VWAP below entry", stop_available, True),
            ("MINIMUM_STOP_DISTANCE", stop_ratio, ">= 0.002 (0.2% of entry)", stop_distance_ok, stop_available),
            ("RESISTANCE_ABOVE_ENTRY", {"resistance": resistance, "entry": entry}, "resistance > entry", resistance_ok, stop_available and stop_distance_ok),
            ("MINIMUM_RISK_REWARD", reward_ratio, f">= {config.MIN_RISK_REWARD_RATIO}", reward_ok, stop_available and stop_distance_ok and resistance_ok),
        ]
        for name, actual, requirement, passed, evaluated in downstream:
            rules.append(TechnicalAgent._rule(name, actual, requirement, passed, "HARD", evaluated=evaluated))

        completed = 0
        forming = 0
        for candle in all_real:
            try:
                began = datetime.fromisoformat(candle.begins_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                continue
            if began + timedelta(minutes=5) <= now:
                completed += 1
            elif began <= now < began + timedelta(minutes=5):
                forming += 1
        evaluated_hard = [
            rule for rule in rules
            if rule["classification"] in TRUE_HARD_CLASSIFICATIONS
            and rule["status"] != "NOT_EVALUATED"
        ]
        result = {
            "rules": rules,
            "hard_gates_passed": sum(rule["status"] == "PASS" for rule in evaluated_hard),
            "hard_gates_evaluated": len(evaluated_hard),
            "failed_hard_gates": [],
            "true_hard_gate_failures": [],
            "signal_quality_failures": [],
            "completed_candles": completed,
            "forming_candles": forming,
            "candle_policy": "COMPLETED_NON_INTERPOLATED_5_MINUTE_BARS",
            "forming_candle_used": False,
            "technical_score": round(score / 6.5, 3),
            "raw_strategy_score": score,
            "confidence": confidence,
            "technical_confidence": confidence,
            "technical_disposition": plan.get("technical_disposition"),
        }
        hard_failures, signal_failures = failed_gates(result)
        result["failed_hard_gates"] = hard_failures
        result["true_hard_gate_failures"] = hard_failures
        result["signal_quality_failures"] = signal_failures
        return result

    @staticmethod
    def _rule(name, actual, required, passed, rule_type, contribution=0.0, maximum=0.0, *, evaluated=True):
        policy = policy_for(name)
        if policy.classification.value in TRUE_HARD_CLASSIFICATIONS:
            display_type = "HARD"
        elif maximum:
            display_type = "SOFT"
        else:
            display_type = "SIGNAL_QUALITY"
        return {
            "rule_name": name,
            "actual": actual,
            "required": required,
            "status": "NOT_EVALUATED" if not evaluated else "PASS" if passed else "FAIL",
            "type": display_type,
            "classification": policy.classification.value,
            "permanently_rejects": policy.permanently_rejects,
            "prevents_fast_watch": policy.prevents_fast_watch,
            "reevaluated_later": policy.reevaluated_later,
            "score_contribution": contribution if passed else 0.0,
            "maximum_score_contribution": maximum,
        }
