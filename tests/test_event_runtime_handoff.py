"""End-to-end local handoff tests for slow research -> event state -> fast quotes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.candidate_context import CandidateContextStore, contexts_from_cycle
from event_driven.orchestrator import ShadowEventOrchestrator
from event_driven.state import CandidateState
from shadow.execution import ShadowExecutionEngine
from shadow.portfolio import ShadowPortfolio
from watcher.candidate_watcher import FastCandidateWatcher, LiveScore
from watcher.models import FastQuote
from test_candidate_watchlist import FreshRefresher


NOW = datetime(2026, 9, 11, 16, 40, tzinfo=timezone.utc)


def slow_result(score: float = .569, *, disposition: str = "QUALIFIED",
                stop: float | None = 11.8378, target: float | None = 12.11,
                risk_reward: float | None = 1.95):
    vetoes = [] if disposition == "QUALIFIED" else ["technical setup failed deterministic validation"]
    return {
        "generated_at": NOW.isoformat(),
        "timestamp": NOW.isoformat(),
        "analysis_started_at": NOW.isoformat(),
        "market_context": {"effective_regular_session": True},
        "llm_reasoning": {"status": "AVAILABLE", "trace": {"status": "SUCCESS"}},
        "scanner_candidates": [{"symbol": "NMAX", "instrument_type": "EQUITY"}],
        "analyzed_candidates": [{
            "symbol": "NMAX", "technical_disposition": disposition,
            "entry": 11.93 if stop is not None else None,
            "stop": stop, "target": target, "risk_reward_ratio": risk_reward,
            "invalidation_condition": "price loses the validated stop reference",
            "supporting_indicators": {
                "intraday_support_reference": stop,
                "intraday_resistance_reference": target,
            },
            "technical_context": {
                "technical_score": .846, "evidence": ["trend"], "conflicts": [],
            },
            "market_context": {"regime": "BULLISH"},
            "sector_context": {"sector": "GENERAL"},
            "news_context": {"score": .35},
            "deterministic_technical_metrics": {
                "technical_data_valid": disposition == "QUALIFIED",
                "current_price": 11.93, "bid": 11.92, "ask": 11.93,
                "vwap": 11.88, "ema9": 11.89,
                "technical_validation": {"rules": [{
                    "rule_name": "QUOTE_FRESHNESS", "type": "HARD", "status": "PASS",
                }]},
            },
            "llm_analysis": {
                "qualitative_analysis": {"summary": "fixture", "reasons_against": []},
                "news_analysis": {"catalyst_type": "NONE"},
            },
            "coordinator_decision": {
                "combined_score": score, "technical_score": .846,
                "qualitative_score": .33, "news_score": .35,
                "sector_score": .4, "market_score": .8,
                "confidence": .7, "vetoes": vetoes,
            },
        }],
    }


class PriceScorer:
    def __init__(self):
        self.calls = 0

    def score(self, context, quote):
        self.calls += 1
        value = .50 if self.calls == 1 else .60
        return LiveScore(value, {"fixture": {"value": value, "weight": 1, "observed": quote.last_price}})


class SequenceScorer:
    def __init__(self, values):
        self.values = iter(values)

    def score(self, context, quote):
        value = next(self.values)
        return LiveScore(value, {
            "fixture": {"value": value, "weight": 1, "observed": quote.last_price}
        })


def test_subthreshold_slow_research_is_not_lost_from_canonical_state(tmp_path):
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    result = slow_result(.569)
    runtime.ingest_slow_cycle(result, [], now=NOW)
    state = runtime.state_store.get("NMAX")
    assert state.state == CandidateState.WATCHLIST
    assert state.slow_alpha_score == .569
    assert state.technical_score == .846
    assert state.qualitative_score == .33
    assert state.news_score == .35
    assert state.sector_score == .4
    assert state.market_score == .8
    assert state.research_price == 11.93
    assert state.stop_reference == 11.8378
    assert state.target_reference == 12.11
    assert state.eligible_for_fast_watch is False
    assert state.admission_reason == "FAST_WATCH_NOT_ADMITTED: SLOW_SCORE_BELOW_0.60"
    runtime.bus.drain()
    alpha = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()
             if json.loads(line)["event"] == "ALPHA_UPDATED"][-1]
    assert alpha["slow_score"] == .569
    assert alpha["previous_state"] == "WATCHLIST"
    assert alpha["new_state"] == "WATCHLIST"


def test_one_canonical_state_store_receives_slow_and_fast_updates(tmp_path):
    result = slow_result(.66)
    contexts = contexts_from_cycle(result, now=NOW)
    assert len(contexts) == 1
    context_cache = CandidateContextStore(tmp_path / "research_contexts.json")
    context_cache.replace(contexts, now=NOW)
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    runtime.ingest_slow_cycle(result, contexts, now=NOW)
    clock = [NOW + timedelta(seconds=5)]
    scorer = PriceScorer()
    candidate = FastCandidateWatcher(
        context_cache,
        ShadowExecutionEngine(ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=scorer,
        event_orchestrator=runtime,
        state_store=runtime.state_store,
        clock=lambda: clock[0],
    )
    assert candidate.state_store is runtime.state_store
    assert runtime.state_store.get("NMAX").state == CandidateState.SETUP_FORMING

    runtime.quote(FastQuote("NMAX", 11.92, 11.93, 11.925, clock[0], "MOCK", True))
    runtime.bus.drain()
    first = runtime.state_store.get("NMAX")
    first_combined = first.combined_alpha_score
    assert first.live_market_score == .5
    assert first.context_age_seconds == 5

    clock[0] = NOW + timedelta(seconds=7)
    runtime.quote(FastQuote("NMAX", 11.92, 11.93, 11.925, clock[0], "MOCK", True))
    runtime.bus.drain()
    second = runtime.state_store.get("NMAX")
    assert second.live_market_score == .6
    assert second.combined_alpha_score != first_combined
    assert second.context_age_seconds == 7
    assert scorer.calls == 2


def test_context_expiration_updates_same_canonical_store(tmp_path):
    result = slow_result(.66)
    contexts = contexts_from_cycle(result, now=NOW)
    cache = CandidateContextStore(tmp_path / "research_contexts.json")
    cache.replace(contexts, now=NOW)
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    runtime.ingest_slow_cycle(result, contexts, now=NOW)
    candidate = FastCandidateWatcher(
        cache,
        ShadowExecutionEngine(ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        event_orchestrator=runtime,
        clock=lambda: NOW + timedelta(seconds=300),
    )
    assert candidate.symbols(NOW + timedelta(seconds=300)) == []
    assert runtime.state_store.get("NMAX").state == CandidateState.EXPIRED


def test_open_shadow_position_keeps_position_ownership_across_slow_refresh(tmp_path):
    result = slow_result(.632)
    contexts = contexts_from_cycle(result, now=NOW)
    cache = CandidateContextStore(tmp_path / "research_contexts.json")
    cache.replace(contexts, now=NOW)
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json",
        event_log_path=tmp_path / "events.jsonl",
        shadow_portfolio=portfolio,
    )
    runtime.ingest_slow_cycle(result, contexts, now=NOW)
    clock = [NOW + timedelta(seconds=133.26)]
    candidate = FastCandidateWatcher(
        cache,
        ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=SequenceScorer([.821768, .846344, .846344]),
        event_orchestrator=runtime,
        pre_execution_refresher=FreshRefresher(),
        clock=lambda: clock[0],
    )

    runtime.quote(FastQuote(
        "NMAX", 11.92, 11.93, 11.925, clock[0], "MOCK", True
    ))
    runtime.bus.drain()
    first = runtime.state_store.get("NMAX")
    assert first.combined_alpha_score == pytest.approx(.705788, abs=2e-5)
    assert first.state == CandidateState.SETUP_FORMING

    clock[0] = NOW + timedelta(seconds=169.26)
    runtime.quote(FastQuote(
        "NMAX", 11.92, 11.93, 11.925, clock[0], "MOCK", True
    ))
    runtime.bus.drain()
    opened = runtime.state_store.get("NMAX")
    assert opened.combined_alpha_score == pytest.approx(.720489, abs=2e-5)
    assert opened.state == CandidateState.SETUP_FORMING

    # The unchanged two-update confirmation rule requires one more qualifying
    # quote after the threshold crossing.
    clock[0] = NOW + timedelta(seconds=170.26)
    runtime.quote(FastQuote(
        "NMAX", 11.92, 11.93, 11.925, clock[0], "MOCK", True
    ))
    runtime.bus.drain()
    opened = runtime.state_store.get("NMAX")
    assert opened.state == CandidateState.POSITION_OPEN
    assert portfolio.has_symbol("NMAX")
    assert len(portfolio.snapshot().open_positions) == 1

    later = NOW + timedelta(minutes=5)
    degraded = slow_result(.2, disposition="REJECTED")
    degraded["generated_at"] = later.isoformat()
    degraded["analysis_started_at"] = later.isoformat()
    runtime.ingest_slow_cycle(degraded, [], now=later)
    runtime.ingest_scanner_rows(
        [{"symbol": "NMAX"}], now=later + timedelta(seconds=1),
        cycle_id="rediscovery",
    )
    runtime.bus.drain()

    refreshed = runtime.state_store.get("NMAX")
    assert refreshed.state == CandidateState.POSITION_OPEN
    assert refreshed.open_position_context_status == "DEGRADED"
    assert refreshed.position_invalidation_signals
    assert "technical setup failed deterministic validation" in refreshed.position_invalidation_signals
    assert refreshed.admission_reason.startswith("POSITION_INVALIDATION_SIGNAL:")
    assert refreshed.technical_score == .846
    assert refreshed.news_context["score"] == .35
    assert len(portfolio.snapshot().open_positions) == 1
    assert not any(
        item.new_state in {"REJECTED", "WATCHLIST", "SETUP_FORMING", "TRADE_READY"}
        for item in refreshed.transition_history
        if item.previous_state == "POSITION_OPEN"
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    slow_alpha = [
        row for row in records
        if row["event"] == "ALPHA_UPDATED" and row["symbol"] == "NMAX"
    ][-1]
    assert slow_alpha["previous_state"] == "POSITION_OPEN"
    assert slow_alpha["new_state"] == "POSITION_OPEN"
    assert slow_alpha["candidate_state"] == "POSITION_OPEN"

    # A portfolio/state divergence is explicit and repaired toward the local
    # exposure record; it is never silently treated as a new candidate.
    divergent = ShadowEventOrchestrator(
        state_path=tmp_path / "divergent_states.json",
        event_log_path=tmp_path / "divergent_events.jsonl",
        shadow_portfolio=portfolio,
    )
    divergent.state_store.discover("NMAX", timestamp=later)
    divergent.reconcile_position_state(now=later)
    divergent.bus.drain()
    assert divergent.state_store.get("NMAX").state == CandidateState.POSITION_OPEN
    divergence_events = (tmp_path / "divergent_events.jsonl").read_text()
    assert "PORTFOLIO_OPEN_STATE_NOT_OPEN" in divergence_events


def test_runtime_rejects_a_second_candidate_state_store(tmp_path):
    result = slow_result(.66)
    contexts = contexts_from_cycle(result, now=NOW)
    cache = CandidateContextStore(tmp_path / "research_contexts.json")
    cache.replace(contexts, now=NOW)
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    from event_driven.state import CandidateStateStore
    with pytest.raises(ValueError, match="share CandidateStateStore"):
        FastCandidateWatcher(
            cache,
            ShadowExecutionEngine(ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")),
            events_path=tmp_path / "candidate_events.jsonl",
            score_history_path=tmp_path / "scores.jsonl",
            event_orchestrator=runtime,
            state_store=CandidateStateStore(tmp_path / "other_states.json"),
        )
