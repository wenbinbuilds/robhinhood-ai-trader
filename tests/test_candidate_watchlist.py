"""Local-only two-speed shadow-entry tests; no external service is contacted."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread

import pytest
from copy import deepcopy

import config
from agent.candidate_context import CandidateContext, CandidateContextStore, contexts_from_cycle
from agent.candidate_analyzer import Candle, completed_candles
from shadow.execution import ShadowExecutionEngine
from shadow.portfolio import ShadowPortfolio
from watcher.candidate_watcher import FastCandidateWatcher, LiveMarketScorer, LiveScore, dynamic_weights
from watcher.models import FastQuote
from watcher.status import shadow_dashboard_projection
from test_market_cycle import bullish_candidate


NOW = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)


class FreshRefresher:
    def refresh_symbol(self, symbol, *, now):
        value = deepcopy(bullish_candidate())
        value.update(symbol=symbol, quote_as_of=now.isoformat(), market_direction="BULLISH")
        return value


def context(*, slow=0.66, stop=99.0, target=105.0, at=NOW):
    return CandidateContext(
        symbol="ACME", research_cycle_id="cycle-1", research_timestamp=at.isoformat(),
        analysis_price=101.0, slow_context_score=slow, technical_context_score=.8,
        qualitative_score=.7, news_score=.6, sector_score=.6, market_score=.7,
        llm_thesis="concise thesis", llm_conflicts=[], catalyst_classification="NONE",
        slow_technical_evidence=["ema9_above_ema20"], slow_technical_conflicts=[],
        slow_hard_gate_status={"QUOTE_FRESHNESS": "PASS"},
        suggested_stop_reference=stop, suggested_target_reference=target,
        invalidation_condition="price below stop", expiration_timestamp=(at + timedelta(seconds=300)).isoformat(),
        research_vwap=100.5, research_ema9=100.75,
        intraday_support_reference=stop, intraday_resistance_reference=target,
        research_bid=100.99, research_ask=101.01, risk_reward_ratio=2.0,
        coordinator_confidence=.75,
        slow_score_formula="0.35*technical + 0.20*news + 0.10*sector + 0.10*market + 0.25*qualitative",
    )


def quote(at=NOW, price=101.0, bid=100.99, ask=101.01, open_=True):
    return FastQuote("ACME", bid, ask, price, at, "MOCK_FAST", open_)


def watcher(tmp_path, ctx=None, scorer=None):
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    store = CandidateContextStore(tmp_path / "watchlist.json")
    store.replace([ctx or context()], now=NOW)
    value = FastCandidateWatcher(
        store, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl", scorer=scorer,
        pre_execution_refresher=FreshRefresher(),
    )
    return value, store, portfolio


def cycle_result(score=.66):
    return {
        "generated_at": NOW.isoformat(),
        "market_context": {"effective_regular_session": True},
        "llm_reasoning": {"status": "AVAILABLE"},
        "analyzed_candidates": [{
            "symbol": "ACME", "technical_disposition": "QUALIFIED",
            "stop": 99.0, "target": 105.0, "risk_reward_ratio": 2.0,
            "invalidation_condition": "below stop",
            "supporting_indicators": {"intraday_support_reference": 99, "intraday_resistance_reference": 105},
            "technical_context": {"technical_score": .8, "evidence": ["trend"], "conflicts": []},
            "market_context": {"regime": "BULLISH"}, "sector_context": {"sector": "TECH"},
            "deterministic_technical_metrics": {
                "technical_data_valid": True, "current_price": 101.0, "bid": 100.99,
                "ask": 101.01, "vwap": 100.5, "ema9": 100.75,
                "technical_validation": {"rules": [{"rule_name": "QUOTE_FRESHNESS", "type": "HARD", "status": "PASS"}]},
            },
            "llm_analysis": {
                "qualitative_analysis": {"summary": "thesis", "reasons_against": []},
                "news_analysis": {"catalyst_type": "NONE"},
            },
            "coordinator_decision": {
                "combined_score": score, "technical_score": .8, "qualitative_score": .7,
                "news_score": .6, "sector_score": .6, "market_score": .7,
                "confidence": .75, "vetoes": [],
            },
        }],
    }


def test_slow_context_creation_admission_and_atomic_persistence(tmp_path):
    contexts = contexts_from_cycle(cycle_result(), now=NOW)
    assert len(contexts) == 1 and contexts[0].slow_context_score == .66
    assert contexts[0].expiration_timestamp == (NOW + timedelta(seconds=300)).isoformat()
    store = CandidateContextStore(tmp_path / "watchlist.json")
    store.replace(contexts, now=NOW)
    restored = CandidateContextStore(store.path).snapshot()
    assert restored[0].symbol == "ACME"
    assert not list(tmp_path.glob(".watchlist.json.*"))
    assert contexts_from_cycle(cycle_result(.59), now=NOW) == []


def test_cached_reasoning_does_not_reset_context_ttl(tmp_path):
    store = CandidateContextStore(tmp_path / "watchlist.json")
    original = context(at=NOW)
    store.replace([original], now=NOW)
    later = NOW + timedelta(seconds=200)
    refreshed = context(at=later)
    store.replace([refreshed], now=later, preserve_research=True)
    restored = store.snapshot()[0]
    assert restored.research_timestamp == NOW.isoformat()
    assert restored.expiration_timestamp == (NOW + timedelta(seconds=300)).isoformat()
    assert restored.expired_at(NOW + timedelta(seconds=300))


def test_dynamic_weight_interpolation_and_expiration():
    start = dynamic_weights(0)
    middle = dynamic_weights(150)
    end = dynamic_weights(300)
    assert start == pytest.approx((.70, .30))
    assert middle == pytest.approx((.60, .40))
    assert end == pytest.approx((.50, .50))
    assert all(sum(dynamic_weights(age)) == pytest.approx(1) for age in (0, 1, 150, 299, 300, 900))
    assert start[0] > middle[0] > end[0]
    assert start[1] < middle[1] < end[1]
    assert context().expired_at(NOW + timedelta(seconds=300))


def test_live_score_is_normalized_documented_and_not_price_dominated():
    scorer = LiveMarketScorer()
    result = scorer.score(context(), quote())
    assert 0 <= result.score <= 1
    assert sum(scorer.weights.values()) == pytest.approx(1)
    assert scorer.weights["controlled_momentum"] == .15
    assert result.factors["spread_quality"]["value"] > 0


class SequenceScorer:
    def __init__(self, values): self.values = iter(values); self.calls = 0
    def score(self, context, quote):
        self.calls += 1
        return LiveScore(next(self.values), {"fixture": {"weight": 1, "value": 1, "observed": None}})


def test_simulated_dynamic_crossing_hysteresis_entry_and_handoff(tmp_path):
    scorer = SequenceScorer([.55, .70, .84, .84])
    candidate, store, portfolio = watcher(tmp_path, scorer=scorer)
    for seconds in (60, 120):
        now = NOW + timedelta(seconds=seconds)
        candidate.process_quotes({"ACME": quote(now)}, now=now)
        assert not portfolio.snapshot().open_positions
    crossing = NOW + timedelta(seconds=180)
    candidate.process_quotes({"ACME": quote(crossing)}, now=crossing)
    assert store.snapshot()[0].status == "PENDING_CONFIRMATION"
    assert not portfolio.snapshot().open_positions
    candidate.process_quotes({"ACME": quote(crossing + timedelta(seconds=2))}, now=crossing + timedelta(seconds=2))
    positions = portfolio.snapshot().open_positions
    assert len(positions) == 1
    assert store.snapshot() == []
    assert positions[0].dynamic_score >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD
    assert positions[0].slow_context_score == .66
    assert scorer.calls == 4  # no second slow/LLM pass occurred


class PartialRefresh:
    def __init__(self, *, price=101.0, bid=100.99, ask=101.01):
        self.price, self.bid, self.ask = price, bid, ask
        self.calls = []

    def refresh_symbol(self, symbol, *, now):
        self.calls.append(symbol)
        return {
            "symbol": symbol, "current_price": self.price,
            "bid": self.bid, "ask": self.ask,
            "quote_as_of": now.isoformat(), "relative_volume": None,
        }


def wal_context(*, stop=99.0):
    item = context(slow=.730, stop=stop, target=105.0)
    item.canonical_market_data = deepcopy(bullish_candidate())
    item.canonical_market_data.update(
        symbol="ACME", quote_as_of=NOW.isoformat(),
        market_direction="BULLISH", relative_volume=1.8,
    )
    item.consecutive_qualifying_updates = 1
    return item


def test_wal_regression_partial_refresh_preserves_required_fields_and_enters(tmp_path):
    item = wal_context()
    store = CandidateContextStore(tmp_path / "watchlist.json")
    store.replace([item], now=NOW)
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    refresher = PartialRefresh()
    candidate = FastCandidateWatcher(
        store, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=SequenceScorer([.870171]), pre_execution_refresher=refresher,
    )
    at = NOW + timedelta(seconds=95)
    transitions = candidate.process_quotes({"ACME": quote(at)}, now=at)
    assert transitions[0]["final"] == "SHADOW_POSITION"
    assert transitions[0]["dynamic_score"] == pytest.approx(.7809, abs=.001)
    assert refresher.calls == ["ACME"]
    log = (tmp_path / "candidate_events.jsonl").read_text()
    assert '"required_fields_status": "PASS"' in log
    assert '"missing_required_fields": []' in log
    assert len(portfolio.snapshot().open_positions) == 1


def test_wal_regression_true_stop_distance_failure_remains_blocking(tmp_path):
    item = wal_context()
    item.canonical_market_data.update(
        vwap=100.0, ema20=99.5, intraday_support_reference=98.5,
    )
    store = CandidateContextStore(tmp_path / "watchlist.json")
    store.replace([item], now=NOW)
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    candidate = FastCandidateWatcher(
        store, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=SequenceScorer([.870171]),
        pre_execution_refresher=PartialRefresh(
            price=100.18, bid=100.17, ask=100.19,
        ),
    )
    at = NOW + timedelta(seconds=95)
    transitions = candidate.process_quotes(
        {"ACME": quote(at, price=100.18, bid=100.17, ask=100.19)}, now=at,
    )
    assert transitions[0]["reason"] == (
        "REFRESHED_HARD_GATE_FAILED:MINIMUM_STOP_DISTANCE"
    )
    assert not portfolio.snapshot().open_positions
    log = (tmp_path / "candidate_events.jsonl").read_text()
    assert '"required_fields_status": "PASS"' in log
    assert '"missing_required_fields": []' in log


def test_duplicate_entry_is_prevented_and_candidate_hands_off(tmp_path):
    scorer = SequenceScorer([1, 1])
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), scorer)
    candidate.process_quotes({"ACME": quote()}, now=NOW)
    candidate.process_quotes({"ACME": quote(NOW + timedelta(seconds=1))}, now=NOW + timedelta(seconds=1))
    assert len(portfolio.snapshot().open_positions) == 1
    assert candidate.symbols(NOW + timedelta(seconds=2)) == []


@pytest.mark.parametrize(("ctx", "value", "now", "reason"), [
    (context(), quote(NOW - timedelta(seconds=6)), NOW, "QUOTE_STALE"),
    (context(), quote(NOW, bid=100, ask=101), NOW, "SPREAD_TOO_WIDE"),
    (context(), quote(NOW + timedelta(seconds=301)), NOW + timedelta(seconds=301), "CONTEXT_EXPIRED"),
])
def test_hard_entry_blockers(ctx, value, now, reason, tmp_path):
    candidate, store, portfolio = watcher(tmp_path, ctx, SequenceScorer([1, 1]))
    if reason == "CONTEXT_EXPIRED":
        assert candidate.symbols(now) == []
        assert store.snapshot()[0].status == "CONTEXT_EXPIRED"
    else:
        assert candidate._hard_blocker(ctx, value, now) == reason
        candidate.process_quotes({"ACME": value}, now=now)
    assert not portfolio.snapshot().open_positions


def test_price_spike_cannot_override_bad_risk_reward(tmp_path):
    candidate, _, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1, 1]))
    spike = quote(price=104.9, bid=104.89, ask=104.91)
    candidate.process_quotes({"ACME": spike}, now=NOW)
    assert not portfolio.snapshot().open_positions


def test_score_history_is_sampled_and_threshold_crossings_recorded(tmp_path):
    candidate, _, _ = watcher(tmp_path, scorer=SequenceScorer([.5, .5, .5]))
    for seconds in (0, 2, 21):
        now = NOW + timedelta(seconds=seconds)
        candidate.process_quotes({"ACME": quote(now)}, now=now)
    lines = (tmp_path / "scores.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_dashboard_includes_watchlist_candidate(tmp_path):
    store = CandidateContextStore(tmp_path / "watchlist.json")
    item = context()
    item.live_market_score, item.dynamic_score = .8, .72
    item.slow_weight, item.live_weight, item.current_price = .6, .4, 102
    store.replace([item], now=NOW)
    view = shadow_dashboard_projection({}, {}, {}, json.loads(store.path.read_text()), now=NOW)
    assert view["watchlist"][0]["symbol"] == "ACME"
    assert view["watchlist"][0]["dynamic_score"] == .72


def test_fast_candidate_path_has_no_llm_codex_web_or_order_mutation_imports():
    path = Path(__file__).resolve().parents[1] / "watcher" / "candidate_watcher.py"
    tree = ast.parse(path.read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imports.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom): imports.append(node.module or "")
    forbidden = ("subprocess", "codex", "llm", "web", "robinhood_executor", "robinhood_mcp")
    assert not any(any(name in module for name in forbidden) for module in imports)
    source = path.read_text().lower()
    assert not any(token in source for token in ("place_order", "submit_order", "cancel_order", "review_order"))


def test_context_store_concurrent_snapshot_and_replace(tmp_path):
    store = CandidateContextStore(tmp_path / "watchlist.json")
    errors = []
    def writer():
        try:
            for index in range(20):
                store.replace([context(slow=.6 + index / 1000)], now=NOW)
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=writer)
    thread.start()
    while thread.is_alive():
        store.snapshot()
    thread.join()
    assert not errors
    assert json.loads(store.path.read_text())["schema_version"] == 1


def test_exact_five_minute_boundary_excludes_forming_bar():
    candle = Candle(NOW.isoformat(), 100, 101, 99, 100.5, 1000)
    assert completed_candles((candle,), NOW + timedelta(minutes=4, seconds=59)) == ()
    assert completed_candles((candle,), NOW + timedelta(minutes=5)) == (candle,)
