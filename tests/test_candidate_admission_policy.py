"""Admission-policy integration tests; all market/model/broker inputs are local."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json

import pytest

import config
from agent.candidate_analyzer import CandidateData
from agent.candidate_context import CandidateContextStore, contexts_from_cycle
from agent.coordinator import CoordinatorAgent
from agent.gate_policy import (
    GateClassification,
    TECHNICAL_GATE_POLICIES,
    TRUE_HARD_CLASSIFICATIONS,
)
from agent.technical_agent import TechnicalAgent
from event_driven.orchestrator import ShadowEventOrchestrator
from event_driven.state import CandidateState
from shadow.execution import ShadowExecutionEngine
from shadow.portfolio import ShadowPortfolio
from watcher.candidate_watcher import FastCandidateWatcher, LiveScore
from watcher.models import FastQuote
from test_coordinator import inputs, llm_analysis
from test_market_cycle import NOW, bullish_candidate


def mrna_like_assessment():
    row = {"symbol": "MRNA", **deepcopy(bullish_candidate())}
    row.update(
        macd=0.2, macd_signal=0.45, macd_histogram=-0.1,
        market_direction="BULLISH",
    )
    for candle in row["candles"]:
        candle["close"] = candle["open"] - 0.05
    return TechnicalAgent().analyze(CandidateData.from_mapping(row), now=NOW)


def supportive_decision(technical):
    market, news, _unused, sector = inputs()
    return CoordinatorAgent().decide(
        "MRNA", market=market, news=news, technical=technical, sector=sector,
        llm_analysis=llm_analysis(
            setup_quality=.9, catalyst_quality=.9, continuation=.9, conflict=0,
        ),
    )


def cycle_result(technical, decision):
    plan = dict(technical.candidate_plan)
    return {
        "generated_at": NOW.isoformat(),
        "analysis_started_at": NOW.isoformat(),
        "market_context": {"effective_regular_session": True},
        "llm_reasoning": {"status": "AVAILABLE", "trace": {"status": "SUCCESS"}},
        "scanner_candidates": [{"symbol": "MRNA", "instrument_type": "EQUITY"}],
        "analyzed_candidates": [{
            **plan,
            "symbol": "MRNA",
            "deterministic_technical_metrics": dict(technical.metrics),
            "technical_context": technical.context.to_dict(),
            "market_context": {"regime": "BULLISH"},
            "sector_context": {"sector": "HEALTHCARE"},
            "news_context": {"score": decision.news_score},
            "llm_analysis": {
                "qualitative_analysis": {
                    "summary": "supportive fixture", "reasons_against": [],
                },
                "news_analysis": {"catalyst_type": "EARNINGS"},
            },
            "coordinator_decision": decision.to_dict(),
        }],
    }


def test_every_technical_gate_has_exact_admission_classification():
    expected = {
        "SYMBOL_PRESENT", "REQUIRED_FIELDS_PRESENT", "MINIMUM_CANDLES",
        "QUOTE_FRESHNESS", "PRICE_RANGE", "SPREAD", "PRICE_VS_VWAP",
        "EMA_STRUCTURE", "RSI", "MACD", "RELATIVE_VOLUME",
        "CANDLE_STRUCTURE", "MARKET_DIRECTION", "MINIMUM_STRATEGY_SCORE",
        "MAXIMUM_CONFLICTS", "MINIMUM_CONFIDENCE", "STOP_REFERENCE_AVAILABLE",
        "MINIMUM_STOP_DISTANCE", "RESISTANCE_ABOVE_ENTRY",
        "MINIMUM_RISK_REWARD",
    }
    assert set(TECHNICAL_GATE_POLICIES) == expected
    assert all(isinstance(item.classification, GateClassification)
               for item in TECHNICAL_GATE_POLICIES.values())


def test_mrna_like_low_confidence_is_monitorable_and_enters_setup_forming(tmp_path):
    technical = mrna_like_assessment()
    validation = technical.metrics["technical_validation"]
    assert technical.context.technical_score == .692
    assert technical.context.confidence < config.MIN_CANDIDATE_CONFIDENCE
    assert technical.candidate_plan["technical_disposition"] == "MONITORABLE"
    assert validation["true_hard_gate_failures"] == []
    assert validation["signal_quality_failures"] == [
        "MACD", "CANDLE_STRUCTURE", "MINIMUM_CONFIDENCE",
    ]

    decision = supportive_decision(technical)
    assert decision.combined_score > config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD
    assert decision.vetoes == ()
    contexts = contexts_from_cycle(cycle_result(technical, decision), now=NOW)
    assert len(contexts) == 1
    assert contexts[0].signal_quality_failures == validation["signal_quality_failures"]

    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    runtime.ingest_slow_cycle(cycle_result(technical, decision), contexts, now=NOW)
    state = runtime.state_store.get("MRNA")
    assert state.state == CandidateState.SETUP_FORMING
    assert state.eligible_for_fast_watch is True
    assert state.admission_reason == "FAST_WATCH_ADMITTED_WITH_SIGNAL_QUALITY_WARNINGS"
    assert state.true_hard_gate_failures == []
    assert state.signal_quality_failures == validation["signal_quality_failures"]
    runtime.bus.drain()
    alpha = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "ALPHA_UPDATED"
    ][-1]
    assert alpha["candidate_state"] == "SETUP_FORMING"
    assert alpha["technical_confidence"] == .572
    assert alpha["true_hard_gate_failures"] == []
    assert alpha["signal_quality_failures"] == [
        "MACD", "CANDLE_STRUCTURE", "MINIMUM_CONFIDENCE",
    ]


def test_aggregate_quality_rules_warn_but_do_not_permanently_reject():
    row = {"symbol": "WEAK", **deepcopy(bullish_candidate())}
    row.update(current_price=99.0, ema9=98.0, ema20=100.0, rsi14=20.0,
               macd=-.5, macd_signal=.1, relative_volume=.5)
    technical = TechnicalAgent().analyze(CandidateData.from_mapping(row), now=NOW)
    validation = technical.metrics["technical_validation"]
    assert technical.candidate_plan["technical_disposition"] == "MONITORABLE"
    assert validation["true_hard_gate_failures"] == []
    assert {"MINIMUM_STRATEGY_SCORE", "MAXIMUM_CONFLICTS", "MINIMUM_CONFIDENCE"}.issubset(
        validation["signal_quality_failures"]
    )
    for name in ("MINIMUM_STRATEGY_SCORE", "MAXIMUM_CONFLICTS", "MINIMUM_CONFIDENCE"):
        policy = TECHNICAL_GATE_POLICIES[name]
        assert policy.classification == GateClassification.SLOW_SIGNAL_QUALITY
        assert policy.permanently_rejects is False


@pytest.mark.parametrize(("changes", "failure"), [
    ({"quote_as_of": "2026-09-09T14:00:00Z"}, "QUOTE_FRESHNESS"),
    ({"bid": 100.0, "ask": 101.0}, "SPREAD"),
    ({"intraday_support_reference": 102.0, "intraday_low": 102.0,
      "ema20": 102.0, "vwap": 102.0}, "STOP_REFERENCE_AVAILABLE"),
    ({"intraday_resistance_reference": 100.0}, "RESISTANCE_ABOVE_ENTRY"),
    ({"intraday_support_reference": 99.0,
      "intraday_resistance_reference": 101.5}, "MINIMUM_RISK_REWARD"),
])
def test_true_hard_failures_remain_terminal(changes, failure):
    row = {"symbol": "BLOCKED", **deepcopy(bullish_candidate()), **changes}
    technical = TechnicalAgent().analyze(CandidateData.from_mapping(row), now=NOW)
    validation = technical.metrics["technical_validation"]
    assert failure in validation["true_hard_gate_failures"]
    assert TECHNICAL_GATE_POLICIES[failure].classification.value in TRUE_HARD_CLASSIFICATIONS
    assert technical.candidate_plan["technical_disposition"] == "REJECTED"


def test_signal_warning_is_not_applied_twice_to_slow_alpha():
    technical = mrna_like_assessment()
    decision = supportive_decision(technical)
    expected = (
        technical.context.technical_score * config.COORDINATOR_WEIGHTS["technical"]
        + decision.news_score * config.COORDINATOR_WEIGHTS["news"]
        + decision.sector_score * config.COORDINATOR_WEIGHTS["sector"]
        + decision.market_score * config.COORDINATOR_WEIGHTS["market"]
        + decision.qualitative_score * config.COORDINATOR_WEIGHTS["qualitative"]
    )
    assert decision.combined_score == round(expected, 3)
    assert decision.signal_quality_failures == (
        "MACD", "CANDLE_STRUCTURE", "MINIMUM_CONFIDENCE",
    )
    assert decision.vetoes == ()


class SequenceScorer:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def score(self, context, quote):
        self.calls += 1
        value = next(self.values)
        return LiveScore(value, {"fixture": {"value": value, "weight": 1}})


def test_warned_candidate_can_improve_or_decline_with_hysteresis(tmp_path):
    technical = mrna_like_assessment()
    decision = supportive_decision(technical)
    contexts = contexts_from_cycle(cycle_result(technical, decision), now=NOW)
    cache = CandidateContextStore(tmp_path / "watchlist.json")
    cache.replace(contexts, now=NOW)
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    scorer = SequenceScorer([.2, .95, .2, .95, .95])
    watcher = FastCandidateWatcher(
        cache, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "events.jsonl",
        score_history_path=tmp_path / "scores.jsonl", scorer=scorer,
    )
    observed = []
    for seconds in (1, 3, 5, 7):
        at = NOW.replace(microsecond=0) + timedelta(seconds=seconds)
        watcher.process_quotes(
            {"MRNA": FastQuote("MRNA", 100.98, 101.02, 101.0, at, "MOCK", True)},
            now=at,
        )
        observed.append(cache.snapshot()[0].dynamic_score)
    assert observed[1] > observed[0]
    assert observed[2] < observed[1]
    assert portfolio.snapshot().open_positions == []  # confirmation reset on decline

    at = NOW.replace(microsecond=0) + timedelta(seconds=9)
    watcher.process_quotes(
        {"MRNA": FastQuote("MRNA", 100.98, 101.02, 101.0, at, "MOCK", True)},
        now=at,
    )
    assert len(portfolio.snapshot().open_positions) == 1
    assert scorer.calls == 5


def test_warned_candidate_cannot_bypass_daily_loss_limit(tmp_path):
    technical = mrna_like_assessment()
    decision = supportive_decision(technical)
    contexts = contexts_from_cycle(cycle_result(technical, decision), now=NOW)
    cache = CandidateContextStore(tmp_path / "watchlist.json")
    cache.replace(contexts, now=NOW)
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    portfolio.state.daily_pnl = (
        -portfolio.state.starting_capital * config.MAX_DAILY_LOSS_PERCENT
    )
    watcher = FastCandidateWatcher(
        cache, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=SequenceScorer([1, 1]),
    )
    for seconds in (1, 3):
        at = NOW + timedelta(seconds=seconds)
        watcher.process_quotes(
            {"MRNA": FastQuote("MRNA", 100.98, 101.02, 101.0, at, "MOCK", True)},
            now=at,
        )
    assert portfolio.snapshot().open_positions == []
    assert cache.snapshot()[0].candidate_state == "REJECTED"
    assert "DAILY_LOSS_LIMIT" in (tmp_path / "events.jsonl").read_text()
