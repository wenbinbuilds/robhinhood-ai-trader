"""Deterministic, local-only proof of between-bar shadow entry and exit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.candidate_context import CandidateContext, CandidateContextStore
from event_driven.orchestrator import ShadowEventOrchestrator
from event_driven.state import CandidateState
from execution.shadow_executor import ShadowExecutor
from shadow.execution import ShadowExecutionEngine
from shadow.portfolio import ShadowPortfolio
from watcher.candidate_watcher import FastCandidateWatcher, LiveScore
from watcher.fast_watcher import FastPositionWatcher
from watcher.models import FastQuote


class _ScoreSequence:
    def __init__(self, values):
        self._values = iter(values)

    def score(self, context, quote):
        value = next(self._values)
        return LiveScore(value, {"simulation": {"value": value, "weight": 1.0, "observed": quote.mark_price}})


class _Quotes:
    name = "DETERMINISTIC_SIMULATION"
    mode = "REALTIME_FAST"

    def __init__(self, now: datetime, value: float) -> None:
        self.now, self.value = now, value

    def get_quotes(self, symbols):
        return {
            symbol: FastQuote(symbol, self.value, self.value + .02, self.value + .01,
                              self.now, self.name, True)
            for symbol in symbols
        }


def _context(symbol: str, now: datetime) -> CandidateContext:
    return CandidateContext(
        symbol=symbol, research_cycle_id="simulation-cycle", research_timestamp=now.isoformat(),
        analysis_price=101.0, slow_context_score=.66, technical_context_score=.8,
        qualitative_score=.7, news_score=.6, sector_score=.6, market_score=.7,
        llm_thesis="deterministic simulation", llm_conflicts=[], catalyst_classification="NONE",
        slow_technical_evidence=["EMA9_ABOVE_EMA20"], slow_technical_conflicts=[],
        slow_hard_gate_status={"QUOTE_FRESHNESS": "PASS"}, suggested_stop_reference=99,
        suggested_target_reference=105, invalidation_condition="price below 99",
        expiration_timestamp=(now + timedelta(seconds=300)).isoformat(), research_vwap=100.5,
        research_ema9=100.75, intraday_support_reference=99,
        intraday_resistance_reference=105, research_bid=100.99, research_ask=101.01,
        risk_reward_ratio=2.0, coordinator_confidence=.75,
        slow_score_formula="existing deterministic coordinator score",
    )


def run_deterministic_shadow_simulation(base_dir: str | Path | None = None) -> dict:
    """No network, model, or broker operation is reachable from this function."""
    temporary = TemporaryDirectory() if base_dir is None else None
    root = Path(temporary.name if temporary else base_dir)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
    portfolio = ShadowPortfolio(root / "portfolio.json", root / "trades.jsonl")
    context_store = CandidateContextStore(root / "watchlist.json")
    context_store.replace([_context("ACME", now)], now=now)
    runtime = ShadowEventOrchestrator(state_path=root / "states.json", event_log_path=root / "events.jsonl")
    runtime.state_store.discover("ACME", timestamp=now)
    runtime.state_store.transition("ACME", CandidateState.WATCHLIST, timestamp=now, event_type="SCANNER_ADMISSION")
    runtime.state_store.transition("ACME", CandidateState.SETUP_FORMING, timestamp=now, event_type="SLOW_ALPHA_READY")
    runtime.start()
    engine = ShadowExecutionEngine(portfolio)
    candidate = FastCandidateWatcher(
        context_store, engine, events_path=root / "candidate.jsonl",
        score_history_path=root / "scores.jsonl",
        scorer=_ScoreSequence([.57, .78, .88, .88]), event_orchestrator=runtime,
    )
    combined_path = []
    for seconds in (15, 70, 200, 202):
        at = now + timedelta(seconds=seconds)
        candidate.process_quotes({"ACME": FastQuote("ACME", 100.99, 101.01, 101,
                                                     at, "SIM_QUOTE", True)}, now=at,
                                 publish_quote_event=False)
        rows = context_store.snapshot()
        combined_path.append(rows[0].dynamic_score if rows else portfolio.snapshot().open_positions[0].dynamic_score)
    opened = portfolio.snapshot().open_positions[0]
    provider = _Quotes(now + timedelta(seconds=203), 106)
    position_watcher = FastPositionWatcher(
        portfolio, provider, ShadowExecutor(engine), status_path=root / "fast.json",
        events_path=root / "position.jsonl", clock=lambda: provider.now,
        event_orchestrator=runtime,
    )
    position_watcher.tick()
    closed = portfolio.snapshot().closed_positions[0]

    # Independent proof: even live alpha=1.0 cannot bypass the daily-loss veto.
    portfolio.state.daily_pnl = -portfolio.state.starting_capital * .03
    blocked_store = CandidateContextStore(root / "blocked_watchlist.json")
    blocked_store.replace([_context("RISKY", now)], now=now)
    blocked = FastCandidateWatcher(
        blocked_store, engine, events_path=root / "blocked.jsonl",
        score_history_path=root / "blocked_scores.jsonl",
        scorer=_ScoreSequence([1, 1]),
    )
    for seconds in (1, 3):
        at = now + timedelta(seconds=seconds)
        blocked.process_quotes({"RISKY": FastQuote("RISKY", 100.99, 101.01, 101,
                                                   at, "SIM_QUOTE", True)}, now=at)
    risk_blocked = not portfolio.has_symbol("RISKY")
    runtime.stop()
    result = {
        "mode": "SHADOW_TRADING",
        "symbol": "ACME",
        "slow_alpha": .66,
        "live_alpha_path": [.57, .78, .88, .88],
        "combined_alpha_path": combined_path,
        "entry_timestamp": opened.entry_timestamp,
        "entry_price": opened.entry_price,
        "exit_timestamp": closed.exit_timestamp,
        "exit_reason": closed.exit_reason,
        "risk_blocked_high_alpha": risk_blocked,
        "real_order_operations": 0,
    }
    if temporary is not None:
        temporary.cleanup()
    return result
