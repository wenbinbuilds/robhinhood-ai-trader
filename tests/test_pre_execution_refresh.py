"""Deterministic pre-entry refresh regressions; no broker order operations."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from execution.pre_execution import PreExecutionRiskContext, PreExecutionValidator
from test_market_cycle import bullish_candidate


NOW = datetime(2026, 9, 9, 15, 2, tzinfo=timezone.utc)


def coordinator():
    return {
        "symbol": "ACME", "decision": "TRADE_CANDIDATE",
        "entry": 99.0, "stop": 97.0, "target": 103.0,
        "combined_score": .8, "technical_score": .9,
        "news_score": .6, "sector_score": .6, "market_score": .8,
        "confidence": .8, "setup_name": "INTRADAY_MOMENTUM_V1",
        "thesis": "pre-LLM thesis", "invalidation_condition": "old stop",
    }


def risk():
    return PreExecutionRiskContext(
        account_equity=100_000, available_buying_power=20_000,
        daily_realized_pnl=0, open_positions=0, trades_today=0,
    )


def fresh(**updates):
    value = deepcopy(bullish_candidate())
    value.update(symbol="ACME", quote_as_of=NOW.isoformat())
    value.update(updates)
    return value


class Refresher:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value, error, []

    def refresh_symbol(self, symbol, *, now):
        self.calls.append(symbol)
        if self.error:
            raise self.error
        return self.value


def test_stale_before_refresh_is_rebuilt_from_selected_symbol_only():
    provider = Refresher(fresh(current_price=102.0, bid=101.98, ask=102.02))
    result = PreExecutionValidator().evaluate(
        coordinator(), provider, risk(), now=NOW
    )
    assert result.approved
    assert provider.calls == ["ACME"]
    assert result.plan.market_data_timestamp == NOW.isoformat()
    assert result.plan.entry_price == 102.02
    assert result.plan.entry_price != coordinator()["entry"]
    assert result.plan.stop_price != coordinator()["stop"]
    assert result.plan.quantity == result.risk_result["max_shares"]


def test_successful_refresh_logs_recalculated_fields():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(fresh()), risk(), now=NOW
    )
    assert result.approved
    assert result.log_record["event"] == "PRE_EXECUTION_REFRESH"
    assert result.log_record["refreshed_quote_age_seconds"] == 0
    assert result.log_record["refreshed_risk_reward_ratio"] == pytest.approx(
        result.plan.risk_reward_ratio, abs=.01
    )
    assert result.log_record["refreshed_spread_percent"] is not None
    assert result.log_record["refreshed_price_vs_vwap"] == "ABOVE"
    assert result.log_record["hard_gate_results"]
    assert result.log_record["rejection_reason"] is None


def test_stale_after_refresh_is_rejected_by_exchange_timestamp():
    result = PreExecutionValidator().evaluate(
        coordinator(),
        Refresher(fresh(quote_as_of=(NOW - timedelta(seconds=16)).isoformat())),
        risk(), now=NOW,
    )
    assert not result.approved
    assert result.reason == "REFRESHED_QUOTE_STALE"
    assert result.quote_age_seconds == 16
    assert result.log_record["rejection_reason"] == "REFRESHED_QUOTE_STALE"


def test_failed_refresh_is_rejected_with_exact_reason():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(error=TimeoutError()), risk(), now=NOW
    )
    assert not result.approved
    assert result.reason == "PRE_EXECUTION_REFRESH_FAILED:TimeoutError"


def test_hard_gate_failure_after_refresh_blocks_entry():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(fresh(bid=100.0, ask=102.0)), risk(), now=NOW
    )
    assert not result.approved
    assert result.reason == "REFRESHED_HARD_GATE_FAILED:SPREAD"
    # Geometry remains observable even when an earlier hard gate rejects the
    # refreshed setup; it is diagnostic evidence, never execution approval.
    assert result.log_record["refreshed_risk_reward_ratio"] == pytest.approx(1.6666667)
    assert result.log_record["refreshed_risk_distance"] == pytest.approx(1.8)
    assert result.log_record["refreshed_reward_distance"] == pytest.approx(3.0)
    failed = [
        row["rule_name"] for row in result.log_record["hard_gate_results"]
        if row["status"] == "FAIL"
    ]
    assert "SPREAD" in failed


def partial_quote(**updates):
    value = {
        "symbol": "ACME", "current_price": 101.0,
        "bid": 100.98, "ask": 101.02, "quote_as_of": NOW.isoformat(),
    }
    value.update(updates)
    return value


def test_partial_refresh_retains_complete_canonical_slow_context():
    canonical = fresh()
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(partial_quote()), risk(), now=NOW,
        canonical_context=canonical,
    )
    assert result.approved
    assert result.market_data["relative_volume"] == 1.8
    assert result.market_data["candles"] == canonical["candles"]
    assert result.log_record["required_fields_status"] == "PASS"
    assert result.log_record["missing_required_fields"] == []


def test_none_from_partial_refresh_does_not_erase_slow_value():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(partial_quote(relative_volume=None)), risk(),
        now=NOW, canonical_context=fresh(relative_volume=1.8),
    )
    assert result.approved
    assert result.market_data["relative_volume"] == 1.8


def test_missing_fresh_quote_fact_never_falls_back_to_stale_slow_quote():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(partial_quote(bid=None)), risk(),
        now=NOW, canonical_context=fresh(bid=100.98),
    )
    assert not result.approved
    assert result.reason == "REFRESHED_HARD_GATE_FAILED:REQUIRED_FIELDS_PRESENT,SPREAD"
    assert result.market_data["bid"] is None
    assert result.log_record["missing_required_fields"] == ["bid"]


def test_explicit_refreshed_value_replaces_slow_value():
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(partial_quote(relative_volume=2.4)), risk(),
        now=NOW, canonical_context=fresh(relative_volume=1.8),
    )
    assert result.approved
    assert result.market_data["relative_volume"] == 2.4


def test_truly_missing_required_field_still_fails_with_exact_diagnostic():
    canonical = fresh()
    canonical.pop("relative_volume")
    result = PreExecutionValidator().evaluate(
        coordinator(), Refresher(partial_quote(relative_volume=None)), risk(),
        now=NOW, canonical_context=canonical,
    )
    assert not result.approved
    assert result.reason == "REFRESHED_HARD_GATE_FAILED:REQUIRED_FIELDS_PRESENT"
    assert result.log_record["required_fields_status"] == "FAIL"
    assert result.log_record["missing_required_fields"] == ["relative_volume"]


def test_required_fields_pass_while_minimum_stop_distance_independently_fails():
    canonical = fresh(
        vwap=100.0, ema20=99.5, intraday_support_reference=98.5,
        intraday_resistance_reference=105.0,
    )
    result = PreExecutionValidator().evaluate(
        coordinator(),
        Refresher(partial_quote(current_price=100.18, bid=100.17, ask=100.19)),
        risk(), now=NOW, canonical_context=canonical,
    )
    assert not result.approved
    assert result.reason == "REFRESHED_HARD_GATE_FAILED:MINIMUM_STOP_DISTANCE"
    assert result.log_record["required_fields_status"] == "PASS"
    assert result.log_record["missing_required_fields"] == []


def test_required_fields_pass_while_minimum_risk_reward_independently_fails():
    canonical = fresh(intraday_resistance_reference=103.5)
    result = PreExecutionValidator().evaluate(
        coordinator(),
        Refresher(partial_quote(current_price=103.0, bid=102.99, ask=103.01)),
        risk(), now=NOW, canonical_context=canonical,
    )
    assert not result.approved
    assert result.reason == "REFRESHED_HARD_GATE_FAILED:MINIMUM_RISK_REWARD"
    assert result.log_record["required_fields_status"] == "PASS"
