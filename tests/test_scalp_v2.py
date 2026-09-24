from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import config
import pytest
from strategies.scalp_v2.research import CausalQuoteBuffer, HybridScalpResearchEngine


NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)


def trace(at, *, mid=100.0, setup="EMA9_CONTINUATION", episode="V2-EPISODE"):
    bid, ask = mid - .01, mid + .01
    return {
        "timestamp": at.isoformat(), "symbol": "AMD", "episode_id": episode,
        "setup_type": setup, "final": "BLOCKED",
        "stage_flags": {
            "quote_fresh": True, "valid_bid_ask": True, "spread_pass": True,
            "micro_bars_pass": True, "volume_expansion_pass": True,
            "eligible_micro_signals": True,
        },
        "freshness": {
            "quote_age_seconds": .1,
            "provenance": {
                "exchange_timestamp": at.isoformat(), "bid": bid, "ask": ask,
                "mid": mid, "spread_pct": (ask - bid) / mid,
            },
        },
        "spread": {"observed_pct": (ask - bid) / mid},
        "volume_expansion": {"passed": True, "status": "PASS", "observed": 1.3},
        "micro_bars": {"latest_completed_bar_timestamp": (at - timedelta(minutes=5)).isoformat()},
        "signal": {"score": .50, "components": {"relative_strength": {"raw": .001}}},
        "geometry": {"structural_levels": {"ema9": 100.0, "vwap": 99.5, "recent_low": 99.8}},
        "episode_lifecycle": {
            "original_anchor_price": 100.0,
            "fingerprint_fields": {"ema20": 99.7},
        },
    }


def safe_engine(tmp_path, monkeypatch, *, persist=False):
    switch = tmp_path / "kill.json"
    switch.write_text(json.dumps({"trading_blocked": True}))
    monkeypatch.setattr(config, "LIVE_KILL_SWITCH_PATH", str(switch))
    return HybridScalpResearchEngine(
        state_path=tmp_path / "v2.json", events_path=tmp_path / "v2.jsonl",
        persist=persist,
    )


def test_quote_buffer_uses_only_points_available_at_observation_time():
    buffer = CausalQuoteBuffer()
    first = trace(NOW, mid=100.0)
    second = trace(NOW + timedelta(seconds=5), mid=99.9)
    future = trace(NOW + timedelta(seconds=10), mid=100.1)
    buffer.observe(first)
    buffer.observe(second)
    before = buffer.features("AMD")
    assert before["return_5s"] == pytest.approx(-0.001)
    assert before["acceleration_5s"] is None
    buffer.observe(future)
    after = buffer.features("AMD")
    assert after["return_5s"] > 0
    assert after["acceleration_5s"] > 0


def test_v2_arms_then_uses_fast_trigger_without_mutating_v1_trace(tmp_path, monkeypatch):
    engine = safe_engine(tmp_path, monkeypatch)
    observations = [
        trace(NOW, mid=100.0),
        trace(NOW + timedelta(seconds=5), mid=99.95),
        trace(NOW + timedelta(seconds=10), mid=100.1),
    ]
    original = deepcopy(observations)
    results = [
        engine.observe([row], now=datetime.fromisoformat(row["timestamp"]), persist=False)
        for row in observations
    ]
    assert observations == original
    assert results[0]["armed"] == 1
    final = results[-1]["observations"][0]
    assert final["selected_trigger"] == "EMA9_TURN_UP"
    assert final["fast_trigger"] is True
    assert final["v1_score"] == .50
    assert final["v2_decision"] in {"V2_ENTRY_READY", "V2_TRIGGER_GEOMETRY_BLOCKED"}


def test_v2_state_and_research_pnl_are_separate_files(tmp_path, monkeypatch):
    engine = safe_engine(tmp_path, monkeypatch, persist=True)
    result = engine.observe([trace(NOW)], now=NOW)
    state = json.loads((tmp_path / "v2.json").read_text())
    assert result["research_only"] is True
    assert state["strategy_id"] == config.SCALP_V2_STRATEGY_ID
    assert state["research_only"] is True
    assert state["open_positions"] == []
    assert state["closed_trades"] == []


def test_v2_uses_deterministic_scalp_risk_limits_on_isolated_capital(
        tmp_path, monkeypatch):
    engine = safe_engine(tmp_path, monkeypatch)
    risk = engine._risk("AMD", {"entry": 100.0, "stop": 99.9}, NOW)
    assert risk["approved"] is True
    assert risk["max_shares"] >= 1
    assert risk["starting_capital"] == config.SHADOW_STARTING_CAPITAL
    assert risk["research_equity"] == config.SHADOW_STARTING_CAPITAL
    assert risk["duplicate_symbol"] is False


def test_v2_refuses_unblocked_kill_switch(tmp_path, monkeypatch):
    switch = tmp_path / "kill.json"
    switch.write_text(json.dumps({"trading_blocked": False}))
    monkeypatch.setattr(config, "LIVE_KILL_SWITCH_PATH", str(switch))
    try:
        HybridScalpResearchEngine(
            state_path=tmp_path / "v2.json", events_path=tmp_path / "v2.jsonl"
        )
    except ValueError as exc:
        assert "blocked kill switch" in str(exc)
    else:
        raise AssertionError("V2 must fail closed when the kill switch is unblocked")
