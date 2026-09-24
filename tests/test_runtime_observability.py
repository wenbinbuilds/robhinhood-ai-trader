from datetime import datetime, timezone
from types import SimpleNamespace
import json

import config
from trading_runtime.observability import (
    RuntimeDashboard,
    depth_aware_bottleneck,
    human_reason,
    render_scalp_drilldown,
)
from watcher.models import FastQuote


NOW = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)


def candidate(reason, stage, score, *, eligible=True):
    return {
        "symbol": "NBIS", "setup_type": "MICRO_BREAKOUT",
        "blocking_stage": stage, "blocking_reasons": [reason],
        "rejection_reasons": [reason], "final": "BLOCKED",
        "stage_flags": {
            "eligible_micro_signals": eligible,
            "signal_score_pass": score is not None and score >= .70,
            "entries": False,
        },
        "signal": {"score": score, "minimum": .70, "components": {}},
        "freshness": {"quote_age_seconds": 1.0, "quote_status": "FRESH"},
        "spread": {"observed_pct": .00045},
        "volume_expansion": {"observed": 1.62},
        "geometry": {"entry_extension_pct": .0096},
        "micro_bars": {}, "episode_lifecycle": {},
    }


def fake_portfolio(open_positions=()):
    state = SimpleNamespace(
        equity=10005.0, cash=9000.0, realized_pnl=3.0,
        unrealized_pnl=2.0, open_positions=list(open_positions),
        closed_positions=[],
    )
    return SimpleNamespace(snapshot=lambda: state)


def test_human_reason_mapping_and_depth_aware_bottleneck():
    early = candidate("VOLUME_EXPANSION_BELOW_MINIMUM", "volume_expansion_pass", .4,
                      eligible=False)
    late = candidate("ENTRY_OVEREXTENDED", "extension_pass", .75)
    diagnostics = {"filtered_reasons": {"VOLUME_EXPANSION_BELOW_MINIMUM": 8}}
    result = depth_aware_bottleneck([early, late], diagnostics)
    assert result["scope"] == "ELIGIBLE_CANDIDATE"
    assert result["reason"] == "ENTRY_OVEREXTENDED"
    assert "too far to chase" in human_reason("ENTRY_OVEREXTENDED")


def test_no_eligible_candidate_reports_universe_filter_separately():
    result = depth_aware_bottleneck([], {
        "filtered_reasons": {"VOLUME_EXPANSION_BELOW_MINIMUM": 6}
    })
    assert result["scope"] == "UNIVERSE_FILTER"
    assert result["stage"] == "before_eligibility"
    assert "no eligible setup" in result["message"]


def test_normal_dashboard_renders_position_and_scalp_candidate(monkeypatch):
    import trading_runtime.observability as module
    monkeypatch.setattr(module, "read_kill_switch", lambda _: SimpleNamespace(trading_blocked=True))
    position = SimpleNamespace(
        symbol="PRGO", strategy="MOMENTUM", strategy_id="MOMENTUM",
        entry_price=14.90, last_price=15.00, stop=14.81, target=15.09,
        dynamic_score=.722, unrealized_pnl=10.0,
        entry_timestamp="2026-09-24T16:30:00+00:00",
        monitoring_status="ACTIVE",
    )
    dashboard = RuntimeDashboard(fake_portfolio([position]))
    view = dashboard.project(
        now=NOW, provider="DIRECT_ROBINHOOD_MCP", provider_mode="REALTIME_FAST",
        watcher_status="ACTIVE",
        quotes={"NBIS": FastQuote("NBIS", 247.2, 247.3, 247.25, NOW, "TEST", True)},
        scalp_result={
            "traces": [candidate("ENTRY_OVEREXTENDED", "extension_pass", .749)],
            "diagnostics": {"funnel": {
                "universe_observations": 12, "quote_fresh": 10,
                "micro_signals_detected_anywhere": 4,
                "eligible_micro_signals": 1, "signal_score_pass": 1,
            }},
        },
    )
    output = dashboard.render(view)
    assert "TRADER — SHADOW MODE" in output
    assert "SHADOW ONLY — LIVE EXECUTION BLOCKED" in output
    assert "PRGO OPEN" in output and "score=0.722" in output
    assert "NBIS MICRO_BREAKOUT score=0.749/0.70" in output
    assert "bottleneck: scope=ELIGIBLE_CANDIDATE" in output


def test_dashboard_suppresses_semantically_identical_poll(monkeypatch):
    import trading_runtime.observability as module
    monkeypatch.setattr(module, "read_kill_switch", lambda _: SimpleNamespace(trading_blocked=True))
    output = []
    dashboard = RuntimeDashboard(fake_portfolio(), output=lambda text, flush: output.append(text))
    kwargs = dict(
        now=NOW, provider="DIRECT", provider_mode="REALTIME_FAST",
        watcher_status="ACTIVE", quotes={},
        scalp_result={"traces": [], "diagnostics": {"funnel": {}}},
    )
    assert dashboard.update(**kwargs) is True
    assert dashboard.update(**{**kwargs, "now": NOW.replace(microsecond=1)}) is False
    assert len(output) == 1


def test_candidate_drilldown_retains_machine_values():
    row = candidate("ENTRY_OVEREXTENDED", "extension_pass", .749)
    row.update({"timestamp": NOW.isoformat(), "episode_id": "episode-1",
                "setup_evidence": ["breakout"]})
    row["signal"].update({
        "score_before_penalties": .949, "total_penalties": .2,
        "components": {"micro_momentum": {
            "raw": .003, "normalized": 1.0, "weight": .25,
            "contribution": .25, "clamp": "MAX",
        }},
    })
    row["geometry"].update({
        "entry": 247.4, "stop": 244.9, "target": 247.72,
        "entry_extension_threshold_pct": .003,
        "entry_extension_reference": {"production_reference_type": "EMA9"},
    })
    text = render_scalp_drilldown(row)
    assert "episode=episode-1" in text
    assert "micro_momentum: raw=0.003" in text
    assert "extension_threshold=0.003" in text
    assert "ENTRY_OVEREXTENDED" in text


def test_candidate_drilldown_cli_reads_latest_persisted_observation(
    tmp_path, monkeypatch, capsys
):
    import runner
    row = candidate("ENTRY_OVEREXTENDED", "extension_pass", .749)
    row.update({"timestamp": NOW.isoformat(), "episode_id": "episode-1",
                "setup_evidence": ["breakout"]})
    path = tmp_path / "diagnostics.jsonl"
    path.write_text(json.dumps({
        "record_type": "CANDIDATE_OBSERVATION", "symbol": "NBIS",
        "payload": row,
    }) + "\n")
    monkeypatch.setattr(config, "SCALP_DIAGNOSTICS_PATH", str(path))
    assert runner.main(["--scalp-drilldown", "NBIS"]) == 0
    output = capsys.readouterr().out
    assert "SCALP DEBUG — NBIS" in output
    assert "ENTRY_OVEREXTENDED" in output
