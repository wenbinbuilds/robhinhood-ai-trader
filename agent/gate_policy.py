"""Explicit admission semantics for deterministic candidate gates.

This module classifies existing rules; it does not change their numeric
thresholds or score contributions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class GateClassification(str, Enum):
    DATA_VALIDITY_HARD = "DATA_VALIDITY_HARD"
    MARKET_ACCESS_HARD = "MARKET_ACCESS_HARD"
    LIQUIDITY_HARD = "LIQUIDITY_HARD"
    STRUCTURAL_TRADE_HARD = "STRUCTURAL_TRADE_HARD"
    RISK_HARD = "RISK_HARD"
    SLOW_SIGNAL_QUALITY = "SLOW_SIGNAL_QUALITY"
    LIVE_SIGNAL_QUALITY = "LIVE_SIGNAL_QUALITY"


TRUE_HARD_CLASSIFICATIONS = frozenset({
    GateClassification.DATA_VALIDITY_HARD.value,
    GateClassification.MARKET_ACCESS_HARD.value,
    GateClassification.LIQUIDITY_HARD.value,
    GateClassification.STRUCTURAL_TRADE_HARD.value,
    GateClassification.RISK_HARD.value,
})


@dataclass(frozen=True)
class GatePolicy:
    classification: GateClassification
    permanently_rejects: bool
    prevents_fast_watch: bool
    reevaluated_later: bool


TECHNICAL_GATE_POLICIES: Mapping[str, GatePolicy] = {
    "SYMBOL_PRESENT": GatePolicy(GateClassification.DATA_VALIDITY_HARD, True, True, False),
    "REQUIRED_FIELDS_PRESENT": GatePolicy(GateClassification.DATA_VALIDITY_HARD, True, True, True),
    "MINIMUM_CANDLES": GatePolicy(GateClassification.DATA_VALIDITY_HARD, True, True, True),
    "QUOTE_FRESHNESS": GatePolicy(GateClassification.LIQUIDITY_HARD, True, True, True),
    "PRICE_RANGE": GatePolicy(GateClassification.MARKET_ACCESS_HARD, True, True, True),
    "SPREAD": GatePolicy(GateClassification.LIQUIDITY_HARD, True, True, True),
    "PRICE_VS_VWAP": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "EMA_STRUCTURE": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "RSI": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "MACD": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "RELATIVE_VOLUME": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "CANDLE_STRUCTURE": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "MARKET_DIRECTION": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "MINIMUM_STRATEGY_SCORE": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "MAXIMUM_CONFLICTS": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "MINIMUM_CONFIDENCE": GatePolicy(GateClassification.SLOW_SIGNAL_QUALITY, False, False, True),
    "STOP_REFERENCE_AVAILABLE": GatePolicy(GateClassification.STRUCTURAL_TRADE_HARD, True, True, True),
    "MINIMUM_STOP_DISTANCE": GatePolicy(GateClassification.STRUCTURAL_TRADE_HARD, True, True, True),
    "RESISTANCE_ABOVE_ENTRY": GatePolicy(GateClassification.STRUCTURAL_TRADE_HARD, True, True, True),
    "MINIMUM_RISK_REWARD": GatePolicy(GateClassification.RISK_HARD, True, True, True),
}


RUNTIME_GATE_POLICIES: Mapping[str, GatePolicy] = {
    "REGULAR_MARKET_SESSION": GatePolicy(GateClassification.MARKET_ACCESS_HARD, True, True, True),
    "SUPPORTED_EQUITY": GatePolicy(GateClassification.MARKET_ACCESS_HARD, True, True, False),
    "LONG_ONLY": GatePolicy(GateClassification.MARKET_ACCESS_HARD, True, True, False),
    "CONTEXT_NOT_EXPIRED": GatePolicy(GateClassification.DATA_VALIDITY_HARD, True, True, True),
    "FAST_QUOTE_AVAILABLE": GatePolicy(GateClassification.DATA_VALIDITY_HARD, False, True, True),
    "FAST_QUOTE_FRESHNESS": GatePolicy(GateClassification.LIQUIDITY_HARD, False, True, True),
    "FAST_VALID_PRICE": GatePolicy(GateClassification.DATA_VALIDITY_HARD, False, True, True),
    "FAST_VALID_SPREAD": GatePolicy(GateClassification.LIQUIDITY_HARD, False, True, True),
    "FAST_STOP_GEOMETRY": GatePolicy(GateClassification.STRUCTURAL_TRADE_HARD, True, True, True),
    "FAST_TARGET_GEOMETRY": GatePolicy(GateClassification.STRUCTURAL_TRADE_HARD, True, True, True),
    "FAST_RISK_REWARD": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "DUPLICATE_POSITION": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "MAX_SIMULTANEOUS_POSITIONS": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "MAX_TRADES_PER_DAY": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "DAILY_LOSS_LIMIT": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "BUYING_POWER_AVAILABLE": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "MAX_POSITION_SIZE": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "MAX_RISK_PER_TRADE": GatePolicy(GateClassification.RISK_HARD, True, True, True),
    "MARKET_CLOSING": GatePolicy(GateClassification.MARKET_ACCESS_HARD, True, True, True),
    "LIVE_SPREAD_QUALITY": GatePolicy(GateClassification.LIVE_SIGNAL_QUALITY, False, False, True),
    "LIVE_PRICE_VS_VWAP": GatePolicy(GateClassification.LIVE_SIGNAL_QUALITY, False, False, True),
    "LIVE_PRICE_VS_EMA9": GatePolicy(GateClassification.LIVE_SIGNAL_QUALITY, False, False, True),
    "LIVE_SUPPORT_RESISTANCE_LOCATION": GatePolicy(GateClassification.LIVE_SIGNAL_QUALITY, False, False, True),
    "LIVE_CONTROLLED_MOMENTUM": GatePolicy(GateClassification.LIVE_SIGNAL_QUALITY, False, False, True),
}


def policy_for(rule_name: str) -> GatePolicy:
    """Fail closed when a future validation rule lacks an explicit policy."""

    return TECHNICAL_GATE_POLICIES.get(
        rule_name,
        GatePolicy(GateClassification.DATA_VALIDITY_HARD, True, True, False),
    )


def failed_gates(validation: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return (true-hard failures, signal-quality failures)."""

    hard: list[str] = []
    signal: list[str] = []
    rules = validation.get("rules", [])
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return ["TECHNICAL_VALIDATION_MALFORMED"], []
    for item in rules:
        if not isinstance(item, Mapping) or item.get("status") != "FAIL":
            continue
        name = str(item.get("rule_name") or "UNKNOWN_RULE")
        classification = str(
            item.get("classification") or policy_for(name).classification.value
        )
        if classification in TRUE_HARD_CLASSIFICATIONS:
            hard.append(name)
        elif classification in {
            GateClassification.SLOW_SIGNAL_QUALITY.value,
            GateClassification.LIVE_SIGNAL_QUALITY.value,
        }:
            signal.append(name)
        else:
            hard.append(name)
    return list(dict.fromkeys(hard)), list(dict.fromkeys(signal))
