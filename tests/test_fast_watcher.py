"""Entirely local mocked quotes; never contact Robinhood or invoke a model."""
import ast
import json
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

import config
from execution.shadow_executor import ShadowExecutor
from shadow.portfolio import ShadowPortfolio
from watcher.fast_watcher import FastPositionWatcher
from watcher.models import ExitRequest, FastQuote
from watcher.quote_provider import SnapshotQuoteProvider
from watcher.scheduler import SlowMarketLoop, next_cycle_delay
from watcher.status import shadow_dashboard_projection, print_status
from watcher.storage import atomic_json, event
from test_shadow_trading import NOW, opened, coordinator, quote, candle
from agent.candidate_context import CandidateContextStore
from shadow.execution import ShadowExecutionEngine
from watcher.candidate_watcher import FastCandidateWatcher, LiveScore
from test_candidate_watchlist import context as candidate_context
from event_driven.reasoning import EventDrivenReasoningProvider
from agent.models import LlmReasoningResult, ReasoningTrace


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external process/model/broker call forbidden")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from agent.codex_mcp_bridge import CodexMcpBridge
    from agent.llm_reasoning_bridge import LlmReasoningBridge
    monkeypatch.setattr(CodexMcpBridge, "refresh", forbidden)
    monkeypatch.setattr(LlmReasoningBridge, "reason", forbidden)


class Quotes:
    name = "MOCK_BATCH"
    mode = "REALTIME_FAST"

    def __init__(self, price=102, at=NOW):
        self.price, self.at = price, at
        self.calls = []

    def get_quotes(self, symbols):
        self.calls.append(list(symbols))
        return {s: FastQuote(s, self.price, self.price + .02, self.price + .01,
                             self.at, self.name, True) for s in symbols}


def setup_watcher(tmp_path, provider=None, clock=None):
    portfolio, engine, position = opened(tmp_path)
    provider = provider or Quotes()
    watcher = FastPositionWatcher(portfolio, provider, ShadowExecutor(engine),
                                  status_path=tmp_path / "watcher.json",
                                  events_path=tmp_path / "events.jsonl",
                                  clock=clock or (lambda: NOW), interval=.01)
    return watcher, portfolio, engine, position, provider


def test_quote_conventions():
    q = Quotes().get_quotes(["ACME"])["ACME"]
    assert q.mid_price == pytest.approx(102.01)
    assert q.exit_price == 102
    assert q.age_at(NOW) == 0
    crossed = replace(q, bid=105, ask=100)
    assert crossed.mid_price is None
    assert crossed.mark_price == q.last_price
    assert replace(q, bid=None, ask=None).exit_price == q.last_price
    assert replace(q, bid=float("nan"), ask=None, last_price=None).mark_price is None


def test_batched_marks_pnl_and_restart(tmp_path):
    w, p, engine, position, provider = setup_watcher(tmp_path)
    engine.open_candidate(coordinator("BETA"), quote(), now=NOW)
    w.tick()
    assert provider.calls == [["ACME", "BETA"]]
    assert position.last_price == pytest.approx(102.01)
    assert position.current_bid == 102
    assert position.mark_price_method == "MIDPOINT"
    assert position.current_ask == 102.02
    assert position.unrealized_pnl == pytest.approx(round((102.01 - position.entry_price) * position.quantity, 4))
    assert w.status["status"] == "ACTIVE"
    restored = ShadowPortfolio(p.state_path, p.trades_path)
    assert len(restored.state.open_positions) == 2
    assert restored.state.open_positions[0].last_price_timestamp == NOW.isoformat()


@pytest.mark.parametrize("price,reason", [(98, "STOP_HIT"), (106, "TARGET_HIT")])
def test_exit_between_slow_cycles_is_local_and_conservative(tmp_path, price, reason):
    w, p, _, position, provider = setup_watcher(tmp_path, Quotes(price))
    w.tick()
    assert not p.state.open_positions
    trade = p.state.closed_positions[0]
    assert trade.exit_reason == reason
    assert trade.exit_method == "REALTIME_FAST_EXIT"
    assert trade.exit_price == round(price * (1 - config.SHADOW_EXIT_SLIPPAGE_BPS / 10000), 4)
    assert trade.exit_price < price
    assert len(p.trades_path.read_text().splitlines()) == 1
    w.tick()
    assert len(provider.calls) == 1  # Idle after exit.
    assert len(p.state.closed_positions) == 1


def test_second_exit_request_noop_under_race(tmp_path):
    w, p, _, position, provider = setup_watcher(tmp_path, Quotes(98))
    request = ExitRequest(position.trade_id, position.symbol, "STOP_HIT",
                          provider.get_quotes([position.symbol])[position.symbol], "REALTIME_FAST")
    results = []
    workers = [Thread(target=lambda: results.append(w.executor.execute_exit(request, now=NOW))) for _ in range(8)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(2)
        assert not t.is_alive()
    assert sum(r is not None for r in results) == 1
    assert len(p.state.closed_positions) == 1
    assert len(p.trades_path.read_text().splitlines()) == 1


@pytest.mark.parametrize("offset", [-6, 1])
def test_stale_or_future_quote_never_marks_or_exits(tmp_path, offset):
    w, p, _, position, _ = setup_watcher(tmp_path, Quotes(90, NOW + timedelta(seconds=offset)))
    original = position.last_price
    w.tick()
    assert position.last_price == original
    assert len(p.state.open_positions) == 1
    assert position.monitoring_status == "PRICE_MONITORING_DEGRADED"
    assert w.status["status"] == "DEGRADED"
    assert "FAST_QUOTE_STALE" in w.events_path.read_text()


def test_provider_error_sanitized_and_does_not_exit(tmp_path):
    class Broken(Quotes):
        def get_quotes(self, symbols):
            raise RuntimeError("must-not-log-secret")
    w, p, _, position, _ = setup_watcher(tmp_path, Broken())
    w.tick()
    assert p.state.open_positions
    assert w.status["status"] == "FAST_WATCHER_UNAVAILABLE"
    assert w.metrics["quote_failures"] == 1
    assert "FAST_PROVIDER_ERROR" in w.events_path.read_text()
    assert "must-not-log-secret" not in w.events_path.read_text()


def test_pre_execution_refresh_failure_blocks_entry_but_position_monitoring_continues(tmp_path):
    class SplitQuotes(Quotes):
        def get_quotes(self, symbols):
            self.calls.append(list(symbols))
            prices = {"ACME": 98.0, "NEW": 101.0}
            return {
                symbol: FastQuote(
                    symbol, prices[symbol], prices[symbol] + .02,
                    prices[symbol] + .01, NOW, self.name, True,
                )
                for symbol in symbols
            }

    class HighScore:
        def score(self, context, quote):
            return LiveScore(.99, {"fixture": {"value": .99, "weight": 1}})

    class FailedRefresh:
        def refresh_symbol(self, symbol, *, now):
            raise TimeoutError("mocked")

    watcher, portfolio, _engine, _position, _provider = setup_watcher(
        tmp_path, SplitQuotes()
    )
    cache = CandidateContextStore(tmp_path / "candidates.json")
    candidate = candidate_context(slow=.9)
    candidate.symbol = "NEW"
    candidate.consecutive_qualifying_updates = 1
    cache.replace([candidate], now=NOW)
    watcher.candidate_watcher = FastCandidateWatcher(
        cache, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl",
        scorer=HighScore(), pre_execution_refresher=FailedRefresh(),
    )

    watcher.tick()
    assert not portfolio.has_symbol("NEW")
    assert not portfolio.has_symbol("ACME")
    assert portfolio.snapshot().closed_positions[0].exit_reason == "STOP_HIT"
    assert "PRE_EXECUTION_REFRESH_FAILED:TimeoutError" in (
        tmp_path / "candidate_events.jsonl"
    ).read_text()


def test_both_fast_watchers_continue_while_slow_llm_is_blocked(tmp_path):
    entered, release = Event(), Event()

    class SlowReasoning:
        def reason(self, payload, *, expected_symbols, now):
            entered.set()
            assert release.wait(2)
            return LlmReasoningResult(
                status="UNAVAILABLE", candidates=(),
                trace=ReasoningTrace(
                    "FAKE", "gpt-5.6-sol", now.isoformat(), 1, 1,
                    "1", "1", "FAILED", "TEST_DONE",
                ),
                failure_reason="TEST_DONE",
            )

    slow = EventDrivenReasoningProvider(SlowReasoning())
    thread = Thread(target=lambda: slow.reason(
        {"broad_market_context": {}, "candidates": [{"symbol": "NEW"}]},
        expected_symbols=["NEW"], now=NOW,
    ))
    thread.start()
    assert entered.wait(1)

    watcher, portfolio, _engine, position, _provider = setup_watcher(
        tmp_path, Quotes(101)
    )
    cache = CandidateContextStore(tmp_path / "candidates.json")
    candidate = candidate_context(slow=.6, at=NOW)
    candidate.symbol = "NEW"
    cache.replace([candidate], now=NOW)

    class LowScore:
        def score(self, context, quote):
            return LiveScore(.1, {"fixture": {"value": .1, "weight": 1}})

    watcher.candidate_watcher = FastCandidateWatcher(
        cache, ShadowExecutionEngine(portfolio),
        events_path=tmp_path / "candidate_events.jsonl",
        score_history_path=tmp_path / "scores.jsonl", scorer=LowScore(),
    )
    watcher.tick()
    release.set()
    thread.join(2)
    assert position.last_price_timestamp == NOW.isoformat()
    assert cache.snapshot()[0].last_updated_at == NOW.isoformat()
    assert not thread.is_alive()


def test_idle_then_new_position_activates_batch(tmp_path):
    w, p, engine, _, provider = setup_watcher(tmp_path, Quotes(98))
    w.tick()  # Close first position.
    w.tick()
    assert w.status["status"] == "IDLE"
    assert len(provider.calls) == 1
    engine.open_candidate(coordinator("NEW"), quote(), now=NOW)
    provider.price = 102
    w.tick()
    assert provider.calls[-1] == ["NEW"]


def test_snapshot_provider_honestly_degraded_and_keeps_source_time(tmp_path):
    path = tmp_path / "snapshot.json"
    atomic_json(path, dict(data_source="ROBINHOOD_MCP", mcp_status="CONNECTED",
                          generated_at=NOW.isoformat(), market={"is_regular_session": True},
                          shadow_position_data=[{"symbol": "ACME", **quote(102), "bid": 101}]))
    provider = SnapshotQuoteProvider(path)
    q = provider.get_quotes(["ACME"])["ACME"]
    assert q.timestamp == NOW
    w, p, _, position, _ = setup_watcher(tmp_path, provider)
    w.tick()
    assert w.status["mode"] == "DEGRADED_SNAPSHOT"
    assert w.status["status"] == "DEGRADED"
    assert position.monitoring_status == "PRICE_MONITORING_DEGRADED"
    w.clock = lambda: NOW + timedelta(minutes=3)
    w.tick()
    assert w.metrics["max_quote_age"] == 180
    assert not p.state.closed_positions


def test_snapshot_stop_is_not_realtime_labeled(tmp_path):
    provider = Quotes(98)
    provider.mode = "DEGRADED_SNAPSHOT"
    w, p, *_ = setup_watcher(tmp_path, provider)
    w.tick()
    assert p.state.closed_positions[0].exit_method == "DEGRADED_SNAPSHOT_EXIT"


@pytest.mark.parametrize("value", ["bad json", "{}", '{"data_source":"FAKE"}'])
def test_snapshot_bad_data(tmp_path, value):
    path = tmp_path / "bad.json"
    path.write_text(value)
    with pytest.raises(ValueError):
        SnapshotQuoteProvider(path).get_quotes(["ACME"])


def test_missing_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError):
        SnapshotQuoteProvider(tmp_path / "missing").get_quotes(["ACME"])


def test_pre_entry_quote_never_closes_new_position(tmp_path):
    w, p, _, position, _ = setup_watcher(tmp_path, Quotes(98, NOW - timedelta(seconds=1)))
    w.tick()
    assert len(p.state.open_positions) == 1
    assert position.last_price == position.entry_price


def test_exit_stale_replayed_request_is_noop(tmp_path):
    w, p, _, position, provider = setup_watcher(tmp_path, Quotes(98))
    request = ExitRequest(position.trade_id, position.symbol, "STOP_HIT",
                          provider.get_quotes([position.symbol])[position.symbol], "REALTIME_FAST")
    assert w.executor.execute_exit(request, now=NOW + timedelta(seconds=6)) is None
    assert len(p.state.open_positions) == 1


def test_research_failure_keeps_watcher_alive_with_positions(tmp_path):
    w, p, *_ = setup_watcher(tmp_path)
    class RetryWait:
        def __init__(self):
            self.stopped = False
            self.waited = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, delay):
            assert w.thread.is_alive()
            self.waited = True
            self.stopped = True
    stop = RetryWait()
    loop = SlowMarketLoop(lambda: (3, None), watcher=w,
                          has_positions=lambda: bool(p.snapshot().open_positions),
                          status_path=tmp_path / "slow.json", stop_event=stop)
    assert loop.run() == 0
    assert stop.waited
    assert not w.thread.is_alive()


def test_bar_reconstruction_adverse_stop_first(tmp_path):
    w, p, engine, position, _ = setup_watcher(tmp_path)
    later = NOW + timedelta(minutes=10)
    rows = [candle(NOW + timedelta(minutes=5), low=98, high=106)]
    _, exited, _ = engine.monitor_positions(lambda _: quote(102, candles=rows, as_of=later), now=later)
    assert exited[0]["exit_method"] == "RECONSTRUCTED_FROM_BAR_DATA"
    assert exited[0]["exit_reason"] == "STOP_HIT"
    assert exited[0]["exit_price"] < position.stop
    assert "stop assumed first" in " ".join(exited[0]["warnings"])
    w.tick()
    assert w.metrics["reconstructed_exits"] == 1


def test_newer_fast_mark_not_overwritten_or_closed_by_old_slow_snapshot(tmp_path):
    later = NOW + timedelta(minutes=10)
    w, p, engine, position, _ = setup_watcher(tmp_path, Quotes(103, later), lambda: later)
    w.tick()
    _, exited, _ = engine.monitor_positions(lambda _: quote(90, as_of=NOW), now=NOW)
    assert not exited
    assert position.last_price == pytest.approx(103.01)
    assert len(p.state.open_positions) == 1


def test_fresh_fast_owner_ignores_historical_stop_bar(tmp_path):
    later = NOW + timedelta(minutes=10)
    w, p, engine, position, _ = setup_watcher(tmp_path, Quotes(103, later), lambda: later)
    w.tick()
    rows = [candle(NOW + timedelta(minutes=5), 98, 106)]
    _, exited, _ = engine.monitor_positions(lambda _: quote(103, rows, as_of=later), now=later)
    assert not exited
    assert position.last_price == pytest.approx(103.01)


def test_slow_cycle_blocked_for_simulated_180s_fast_stop_still_runs(tmp_path):
    """Block the actual slow worker; advance its virtual duration by 180s.

    Synchronization (not a 3-minute wall sleep) proves the exit precedes release.
    """
    w, p, _, position, provider = setup_watcher(tmp_path)
    entered, release, exited = Event(), Event(), Event()
    original = w.executor.execute_exit
    def record_exit(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            exited.set()
        return result
    w.executor.execute_exit = record_exit
    elapsed = [0.0]
    def slow():
        entered.set()
        assert release.wait(3)
        elapsed[0] = 180.0
        return 0, {"market_context": {"effective_regular_session": False}}
    loop = SlowMarketLoop(slow, watcher=w, has_positions=lambda: bool(p.snapshot().open_positions),
                          status_path=tmp_path / "slow.json", timer=lambda: elapsed[0])
    thread = Thread(target=loop.run)
    thread.start()
    try:
        assert entered.wait(2)
        provider.price = 98
        assert exited.wait(2), "fast exit blocked by slow research"
        assert not release.is_set()
        assert not p.snapshot().open_positions
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not w.thread.is_alive()
    assert json.loads((tmp_path / "slow.json").read_text())["total_cycle_duration"] == 180


def test_start_to_start_scheduling():
    assert next_cycle_delay(0, 180, 300) == 120
    assert next_cycle_delay(0, 400, 300) == 0
    assert next_cycle_delay(0, 180, 300, False) == 300


def test_clean_keyboard_interrupt_shutdown(tmp_path):
    w, p, *_ = setup_watcher(tmp_path)
    def interrupted():
        raise KeyboardInterrupt()
    assert SlowMarketLoop(interrupted, watcher=w, status_path=tmp_path / "slow.json").run() == 0
    assert not w.thread.is_alive()
    assert json.loads(w.status_path.read_text())["status"] == "STOPPED"


def test_provider_io_does_not_hold_state_lock(tmp_path):
    w, p, _, _, provider = setup_watcher(tmp_path)
    entered, release = Event(), Event()
    original = provider.get_quotes
    def blocked(symbols):
        entered.set()
        assert release.wait(2)
        return original(symbols)
    provider.get_quotes = blocked
    thread = Thread(target=w.tick)
    thread.start()
    try:
        assert entered.wait(1)
        assert p.lock.acquire(timeout=.5)
        p.lock.release()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_atomic_state_concurrent_read_write(tmp_path):
    w, p, *_ = setup_watcher(tmp_path)
    p.save(NOW)
    done = Event()
    errors = []
    def writer():
        try:
            for _ in range(20):
                w.tick()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = Thread(target=writer)
    thread.start()
    while not done.wait(.001):
        assert json.loads(p.state_path.read_text())["schema_version"] == 1
        p.save(NOW)  # Same shared lock, never a second portfolio writer.
    thread.join(2)
    assert not errors
    assert not list(tmp_path.glob(".*.tmp"))


def test_freshness_dashboard_and_status_never_mutate(tmp_path, capsys):
    w, p, *_ = setup_watcher(tmp_path)
    w.tick()
    view = shadow_dashboard_projection(p.state.to_dict(), w.status, {}, now=NOW)
    assert view["fast"]["quote_age"] == 0
    assert view["fast"]["status"] == "ACTIVE"
    old = shadow_dashboard_projection(p.state.to_dict(), w.status, {}, now=NOW + timedelta(minutes=5))
    assert old["fast"]["status"] == "NOT_RUNNING_OR_UNRESPONSIVE"
    assert old["positions"][0]["monitoring_status"] == "PRICE_MONITORING_DEGRADED"
    missing = tmp_path / "no-state"
    assert print_status(missing, missing, missing) == 0
    assert not missing.exists()
    assert "DEGRADED_SNAPSHOT" in capsys.readouterr().out


def test_symbol_limit_marks_unwatched_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FAST_WATCH_MAX_SYMBOLS", 1)
    w, p, engine, _, provider = setup_watcher(tmp_path)
    engine.open_candidate(coordinator("BETA"), quote(), now=NOW)
    w.tick()
    assert provider.calls == [["ACME"]]
    assert w.status["unmonitored_symbols"] == ["BETA"]
    assert w.status["status"] == "DEGRADED"


def test_cutoff_uses_available_early_close(tmp_path):
    provider = Quotes()
    original = provider.get_quotes
    provider.get_quotes = lambda symbols: {s: replace(q, session_close=NOW + timedelta(minutes=4)) for s, q in original(symbols).items()}
    w, p, *_ = setup_watcher(tmp_path, provider)
    w.tick()
    assert p.state.closed_positions[0].exit_reason == "END_OF_DAY_EXIT"


def test_hard_daily_loss_exit(tmp_path):
    w, p, *_ = setup_watcher(tmp_path)
    p.begin_cycle(NOW, [], is_regular_session=True)
    p.state.daily_pnl = -500
    w.tick()
    assert p.state.closed_positions[0].exit_reason == "HARD_RISK_EXIT"


def test_closed_market_no_fast_trade_simulation(tmp_path):
    provider = Quotes(90)
    original = provider.get_quotes
    provider.get_quotes = lambda symbols: {s: replace(q, is_market_open=False) for s, q in original(symbols).items()}
    w, p, *_ = setup_watcher(tmp_path, provider)
    w.tick()
    assert len(p.state.open_positions) == 1
    assert w.status["status"] == "DEGRADED"


def test_event_sampling_and_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FAST_EVENT_LOG_MAX_BYTES", 100)
    w, p, *_ = setup_watcher(tmp_path, Quotes(98, NOW - timedelta(seconds=10)))
    for _ in range(10):
        w.tick()
    # Only one stale and one degraded event, not one per tick.
    all_text = "".join(f.read_text() for f in tmp_path.glob("events.jsonl*"))
    assert all_text.count('"FAST_QUOTE_STALE"') == 1
    for _ in range(10):
        event(w.events_path, "TEST", NOW)
    assert len(list(tmp_path.glob("events.jsonl*"))) == 2


def test_watcher_source_has_no_llm_codex_or_broker_client():
    root = Path(__file__).resolve().parents[1] / "watcher"
    imports = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
    assert not any(any(forbidden in name for forbidden in
                       ("subprocess", "codex", "llm", "robinhood_executor", "requests", "httpx")) for name in imports)
    assert "execution.shadow_executor" in imports
