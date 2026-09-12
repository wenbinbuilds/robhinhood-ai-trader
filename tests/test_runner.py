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
