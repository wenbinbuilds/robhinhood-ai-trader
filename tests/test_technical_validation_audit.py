"""Diagnostic-only coverage of every existing deterministic technical gate."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

import config
from agent.candidate_analyzer import CandidateData
from agent.technical_agent import TechnicalAgent
from test_market_cycle import NOW, bullish_candidate


def assess(**changes):
    row = {"symbol": "ACME", **deepcopy(bullish_candidate()), **changes}
    return TechnicalAgent().analyze(CandidateData.from_mapping(row), now=NOW)


def rules(result):
    return {
        item["rule_name"]: item
        for item in result.metrics["technical_validation"]["rules"]
    }


def test_all_hard_gates_pass_and_setup_is_valid():
    result = assess()
    audit = result.metrics["technical_validation"]
    assert result.candidate_plan["technical_disposition"] == "QUALIFIED"
    assert audit["failed_hard_gates"] == []
    assert audit["hard_gates_passed"] == audit["hard_gates_evaluated"]


@pytest.mark.parametrize(("changes", "gate"), [
    ({"symbol": ""}, "SYMBOL_PRESENT"),
    ({"current_price": None}, "REQUIRED_FIELDS_PRESENT"),
    ({"candles": bullish_candidate()["candles"][-3:]}, "MINIMUM_CANDLES"),
    ({"quote_as_of": (NOW - timedelta(seconds=config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS + 1)).isoformat()}, "QUOTE_FRESHNESS"),
    ({"current_price": 9.99}, "PRICE_RANGE"),
    ({"bid": 100.0, "ask": 101.0}, "SPREAD"),
])
def test_each_pre_score_hard_gate_rejects(changes, gate):
    result = assess(**changes)
    assert rules(result)[gate]["status"] == "FAIL"
    assert result.candidate_plan["technical_disposition"] == "REJECTED"


def test_aggregate_score_conflict_and_confidence_gates_are_visible():
    low = assess(current_price=99.0, ema9=98.0, ema20=100.0, rsi14=20.0,
                 macd=-0.5, macd_signal=0.1, relative_volume=0.5)
    low_rules = rules(low)
    assert low_rules["MINIMUM_STRATEGY_SCORE"]["status"] == "FAIL"
    assert low_rules["MAXIMUM_CONFLICTS"]["status"] == "FAIL"

    # 4.5 / 6.5 rounds to 0.692, but two conflicts reduce confidence below .65.
    near = assess(current_price=100.0, rsi14=94.0, market_direction="BULLISH")
    near_rules = rules(near)
    assert near.context.technical_score == 0.692
    assert near_rules["MINIMUM_STRATEGY_SCORE"]["status"] == "PASS"
    assert near_rules["MAXIMUM_CONFLICTS"]["status"] == "PASS"
    assert near_rules["MINIMUM_CONFIDENCE"]["status"] == "FAIL"
    assert near.candidate_plan["technical_disposition"] == "MONITORABLE"
    assert near_rules["MINIMUM_CONFIDENCE"]["classification"] == "SLOW_SIGNAL_QUALITY"
    assert near.metrics["technical_validation"]["true_hard_gate_failures"] == []


def test_stop_reference_gate():
    result = assess(current_price=101.0, ask=101.02, vwap=101.5, ema9=103.0,
                    ema20=102.0, intraday_low=102.0,
                    intraday_support_reference=102.0)
    assert rules(result)["STOP_REFERENCE_AVAILABLE"]["status"] == "FAIL"


def test_minimum_stop_distance_gate_uses_decimal_ratio_units():
    result = assess(intraday_support_reference=101.0, intraday_low=99.0,
                    ema20=99.5, vwap=100.2)
    gate = rules(result)["MINIMUM_STOP_DISTANCE"]
    assert gate["status"] == "FAIL"
    assert gate["actual"] < 0.002


def test_resistance_above_entry_gate():
    result = assess(intraday_resistance_reference=101.0)
    assert rules(result)["RESISTANCE_ABOVE_ENTRY"]["status"] == "FAIL"


def test_minimum_risk_reward_gate():
    result = assess(intraday_support_reference=99.0,
                    intraday_resistance_reference=101.5)
    gate = rules(result)["MINIMUM_RISK_REWARD"]
    assert gate["status"] == "FAIL"
    assert gate["actual"] < config.MIN_RISK_REWARD_RATIO


def test_soft_factor_ordering_sign_and_units_are_exact():
    assert rules(assess(current_price=100.21))["PRICE_VS_VWAP"]["status"] == "PASS"
    assert rules(assess(current_price=100.20))["PRICE_VS_VWAP"]["status"] == "FAIL"
    assert rules(assess(ema9=100.6, ema20=100.5))["EMA_STRUCTURE"]["status"] == "PASS"
    assert rules(assess(ema9=100.5, ema20=100.5))["EMA_STRUCTURE"]["status"] == "FAIL"
    assert rules(assess(macd=0.5, macd_signal=0.4, macd_histogram=0.01))["MACD"]["status"] == "PASS"
    assert rules(assess(macd=0.5, macd_signal=0.4, macd_histogram=-0.01))["MACD"]["status"] == "FAIL"
    assert rules(assess(relative_volume=1.2))["RELATIVE_VOLUME"]["status"] == "PASS"
    assert rules(assess(relative_volume=1.19))["RELATIVE_VOLUME"]["status"] == "FAIL"


def test_rsi_boundaries_and_partial_score_are_exact():
    assert rules(assess(rsi14=50.0))["RSI"]["status"] == "FAIL"
    assert rules(assess(rsi14=50.01))["RSI"]["score_contribution"] == 1.0
    assert rules(assess(rsi14=70.0))["RSI"]["score_contribution"] == 0.5
    assert rules(assess(rsi14=75.0))["RSI"]["score_contribution"] == 0.5
    assert rules(assess(rsi14=75.01))["RSI"]["status"] == "FAIL"


def test_spread_percentage_boundary_uses_decimal_ratio():
    midpoint = 100.0
    half = config.MAX_SPREAD_PERCENT * midpoint / 2
    assert rules(assess(bid=midpoint-half, ask=midpoint+half))["SPREAD"]["status"] == "PASS"
    assert rules(assess(bid=midpoint-half-0.001, ask=midpoint+half+0.001))["SPREAD"]["status"] == "FAIL"


@pytest.mark.parametrize("value", [None, float("nan")])
def test_none_and_nan_are_missing_not_false_positive(value):
    result = assess(rsi14=value)
    assert rules(result)["REQUIRED_FIELDS_PRESENT"]["status"] == "FAIL"
    assert result.candidate_plan["technical_disposition"] == "REJECTED"


def test_completed_and_forming_candles_are_diagnosed_without_rule_change():
    completed = assess().metrics["technical_validation"]
    assert completed["completed_candles"] == 6
    assert completed["forming_candles"] == 0
    candles = deepcopy(bullish_candidate()["candles"])
    candles[-1]["begins_at"] = (NOW - timedelta(minutes=2)).isoformat()
    forming = assess(candles=candles).metrics["technical_validation"]
    assert forming["forming_candles"] == 1
    assert forming["forming_candle_used"] is False
    assert forming["candle_policy"] == "COMPLETED_NON_INTERPOLATED_5_MINUTE_BARS"
