from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from agent.news_agent import NewsAgent
from shadow.execution import ShadowExecutionEngine
from shadow.models import ShadowTrade
from shadow.performance import ShadowPerformance
from shadow.portfolio import ShadowPortfolio

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


def coordinator(symbol="ACME", news_context=None):
    return {
        "symbol": symbol,
        "decision": "TRADE_CANDIDATE",
        "entry": 101.0,
        "stop": 99.0,
        "target": 105.0,
        "risk_reward_ratio": 2.0,
        "confidence": 0.8,
        "combined_score": 0.8,
        "technical_context": {"technical_score": 0.9},
        "news_context": news_context or {"score": 0.5, "catalyst_type": "NONE", "event_clusters": []},
        "sector_context": {"score": 0.8, "sector": "GENERAL"},
        "market_context": {"score": 0.8, "regime": "BULLISH"},
    }


def quote(price=102.0, candles=None, news_items=None, as_of=NOW):
    return {
        "current_price": price,
        "ask": 101.02,
        "quote_as_of": as_of.isoformat(),
        "candles": candles or [],
        "news_items": news_items or [],
    }


def candle(at, low, high):
    return {
        "begins_at": at.isoformat(), "open": 101, "high": high,
        "low": low, "close": 102, "volume": 1000, "interpolated": False,
    }


def opened(tmp_path: Path, symbol="ACME"):
    portfolio = ShadowPortfolio(tmp_path / "portfolio.json", tmp_path / "trades.jsonl")
    engine = ShadowExecutionEngine(portfolio)
    position, result = engine.open_candidate(coordinator(symbol), quote(), now=NOW)
    assert result["status"] == "OPENED"
    assert position is not None
    return portfolio, engine, position


def benchmark_rows(spy: float, qqq: float) -> list[dict[str, object]]:
    return [
        {"symbol": "SPY", "current_price": spy},
        {"symbol": "QQQ", "current_price": qqq},
    ]


def test_empty_trade_history_is_initialized_safely(tmp_path: Path) -> None:
    trades_path = tmp_path / "nested" / "shadow_trades.jsonl"
    ShadowPortfolio(tmp_path / "shadow.json", trades_path)
    assert trades_path.exists()
    assert trades_path.read_bytes() == b""


def test_existing_trade_history_is_preserved_on_initialization(tmp_path: Path) -> None:
    trades_path = tmp_path / "shadow_trades.jsonl"
    existing = '{"trade_id":"existing"}\n'
    trades_path.write_text(existing, encoding="utf-8")
    ShadowPortfolio(tmp_path / "shadow.json", trades_path)
    assert trades_path.read_text(encoding="utf-8") == existing


def test_benchmark_not_initialized_while_market_closed(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.begin_cycle(
        NOW, benchmark_rows(500.0, 450.0), is_regular_session=False
    )
    assert portfolio.state.trading_date is None
    assert portfolio.state.benchmark_session == {}


def test_benchmark_initializes_once_and_preserves_reference(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.begin_cycle(
        NOW, benchmark_rows(500.0, 450.0), is_regular_session=True
    )
    portfolio.begin_cycle(
        NOW + timedelta(hours=1),
        benchmark_rows(505.0, 455.0),
        is_regular_session=True,
    )
    assert portfolio.state.benchmark_session["SPY"] == {
        "reference_price": 500.0,
        "current_price": 505.0,
    }
    assert portfolio.state.benchmark_session["QQQ"] == {
        "reference_price": 450.0,
        "current_price": 455.0,
    }


def test_benchmark_and_daily_limits_reset_on_next_us_session(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.begin_cycle(
        NOW, benchmark_rows(500.0, 450.0), is_regular_session=True
    )
    portfolio.state.trades_today = 3
    portfolio.state.daily_pnl = -75.0
    next_session = NOW + timedelta(days=1)
    portfolio.begin_cycle(
        next_session, benchmark_rows(510.0, 460.0), is_regular_session=True
    )
    assert portfolio.state.trading_date == "2026-09-10"
    assert portfolio.state.trades_today == 0
    assert portfolio.state.daily_pnl == 0.0
    assert portfolio.state.benchmark_session["SPY"]["reference_price"] == 510.0


def test_utc_midnight_closed_cycle_does_not_reset_us_session(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    regular_cycle = datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc)
    portfolio.begin_cycle(
        regular_cycle, benchmark_rows(500.0, 450.0), is_regular_session=True
    )
    portfolio.state.trades_today = 2
    portfolio.state.daily_pnl = -25.0
    after_utc_midnight_same_new_york_date = datetime(
        2026, 9, 10, 0, 30, tzinfo=timezone.utc
    )
    portfolio.begin_cycle(
        after_utc_midnight_same_new_york_date,
        benchmark_rows(501.0, 451.0),
        is_regular_session=False,
    )
    assert portfolio.state.trading_date == "2026-09-09"
    assert portfolio.state.trades_today == 2
    assert portfolio.state.daily_pnl == -25.0
    assert portfolio.state.benchmark_session["SPY"] == {
        "reference_price": 500.0,
        "current_price": 500.0,
    }


def test_valid_shadow_entry_persists_local_position(tmp_path: Path) -> None:
    portfolio, _, position = opened(tmp_path)
    assert portfolio.state.open_positions == [position]
    assert position.quantity > 0
    assert position.maximum_theoretical_loss > 0


def test_risk_manager_rejection(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.state.daily_pnl = -config.SHADOW_STARTING_CAPITAL * config.MAX_DAILY_LOSS_PERCENT
    position, result = ShadowExecutionEngine(portfolio).open_candidate(coordinator(), quote(), now=NOW)
    assert position is None
    assert result["reason"] == "DAILY_LOSS_LIMIT"


def test_simulated_entry_slippage_is_not_below_ask(tmp_path: Path) -> None:
    _, _, position = opened(tmp_path)
    assert position.entry_price > 101.02


def test_stale_entry_data_is_rejected(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    stale = quote(as_of=NOW - timedelta(minutes=5))
    position, result = ShadowExecutionEngine(portfolio).open_candidate(
        coordinator(), stale, now=NOW
    )
    assert position is None
    assert result["reason"] == "STALE_DATA"


@pytest.mark.parametrize(
    ("low", "high", "reason"),
    [(98.5, 102, "STOP_HIT"), (100, 105.5, "TARGET_HIT"), (98.5, 105.5, "STOP_HIT")],
)
def test_stop_target_and_same_bar_policy(tmp_path: Path, low: float, high: float, reason: str) -> None:
    portfolio, engine, _ = opened(tmp_path)
    rows = [candle(NOW + timedelta(minutes=5), low, high)]
    checked = NOW + timedelta(minutes=10)
    _, exited, _ = engine.monitor_positions(lambda _: quote(candles=rows, as_of=checked), now=checked)
    assert exited[0]["exit_reason"] == reason
    if low <= 99 and high >= 105:
        assert "stop assumed first" in exited[0]["warnings"][0]


def test_forced_end_of_day_exit(tmp_path: Path) -> None:
    _, engine, _ = opened(tmp_path)
    closing = datetime(2026, 9, 9, 19, 56, tzinfo=timezone.utc)
    _, exited, _ = engine.monitor_positions(lambda _: quote(102, as_of=closing), now=closing)
    assert exited[0]["exit_reason"] == "END_OF_DAY_EXIT"


def test_no_new_position_in_no_overnight_window(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    closing = datetime(2026, 9, 9, 19, 56, tzinfo=timezone.utc)
    position, result = ShadowExecutionEngine(portfolio).open_candidate(
        coordinator(), quote(), now=closing
    )
    assert position is None
    assert result["reason"] == "MARKET_CLOSING"


def test_position_carried_past_entry_date_exits_at_first_fresh_mark(tmp_path: Path) -> None:
    portfolio, engine, _ = opened(tmp_path)
    next_session = NOW + timedelta(days=1)
    evaluated, exited, _ = engine.monitor_positions(
        lambda _: quote(102, as_of=next_session), now=next_session
    )
    assert evaluated[0]["status"] == "MISSED_EOD_RECOVERY_EXIT"
    assert exited[0]["exit_reason"] == "MISSED_EOD_RECOVERY_EXIT"
    assert not portfolio.state.open_positions


def test_objective_technical_invalidation_exit(tmp_path: Path) -> None:
    _, engine, _ = opened(tmp_path)
    checked = NOW + timedelta(minutes=10)
    payload = quote(100.0, as_of=checked)
    payload.update({"vwap": 101.0, "ema9": 100.0, "ema20": 100.5})
    evaluated, exited, _ = engine.monitor_positions(lambda _: payload, now=checked)
    assert evaluated[0]["technical_invalidation"] is True
    assert exited[0]["exit_reason"] == "INVALIDATED"
    assert "momentum invalidated" in exited[0]["warnings"][0]


def test_maximum_positions(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    engine = ShadowExecutionEngine(portfolio)
    for symbol in ("AAA", "BBB"):
        assert engine.open_candidate(coordinator(symbol), quote(), now=NOW)[0] is not None
    position, result = engine.open_candidate(coordinator("CCC"), quote(), now=NOW)
    assert position is None
    assert result["reason"] == "MAX_POSITIONS"


def test_maximum_trades(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.state.trades_today = config.MAX_TRADES_PER_DAY
    position, result = ShadowExecutionEngine(portfolio).open_candidate(coordinator(), quote(), now=NOW)
    assert position is None
    assert result["reason"] == "MAX_TRADES"


def test_daily_loss_shutdown(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.state.daily_pnl = -250
    position, result = ShadowExecutionEngine(portfolio).open_candidate(coordinator(), quote(), now=NOW)
    assert position is None
    assert result["reason"] == "DAILY_LOSS_LIMIT"


def test_duplicate_symbol_rejection(tmp_path: Path) -> None:
    portfolio, engine, _ = opened(tmp_path)
    position, result = engine.open_candidate(coordinator(), quote(), now=NOW)
    assert position is None
    assert result["reason"] == "DUPLICATE_POSITION"


def test_state_persistence_and_restart(tmp_path: Path) -> None:
    portfolio, _, position = opened(tmp_path)
    portfolio.save(NOW)
    restored = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    assert restored.state.open_positions[0].trade_id == position.trade_id
    assert restored.has_symbol("ACME")


def test_atomic_state_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    calls = []
    real_replace = __import__("os").replace
    monkeypatch.setattr("shadow.portfolio.os.replace", lambda src, dst: (calls.append((src, dst)), real_replace(src, dst))[1])
    portfolio.save(NOW)
    assert len(calls) == 1
    assert Path(calls[0][1]) == portfolio.state_path


def test_unrealized_and_realized_pnl(tmp_path: Path) -> None:
    portfolio, engine, position = opened(tmp_path)
    portfolio.revalue({"ACME": position.entry_price + 2})
    assert portfolio.state.unrealized_pnl == pytest.approx(2 * position.quantity)
    rows = [candle(NOW + timedelta(minutes=5), 100, 106)]
    checked = NOW + timedelta(minutes=10)
    engine.monitor_positions(lambda _: quote(candles=rows, as_of=checked), now=checked)
    assert portfolio.state.realized_pnl > 0
    assert portfolio.state.unrealized_pnl == 0


def trade(pnl: float, trade_id: str) -> ShadowTrade:
    return ShadowTrade(
        trade_id=trade_id, symbol="ACME", strategy="INTRADAY_MOMENTUM_V1",
        sector="GENERAL", entry_timestamp=NOW.isoformat(), entry_price=100,
        quantity=1, stop=99, target=102, risk_reward_ratio=2,
        exit_timestamp=(NOW + timedelta(minutes=30)).isoformat(),
        exit_price=100 + pnl, exit_reason="TARGET_HIT" if pnl > 0 else "STOP_HIT",
        gross_pnl=pnl, estimated_slippage_cost=0, net_pnl=pnl,
        return_percent=pnl, holding_time_minutes=30,
        coordinator_confidence=0.8, coordinator_score=0.8,
        technical_score=0.9, news_score=0.5, sector_score=0.5,
        market_score=0.8, catalyst_type="NONE", market_regime="BULLISH",
    )


def test_win_rate_profit_factor_and_expectancy(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    portfolio.state.closed_positions = [trade(10, "a"), trade(-5, "b")]
    summary = ShadowPerformance().summarize(portfolio.state)
    assert summary["win_rate"] == 0.5
    assert summary["profit_factor"] == 2
    assert summary["expectancy_per_trade"] == 2.5


def test_maximum_drawdown(tmp_path: Path) -> None:
    portfolio, _, position = opened(tmp_path)
    portfolio.revalue({"ACME": position.entry_price - 5})
    assert portfolio.state.maximum_drawdown > 0
    assert portfolio.state.maximum_drawdown_percent > 0


def test_no_lookahead_ignores_pre_entry_bar(tmp_path: Path) -> None:
    portfolio, engine, _ = opened(tmp_path)
    rows = [candle(NOW - timedelta(minutes=5), 90, 110)]
    evaluated, exited, _ = engine.monitor_positions(lambda _: quote(102, rows), now=NOW + timedelta(minutes=1))
    assert not exited
    assert evaluated[0]["status"] == "OPEN"
    assert portfolio.has_symbol("ACME")


def test_new_pre_entry_news_is_deduplicated_after_entry(tmp_path: Path) -> None:
    raw = [{
        "headline": "ACME earnings beat estimates", "summary": "ACME earnings beat estimates",
        "source": "Wire", "published_at": (NOW - timedelta(minutes=5)).isoformat(),
        "url": "https://example.test/a", "source_quality": "MAJOR_WIRE",
        "catalyst_type": "EARNINGS", "sentiment": "POSITIVE", "importance": 0.9,
    }]
    news = NewsAgent().analyze("ACME", raw, now=NOW).to_dict()
    portfolio = ShadowPortfolio(tmp_path / "p.json", tmp_path / "t.jsonl")
    engine = ShadowExecutionEngine(portfolio)
    assert engine.open_candidate(coordinator(news_context=news), quote(), now=NOW)[0] is not None
    checked = NOW + timedelta(minutes=10)
    evaluated, exited, _ = engine.monitor_positions(lambda _: quote(102, news_items=raw, as_of=checked), now=checked)
    assert not exited
    assert evaluated[0]["status"] == "OPEN"


def test_shadow_engine_contains_no_robinhood_order_calls() -> None:
    names = set(dir(ShadowExecutionEngine))
    forbidden = ("place", "preview", "review", "modify", "cancel", "replace", "submit")
    assert not any(any(word in name.lower() for word in forbidden) for name in names)
