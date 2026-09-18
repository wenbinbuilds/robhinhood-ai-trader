"""Local event-architecture tests; no MCP, Codex, model, or order call."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest

import config
from agent.models import LlmCandidateAnalysis, LlmReasoningResult, ReasoningTrace
from test_llm_reasoning import candidate as llm_candidate
from event_driven.alpha import AlphaCombiner
from event_driven.bus import InProcessEventBus
from event_driven.events import (
    AlphaUpdatedEvent,
    BarClosedEvent,
    CandidateDiscoveredEvent,
    ContextUpdatedEvent,
    EventPriority,
    MarketEvent,
    NewsUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    QuoteEvent,
    RiskApprovedEvent,
    RiskRejectedEvent,
    ScannerEvent,
    TradeCandidateEvent,
)
from event_driven.orchestrator import ReasoningTriggerPolicy, ShadowEventOrchestrator
from event_driven.reasoning import EventDrivenReasoningProvider
from event_driven.simulation import run_deterministic_shadow_simulation
from event_driven.state import CandidateState, CandidateStateStore, IllegalStateTransition, LEGAL_TRANSITIONS
from portfolio.construction import PortfolioConstructor
from watcher.status import shadow_dashboard_projection


NOW = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)


def test_typed_event_creation_and_timezone_contract():
    item = QuoteEvent(NOW, "MOCK", symbol="acme", cycle_id="c1", payload={"price": 101})
    assert item.event_type.value == "QUOTE"
    assert item.symbol == "ACME"
    assert item.priority == EventPriority.MEDIUM
    assert item.to_dict()["timestamp"] == NOW.isoformat()
    with pytest.raises(ValueError):
        MarketEvent(NOW.replace(tzinfo=None), "MOCK")


def test_dispatch_priority_order_and_handler_failure_isolation():
    bus = InProcessEventBus()
    observed = []
    bus.subscribe(None, lambda item: observed.append(item.event_type.value))
    bus.subscribe(ScannerEvent.event_type, lambda item: (_ for _ in ()).throw(RuntimeError("isolated")))
    bus.publish(ScannerEvent(NOW, "MOCK"))
    bus.publish(RiskRejectedEvent(NOW, "MOCK", symbol="ACME"))
    assert bus.drain() == 2
    assert observed == ["RISK_REJECTED", "SCANNER"]
    assert bus.failures[0]["error"] == "RuntimeError"


def test_newest_quote_wins_and_bounded_backpressure():
    bus = InProcessEventBus(max_pending=3)
    prices = []
    bus.subscribe(QuoteEvent.event_type, lambda item: prices.append(item.payload["price"]))
    for price in (100, 101, 102):
        bus.publish(QuoteEvent(NOW + timedelta(seconds=price - 100), "MOCK", symbol="ACME", payload={"price": price}))
    bus.drain()
    assert prices == [102]
    assert bus.coalesced_quotes == 2

    bus.publish(ScannerEvent(NOW, "MOCK"))
    bus.publish(ScannerEvent(NOW, "MOCK"))
    bus.publish(ScannerEvent(NOW, "MOCK"))
    assert bus.publish(RiskRejectedEvent(NOW, "MOCK", symbol="ACME"))
    assert bus.dropped_events == 1


def test_state_store_legal_lifecycle_persistence_and_illegal_transition(tmp_path):
    store = CandidateStateStore(tmp_path / "states.json")
    store.discover("ACME", timestamp=NOW)
    for new_state in (
        CandidateState.WATCHLIST, CandidateState.SETUP_FORMING,
        CandidateState.TRADE_READY, CandidateState.RISK_APPROVED,
        CandidateState.POSITION_OPEN, CandidateState.EXIT_PENDING,
        CandidateState.CLOSED,
    ):
        store.transition("ACME", new_state, timestamp=NOW, event_type="TEST")
    restored = CandidateStateStore(store.path).get("ACME")
    assert restored.state == CandidateState.CLOSED
    assert len(restored.transition_history) == 8
    with pytest.raises(IllegalStateTransition):
        store.transition("ACME", CandidateState.WATCHLIST, timestamp=NOW, event_type="BAD")
    assert CandidateState.TRADE_READY in LEGAL_TRANSITIONS[CandidateState.SETUP_FORMING]


def test_state_store_projects_bar_context_news_quote_and_alpha(tmp_path):
    store = CandidateStateStore(tmp_path / "states.json")
    store.handle(CandidateDiscoveredEvent(NOW, "SCANNER", symbol="ACME"))
    store.transition("ACME", CandidateState.WATCHLIST, timestamp=NOW, event_type="ADMIT")
    store.handle(BarClosedEvent(NOW, "TECH", symbol="ACME", payload={
        "completed_5m_candles": [{"begins_at": NOW.isoformat()}],
        "technical_metrics": {"ema9": 101},
    }))
    store.handle(ContextUpdatedEvent(NOW, "LLM", symbol="ACME", payload={
        "llm_context": {"summary": "stored"}, "research_timestamp": NOW.isoformat(),
        "context_expiration": (NOW + timedelta(minutes=5)).isoformat(),
    }))
    store.handle(NewsUpdatedEvent(NOW, "NEWS", symbol="ACME", payload={"status": "AVAILABLE"}))
    store.handle(QuoteEvent(NOW, "QUOTE", symbol="ACME", payload={"price": 102}))
    store.handle(AlphaUpdatedEvent(NOW, "ALPHA", symbol="ACME", payload={
        "slow_alpha_score": .66, "live_market_score": .8, "combined_alpha_score": .71,
    }))
    item = store.get("ACME")
    assert item.latest_quote["price"] == 102
    assert item.completed_5m_candles
    assert item.slow_technical_metrics["ema9"] == 101
    assert item.llm_context["summary"] == "stored"
    assert item.news_context["status"] == "AVAILABLE"
    assert item.combined_alpha_score == .71


def test_scanner_discovery_still_active_and_removal_events(tmp_path):
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    runtime.ingest_slow_cycle(
        {"generated_at": NOW.isoformat(), "scanner_candidates": [{"symbol": "ACME"}],
         "analyzed_candidates": []}, [], now=NOW,
    )
    assert runtime.state_store.get("ACME").state == CandidateState.WATCHLIST
    runtime.ingest_slow_cycle(
        {"generated_at": (NOW + timedelta(minutes=5)).isoformat(),
         "scanner_candidates": [], "analyzed_candidates": []}, [],
        now=NOW + timedelta(minutes=5),
    )
    assert runtime.state_store.get("ACME").state == CandidateState.EXPIRED
    runtime.bus.drain()
    text = (tmp_path / "events.jsonl").read_text()
    assert "CANDIDATE_DISCOVERED" in text
    assert "CANDIDATE_REMOVED" in text


def test_closed_or_failed_scanner_preserves_watchlist_until_ttl(tmp_path):
    runtime = ShadowEventOrchestrator(
        state_path=tmp_path / "states.json", event_log_path=tmp_path / "events.jsonl"
    )
    runtime.ingest_scanner_snapshot({
        "generated_at": NOW.isoformat(),
        "market": {"is_regular_session": True},
        "scanner": {"status": "OK", "candidates": [{"symbol": "ACME"}]},
    }, now=NOW)
    assert runtime.state_store.get("ACME").state == CandidateState.WATCHLIST
    runtime.ingest_scanner_snapshot({
        "generated_at": (NOW + timedelta(minutes=1)).isoformat(),
        "market": {"is_regular_session": False},
        "scanner": {"status": "SKIPPED_MARKET_CLOSED", "candidates": []},
    }, now=NOW + timedelta(minutes=1))
    assert runtime.state_store.get("ACME").state == CandidateState.WATCHLIST


def test_trade_risk_position_event_projection(tmp_path):
    store = CandidateStateStore(tmp_path / "states.json")
    store.handle(CandidateDiscoveredEvent(NOW, "SCANNER", symbol="ACME"))
    store.transition("ACME", CandidateState.WATCHLIST, timestamp=NOW, event_type="ADMIT")
    store.transition("ACME", CandidateState.SETUP_FORMING, timestamp=NOW, event_type="RESEARCH")
    store.handle(TradeCandidateEvent(NOW, "ALPHA", symbol="ACME"))
    store.handle(RiskApprovedEvent(NOW, "RISK", symbol="ACME"))
    store.handle(PositionOpenedEvent(NOW, "SHADOW", symbol="ACME"))
    store.handle(PositionClosedEvent(NOW, "SHADOW", symbol="ACME", payload={"reason": "TARGET_HIT"}))
    assert store.get("ACME").state == CandidateState.CLOSED

    store.handle(CandidateDiscoveredEvent(NOW, "SCANNER", symbol="RISKY"))
    store.transition("RISKY", CandidateState.WATCHLIST, timestamp=NOW, event_type="ADMIT")
    store.transition("RISKY", CandidateState.SETUP_FORMING, timestamp=NOW, event_type="RESEARCH")
    store.handle(TradeCandidateEvent(NOW, "ALPHA", symbol="RISKY"))
    store.handle(RiskRejectedEvent(NOW, "RISK", symbol="RISKY", payload={"reason": "DAILY_LOSS_LIMIT"}))
    assert store.get("RISKY").state == CandidateState.REJECTED


def test_alpha_combination_decay_and_ttl():
    combiner = AlphaCombiner(ttl_seconds=300, slow_weight_fresh=.70, slow_weight_expiring=.50)
    start = combiner.combine(.66, .88, context_age_seconds=0)
    middle = combiner.combine(.66, .88, context_age_seconds=150)
    end = combiner.combine(.66, .88, context_age_seconds=300)
    assert (start.slow_weight, middle.slow_weight, end.slow_weight) == pytest.approx((.7, .6, .5))
    assert start.live_weight + start.slow_weight == pytest.approx(1)
    assert not start.expired and end.expired
    assert end.combined_alpha_score > start.combined_alpha_score


def test_reasoning_trigger_policy_changes_only_for_meaningful_evidence():
    policy = ReasoningTriggerPolicy()
    evidence = dict(completed_bar_timestamp=NOW.isoformat(), news_event_ids=["n1"],
                    sector_regime="STRONG", market_regime="BULLISH",
                    technical_disposition="QUALIFIED")
    assert policy.should_refresh("ACME", **evidence)
    assert not policy.should_refresh("ACME", **evidence)
    assert policy.should_refresh("ACME", **{**evidence, "news_event_ids": ["n1", "n2"]})


def _reasoning_result(now=NOW):
    return LlmReasoningResult(
        status="AVAILABLE",
        candidates=(LlmCandidateAnalysis.from_mapping(llm_candidate("ACME")),),
        trace=ReasoningTrace("FAKE", None, now.isoformat(), .1, 1, "1", "1", "SUCCESS", None),
    )


def _reasoning_payload(bar=NOW.isoformat()):
    return {
        "analysis_timestamp": NOW.isoformat(),
        "broad_market_context": {"deterministic_interpretation": {"regime": "BULLISH"}},
        "candidates": [{
            "symbol": "ACME",
            "deterministic_technical_metrics": {"latest_completed_bar_timestamp": bar,
                                                  "technical_validation": {"technical_score": .8}},
            "deterministic_proposed_setup": {"technical_disposition": "QUALIFIED"},
            "deterministic_news_event_clusters": [],
            "deterministic_sector_classification": {"sector": "TECH", "bias": "POSITIVE"},
        }],
    }


def test_llm_context_reused_until_meaningful_event():
    class Fake:
        def __init__(self): self.calls = 0
        def reason(self, payload, *, expected_symbols, now):
            self.calls += 1
            return _reasoning_result(now)
    fake = Fake()
    provider = EventDrivenReasoningProvider(fake)
    first = provider.reason(_reasoning_payload(), expected_symbols=["ACME"], now=NOW)
    second = provider.reason({**_reasoning_payload(), "analysis_timestamp": (NOW + timedelta(seconds=2)).isoformat()}, expected_symbols=["ACME"], now=NOW)
    third = provider.reason(
        _reasoning_payload((NOW + timedelta(minutes=5)).isoformat()),
        expected_symbols=["ACME"], now=NOW,
    )
    assert first.status == second.status == "AVAILABLE"
    assert second.trace.status == "CACHED"
    assert third.trace.status == "CACHED"
    assert fake.calls == 1


def test_qualitative_cache_refreshes_only_for_semantic_evidence():
    class Fake:
        def __init__(self): self.calls = 0
        def reason(self, payload, *, expected_symbols, now):
            self.calls += 1
            return _reasoning_result(now)
    fake = Fake()
    provider = EventDrivenReasoningProvider(fake)
    base = _reasoning_payload()
    first = provider.reason(base, expected_symbols=["ACME"], now=NOW)
    original = first.trace.reasoning_invocation_timestamp

    numeric = _reasoning_payload((NOW + timedelta(minutes=5)).isoformat())
    numeric["candidates"][0]["deterministic_technical_metrics"].update(
        rsi14=72, macd=-0.4, macd_signal=-0.2,
    )
    cached = provider.reason(
        numeric, expected_symbols=["ACME"], now=NOW + timedelta(minutes=5)
    )
    assert cached.trace.status == "CACHED"
    assert cached.trace.reasoning_invocation_timestamp == original
    assert (
        cached.candidates[0].qualitative_analysis
        == first.candidates[0].qualitative_analysis
    )
    assert cached.trace.diagnostics["per_candidate"]["ACME"]["cache_age_seconds"] == 300
    assert fake.calls == 1

    news = _reasoning_payload()
    news["candidates"][0]["deterministic_news_event_clusters"] = [
        {"event_id": "new-filing", "catalyst_type": "SEC_FILING",
         "published_at": NOW.isoformat(), "sources": []}
    ]
    refreshed = provider.reason(
        news, expected_symbols=["ACME"], now=NOW + timedelta(minutes=6)
    )
    assert refreshed.trace.diagnostics["per_candidate"]["ACME"]["reason"] == "NEW_MATERIAL_NEWS"
    assert fake.calls == 2


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["candidates"][0]["deterministic_sector_classification"].update(bias="BEARISH"), "SECTOR_REGIME_CHANGE"),
        (lambda value: value["broad_market_context"]["deterministic_interpretation"].update(regime="BEARISH"), "MARKET_REGIME_CHANGE"),
    ],
)
def test_sector_and_market_regime_changes_refresh_llm(mutation, reason):
    class Fake:
        def __init__(self): self.calls = 0
        def reason(self, payload, *, expected_symbols, now):
            self.calls += 1
            return _reasoning_result(now)
    fake = Fake()
    provider = EventDrivenReasoningProvider(fake)
    provider.reason(_reasoning_payload(), expected_symbols=["ACME"], now=NOW)
    changed = _reasoning_payload()
    mutation(changed)
    result = provider.reason(
        changed, expected_symbols=["ACME"], now=NOW + timedelta(minutes=1)
    )
    assert result.trace.diagnostics["per_candidate"]["ACME"]["reason"] == reason
    assert fake.calls == 2


def test_qualitative_cache_ttl_is_independent_of_candidate_ttl():
    class Fake:
        def __init__(self): self.calls = 0
        def reason(self, payload, *, expected_symbols, now):
            self.calls += 1
            return _reasoning_result(now)
    fake = Fake()
    provider = EventDrivenReasoningProvider(fake, ttl_seconds=600)
    provider.reason(_reasoning_payload(), expected_symbols=["ACME"], now=NOW)
    still_cached = provider.reason(
        _reasoning_payload(), expected_symbols=["ACME"],
        now=NOW + timedelta(seconds=301),
    )
    expired = provider.reason(
        _reasoning_payload(), expected_symbols=["ACME"],
        now=NOW + timedelta(seconds=601),
    )
    assert still_cached.trace.status == "CACHED"
    assert expired.trace.diagnostics["per_candidate"]["ACME"]["reason"] == "QUALITATIVE_CONTEXT_EXPIRED"
    assert fake.calls == 2


def test_only_semantic_cache_misses_are_batched_and_symbols_stay_isolated():
    class Fake:
        def __init__(self): self.calls = []
        def reason(self, payload, *, expected_symbols, now):
            self.calls.append(tuple(expected_symbols))
            return LlmReasoningResult(
                status="AVAILABLE",
                candidates=tuple(
                    LlmCandidateAnalysis.from_mapping(llm_candidate(symbol))
                    for symbol in expected_symbols
                ),
                trace=ReasoningTrace(
                    "FAKE", "gpt-5.6-sol", now.isoformat(), .1,
                    len(expected_symbols), "1", "1", "SUCCESS", None,
                ),
            )
    fake = Fake()
    provider = EventDrivenReasoningProvider(fake)
    first = _reasoning_payload()
    beta = dict(first["candidates"][0])
    beta["symbol"] = "BETA"
    first["candidates"] = [first["candidates"][0], beta]
    provider.reason(first, expected_symbols=["ACME", "BETA"], now=NOW)

    changed = _reasoning_payload()
    beta_changed = dict(changed["candidates"][0])
    beta_changed["symbol"] = "BETA"
    beta_changed["deterministic_news_event_clusters"] = [{
        "event_id": "beta-news", "catalyst_type": "PRODUCT",
        "published_at": NOW.isoformat(), "sources": [],
    }]
    changed["candidates"] = [changed["candidates"][0], beta_changed]
    result = provider.reason(
        changed, expected_symbols=["ACME", "BETA"],
        now=NOW + timedelta(minutes=5),
    )
    assert fake.calls == [("ACME", "BETA"), ("BETA",)]
    assert set(result.by_symbol()) == {"ACME", "BETA"}
    assert result.trace.diagnostics["cache_hits"] == 1
    assert result.trace.diagnostics["cache_misses"] == 1


def test_malformed_batch_is_retried_per_symbol_for_failure_isolation():
    class Fake:
        def __init__(self): self.calls = []
        def reason(self, payload, *, expected_symbols, now):
            symbols = tuple(expected_symbols)
            self.calls.append(symbols)
            if len(symbols) > 1 or symbols == ("BETA",):
                return LlmReasoningResult(
                    status="UNAVAILABLE", candidates=(),
                    trace=ReasoningTrace(
                        "FAKE", None, now.isoformat(), .1, len(symbols),
                        "1", "1", "ERROR", "LLM_REASONING_SCHEMA_VIOLATION",
                    ),
                    failure_reason="LLM_REASONING_SCHEMA_VIOLATION",
                )
            return LlmReasoningResult(
                status="AVAILABLE",
                candidates=(LlmCandidateAnalysis.from_mapping(llm_candidate("ACME")),),
                trace=ReasoningTrace(
                    "FAKE", None, now.isoformat(), .1, 1,
                    "1", "1", "SUCCESS", None,
                ),
            )
    payload = _reasoning_payload()
    beta = dict(payload["candidates"][0])
    beta["symbol"] = "BETA"
    payload["candidates"] = [payload["candidates"][0], beta]
    fake = Fake()
    result = EventDrivenReasoningProvider(fake).reason(
        payload, expected_symbols=["ACME", "BETA"], now=NOW
    )
    assert fake.calls == [("ACME", "BETA"), ("ACME",), ("BETA",)]
    assert result.status == "AVAILABLE"
    assert set(result.by_symbol()) == {"ACME"}
    assert result.failure_reason == "PARTIAL_LLM_FAILURE:BETA"
    assert result.trace.status == "PARTIAL_SUCCESS"


def test_llm_latency_is_separate_from_quote_state(tmp_path):
    entered, release = Event(), Event()
    class Slow:
        def reason(self, payload, *, expected_symbols, now):
            entered.set()
            assert release.wait(2)
            return _reasoning_result(now)
    provider = EventDrivenReasoningProvider(Slow())
    thread = Thread(target=lambda: provider.reason(_reasoning_payload(), expected_symbols=["ACME"], now=NOW))
    thread.start()
    assert entered.wait(1)
    store = CandidateStateStore(tmp_path / "states.json")
    store.handle(CandidateDiscoveredEvent(NOW, "SCANNER", symbol="ACME"))
    store.handle(QuoteEvent(NOW, "QUOTE", symbol="ACME", payload={"price": 101}))
    assert store.get("ACME").latest_quote["price"] == 101
    release.set()
    thread.join(2)
    assert not thread.is_alive()


def test_portfolio_construction_rejects_nonapproved_risk():
    with pytest.raises(ValueError):
        PortfolioConstructor().construct({}, {"approved": False}, {}, now=NOW)


def test_dashboard_has_candidate_lifecycle_sections():
    watchlist = {"candidates": [{
        "symbol": "ACME", "candidate_state": "SETUP_FORMING",
        "research_timestamp": NOW.isoformat(), "analysis_price": 101,
        "slow_context_score": .66, "live_market_score": .8,
        "dynamic_score": .71,
    }]}
    view = shadow_dashboard_projection(
        {"open_positions": [], "closed_positions": []}, {}, {}, watchlist, now=NOW
    )
    assert view["universe"][0]["symbol"] == "ACME"
    assert view["setup_forming"][0]["state"] == "SETUP_FORMING"
    assert view["trade_ready"] == []
    assert view["open_positions"] == []
    assert view["recently_closed"] == []


def test_deterministic_between_bar_simulation_and_hard_risk_veto(tmp_path):
    result = run_deterministic_shadow_simulation(tmp_path)
    assert result["combined_alpha_path"][1] < config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD
    assert result["combined_alpha_path"][2] >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD
    assert result["exit_reason"] == "TARGET_HIT"
    assert result["risk_blocked_high_alpha"] is True
    assert result["real_order_operations"] == 0


def test_fast_paths_have_no_llm_or_order_dependency_and_direct_data_stays_enabled():
    root = Path(__file__).resolve().parents[1]
    for relative in ("watcher/candidate_watcher.py", "watcher/fast_watcher.py"):
        source = (root / relative).read_text()
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom): imports.append(node.module or "")
        assert not any("llm" in name or "codex" in name for name in imports)
        lowered = source.lower()
        assert not any(value in lowered for value in ("place_order", "submit_order", "cancel_order", "review_order"))
    assert config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP"
    assert config.MODE == "SHADOW_TRADING"
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False


def test_event_bus_clean_shutdown():
    bus = InProcessEventBus()
    bus.start()
    bus.publish(ScannerEvent(NOW, "MOCK"))
    bus.stop()
    assert bus.pending_count == 0
