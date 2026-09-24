from pathlib import Path
from typing import Any

import config
import runner
import pytest
from shadow.portfolio import ShadowPortfolio
from agent.codex_mcp_bridge import RefreshResult
from contextlib import nullcontext


@pytest.fixture(autouse=True)
def isolated_runner_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(runner, "SHADOW_STATE", tmp_path / "state" / "shadow_portfolio.json")
    monkeypatch.setattr(runner, "SHADOW_TRADES", tmp_path / "state" / "shadow_trades.jsonl")


def test_once_always_runs_refresh_before_analysis(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "ROBINHOOD_DATA_PROVIDER", "LEGACY_CODEX_MCP")
    calls: list[Path] = []

    def fake_refresh(snapshot: Path, portfolio=None, **kwargs) -> tuple[int, dict[str, object] | None]:
        calls.append(snapshot)
        return 0, {"decision": {"type": "NO_TRADE"}}

    monkeypatch.setattr(runner, "refresh_and_run", fake_refresh)

    assert runner.main(["--once", "--snapshot", str(tmp_path / "snapshot.json")]) == 0
    assert calls == [(tmp_path / "snapshot.json").resolve()]


def test_loop_refreshes_before_stopping_on_closed_snapshot(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "ROBINHOOD_DATA_PROVIDER", "LEGACY_CODEX_MCP")
    calls: list[Path] = []

    def fake_refresh(snapshot: Path, portfolio=None, **kwargs) -> tuple[int, dict[str, object] | None]:
        calls.append(snapshot)
        return 0, {
            "decision": {"type": "ANALYSIS_SKIPPED"},
            "market_context": {"effective_regular_session": False},
        }

    monkeypatch.setattr(runner, "refresh_and_run", fake_refresh)

    assert runner.main(["--loop", "--snapshot", str(tmp_path / "snapshot.json")]) == 0
    assert calls == [(tmp_path / "snapshot.json").resolve()]


def test_shadow_summary_never_refreshes_robinhood(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    state = tmp_path / "shadow.json"
    trades = tmp_path / "trades.jsonl"
    ShadowPortfolio(state, trades).save()
    monkeypatch.setattr(runner, "SHADOW_STATE", state)
    monkeypatch.setattr(runner, "SHADOW_TRADES", trades)
    monkeypatch.setattr(
        runner, "refresh_and_run", lambda _: (_ for _ in ()).throw(AssertionError())
    )

    assert runner.main(["--shadow-summary"]) == 0
    assert "SHADOW TRADING SUMMARY" in capsys.readouterr().out


def test_reset_shadow_requires_exact_confirmation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state = tmp_path / "shadow.json"
    trades = tmp_path / "trades.jsonl"
    state.write_text("{}", encoding="utf-8")
    trades.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runner, "SHADOW_STATE", state)
    monkeypatch.setattr(runner, "SHADOW_TRADES", trades)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert runner.main(["--reset-shadow"]) == 1
    assert state.exists() and trades.exists()

    monkeypatch.setattr("builtins.input", lambda _: "RESET SHADOW")
    assert runner.main(["--reset-shadow"]) == 0
    assert not state.exists() and not trades.exists()


def test_unknown_mode_fails_closed(monkeypatch: Any) -> None:
    monkeypatch.setattr(config, "MODE", "UNKNOWN")
    assert runner.main(["--once"]) == 2


def test_scalp_shadow_override_is_loop_only_and_does_not_change_default(monkeypatch, tmp_path):
    assert runner.main(["--once", "--scalp-shadow"]) == 2
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    assert runner.main(["--loop", "--scalp-shadow"]) == 2
    assert config.SCALP_ENABLED is False


def test_scalp_status_reports_discovery_limits_and_safety(tmp_path, capsys):
    assert runner.main(["--scalp-status"]) == 0
    payload = __import__('json').loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["candidate_discovery_source"] == config.SCALP_DISCOVERY_SOURCE
    assert payload["max_quote_age_seconds"] == config.SCALP_MAX_QUOTE_AGE_SECONDS
    assert payload["max_trades_per_symbol"] == config.SCALP_MAX_TRADES_PER_SYMBOL
    assert payload["diagnostics_path"] == config.SCALP_DIAGNOSTICS_PATH
    assert payload["debug_runtime_override"].endswith("--scalp-debug")
    assert payload["safety"]["trading_blocked"] is True
    assert payload["live_supported"] is False


def test_scalp_debug_requires_enabled_shadow_loop(capsys):
    assert runner.main(["--loop", "--scalp-debug"]) == 2
    assert "requires an enabled shadow scalp --loop" in capsys.readouterr().err


def test_scalp_summary_includes_observation_funnel_without_refresh(monkeypatch, capsys):
    monkeypatch.setattr(
        runner, "refresh_and_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    )
    assert runner.main(["--scalp-summary"]) == 0
    payload = __import__('json').loads(capsys.readouterr().out)
    assert payload['session']['candidate_observations'] == 0
    assert 'rates' in payload['session']
    assert payload['performance']['strategy_id'] == 'SCALP'


def test_loop_scalp_shadow_override_reaches_runtime_without_changing_config(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(config, "ROBINHOOD_DATA_PROVIDER", "LEGACY_CODEX_MCP")
    monkeypatch.setattr(config, "FAST_WATCHER_ENABLED", False)
    calls = []

    def failed_cycle(snapshot, portfolio=None, **kwargs):
        calls.append(kwargs.get("scalp_enabled"))
        return 3, None

    monkeypatch.setattr(runner, "refresh_and_run", failed_cycle)
    assert runner.main([
        "--loop", "--scalp-shadow", "--snapshot", str(tmp_path/'snapshot.json')
    ]) == 3
    assert calls == [True]
    assert config.SCALP_ENABLED is False
    output = capsys.readouterr().out
    assert "TRADER — SHADOW MODE" in output
    assert "SHADOW ONLY" in output and "LIVE EXECUTION BLOCKED" in output


def test_status_has_no_side_effects(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "refresh_and_run", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert runner.main(["--shadow-status"]) == 0
    assert "DEGRADED_SNAPSHOT" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_whole_runner_lock_excludes_second_runner(monkeypatch, tmp_path):
    from agent.codex_mcp_bridge import LocalCycleLock
    monkeypatch.setattr(runner, "refresh_and_run", lambda *_: (_ for _ in ()).throw(AssertionError()))
    with LocalCycleLock(tmp_path / "state" / "shadow_runner.lock"):
        assert runner.main(["--once"]) == 3


def test_benchmark_failure_preserves_core_and_reports_infrastructure_blocked(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(config, "ROBINHOOD_DATA_PROVIDER", "LEGACY_CODEX_MCP")
    snapshot_path = tmp_path / "state" / "market_snapshot.json"
    value = {
        "data_source": "ROBINHOOD_MCP", "mcp_status": "CONNECTED",
        "account": {"is_agentic_account": True},
        "portfolio": {"portfolio_value": "10000", "buying_power": "5000"},
        "positions": [], "market": {"status": "OPEN"},
        "scanner": {"status": "OK", "scan_name": config.PROJECT_SCANNER_NAME,
                    "lifecycle_action": "REUSED", "result_count": 13},
        "connectivity_checks": {},
    }
    class Bridge:
        custom = None
        def __init__(self, **kwargs): pass
        def cycle_lock(self): return nullcontext()
        def refresh(self, **kwargs):
            return RefreshResult(value, snapshot_path, "BENCHMARK_FAILED", True, True, True, 0)
    monkeypatch.setattr(runner, "CodexMcpBridge", Bridge)
    code, result = runner.refresh_and_run(snapshot_path)
    output = capsys.readouterr().out
    assert code == 3 and result is None
    assert "MCP STATUS: CONNECTED" in output
    assert "AGENTIC ACCOUNT: FOUND" in output
    assert "SCANNER: OK" in output
    assert "SCAN RESULTS: 13" in output
    assert "BENCHMARK: FAILED" in output
    assert "ANALYSIS STATUS: INFRASTRUCTURE_BLOCKED" in output
