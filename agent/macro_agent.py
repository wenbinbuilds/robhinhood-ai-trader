"""Deterministic broad-market evidence; never selects a security."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.models import MarketContext


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


class MacroAgent:
    """Aggregate SPY/QQQ trend signals once per market cycle."""

    @staticmethod
    def _benchmark_bias(value: Mapping[str, Any]) -> tuple[str, list[str], int, int]:
        symbol = str(value.get("symbol", "INDEX")).upper()
        positive = negative = 0
        evidence: list[str] = []
        comparisons = (
            ("price_vs_vwap", _number(value.get("current_price")), _number(value.get("vwap"))),
            ("ema9_vs_ema20", _number(value.get("ema9")), _number(value.get("ema20"))),
            ("intraday_change", _number(value.get("intraday_change_percent")), 0.0),
        )
        for label, left, right in comparisons:
            if left is None or right is None or left == right:
                continue
            if left > right:
                positive += 1
                evidence.append(f"{symbol}_{label}_bullish")
            else:
                negative += 1
                evidence.append(f"{symbol}_{label}_bearish")

        candles = value.get("candles", [])
        if isinstance(candles, Sequence) and not isinstance(candles, (str, bytes)):
            real = [item for item in candles if isinstance(item, Mapping) and not item.get("interpolated")]
            if len(real) >= 2:
                first = _number(real[0].get("open"))
                last = _number(real[-1].get("close"))
                if first is not None and last is not None and first != last:
                    if last > first:
                        positive += 1
                        evidence.append(f"{symbol}_recent_5m_action_bullish")
                    else:
                        negative += 1
                        evidence.append(f"{symbol}_recent_5m_action_bearish")

        if positive > negative:
            bias = "BULLISH"
        elif negative > positive:
            bias = "BEARISH"
        elif positive + negative:
            bias = "MIXED"
        else:
            bias = "UNKNOWN"
        return bias, evidence, positive, negative

    def analyze(self, market: Mapping[str, Any]) -> MarketContext:
        benchmarks = market.get("benchmarks", [])
        by_symbol = {
            str(item.get("symbol", "")).upper(): item
            for item in benchmarks
            if isinstance(item, Mapping)
        } if isinstance(benchmarks, Sequence) and not isinstance(benchmarks, (str, bytes)) else {}

        evidence: list[str] = []
        unavailable: list[str] = []
        biases: dict[str, str] = {}
        positive = negative = valid = 0
        for symbol in ("SPY", "QQQ"):
            item = by_symbol.get(symbol)
            if item is None:
                biases[symbol] = "UNKNOWN"
                unavailable.append(symbol.lower())
                continue
            bias, reasons, up, down = self._benchmark_bias(item)
            biases[symbol] = bias
            evidence.extend(reasons)
            positive += up
            negative += down
            valid += up + down

        if valid == 0:
            supplied = str(market.get("direction", "UNKNOWN")).upper()
            if supplied in {"BULLISH", "BEARISH", "MIXED"}:
                regime = supplied
                score = 0.8 if supplied == "BULLISH" else 0.2 if supplied == "BEARISH" else 0.5
                confidence = 0.5
                evidence.append("normalized_snapshot_direction_fallback")
            else:
                regime = "UNKNOWN"
                score = 0.5
                confidence = 0.0
        elif positive > negative and all(bias != "BEARISH" for bias in biases.values()):
            regime = "BULLISH"
            score = positive / valid
            confidence = abs(positive - negative) / valid
        elif negative > positive and all(bias != "BULLISH" for bias in biases.values()):
            regime = "BEARISH"
            score = positive / valid
            confidence = abs(positive - negative) / valid
        else:
            regime = "MIXED"
            score = positive / valid
            confidence = abs(positive - negative) / valid

        volatility = market.get("volatility_context")
        if volatility is None:
            unavailable.append("volatility_context")
        return MarketContext(
            regime=regime,
            spy_bias=biases.get("SPY", "UNKNOWN"),
            qqq_bias=biases.get("QQQ", "UNKNOWN"),
            volatility_context=str(volatility) if volatility is not None else None,
            confidence=round(max(0.0, min(1.0, confidence)), 3),
            score=round(max(0.0, min(1.0, score)), 3),
            evidence=tuple(evidence),
            unavailable_fields=tuple(unavailable),
        )
