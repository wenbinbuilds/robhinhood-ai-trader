import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import config
from agent.market_cycle import (
    AccountNotIdentifiedError,
    JsonSnapshotProvider,
    McpNotAuthenticatedError,
    McpUnavailableError,
    MissingSnapshotError,
    RobinhoodDataError,
    StaleSnapshotError,
    MarketCycle,
)
from agent.codex_mcp_bridge import CodexMcpBridge, CodexSnapshotRefresher

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


def normalized_scanner_criteria() -> list[dict[str, Any]]:
    return [
        {
            "filter_type": item["filter_type"],
            "filter_type_enum": item["filter_type"],
            "predicate": item["predicate"],
            "values": list(item["values"]),
            "interval": item.get("interval"),
            "length": item.get("length"),
            "plot": item.get("plot"),
            "expression": None,
        }
        for item in config.SCANNER_CRITERIA
    ]


def snapshot(
    *,
    generated_at: datetime = NOW,
    mcp_status: str = "CONNECTED",
    market_open: bool = False,
    account_found: bool = True,
) -> dict[str, Any]:
    diagnostic = {
        "symbol": "NVDA",
        "current_price": 180.0,
        "bid": 179.99,
        "ask": 180.01,
        "quote_as_of": generated_at.isoformat(),
        "volume": None,
        "relative_volume": None,
        "vwap": None,
        "ema9": None,
        "ema20": None,
        "rsi14": None,
        "macd": None,
        "macd_signal": None,
        "macd_histogram": None,
        "intraday_support_reference": None,
        "intraday_resistance_reference": None,
        "intraday_high": None,
        "intraday_low": None,
        "previous_close": 178.0,
        "market_direction": "UNKNOWN",
        "sector": None,
        "industry": None,
        "sector_benchmark": None,
        "news_items": [],
        "candles": [],
        "level2": None,
        "unavailable_values": ["vwap"],
    }
    return {
        "data_source": "ROBINHOOD_MCP",
        "generated_at": generated_at.isoformat(),
        "mcp_status": mcp_status,
        "mcp_access_path": "CUSTOM_MCP",
        "status_detail": "real read-only Robinhood data retrieved",
        "account": {
            "is_agentic_account": account_found,
            "nickname": "Agentic" if account_found else None,
            "account_type": "individual" if account_found else None,
            "brokerage_trading_type": "limited_margin" if account_found else None,
            "state": "active" if account_found else None,
        },
        "portfolio": {
            "portfolio_value": "100000.00",
            "buying_power": "20000.00",
            "unleveraged_buying_power": "20000.00",
            "cash": "20000.00",
            "equity_value": "80000.00",
            "currency": "USD",
        },
        "positions": [],
        "open_orders": [],
        "daily_realized_pnl": "0.00",
        "market": {
            "status": "OPEN" if market_open else "CLOSED",
            "is_regular_session": market_open,
            "as_of": generated_at.isoformat(),
            "direction": "UNKNOWN",
            "benchmarks": [],
            "volatility_context": None,
        },
        "scanner": {
            "status": "OK",
            "scan_name": config.PROJECT_SCANNER_NAME,
            "scan_id": "safe-scan-id",
            "lifecycle_action": "REUSED",
            "criteria": normalized_scanner_criteria() if market_open else [],
            "sort_configuration": dict(config.SCANNER_SORT),
            "result_count": 0,
            "candidates": [],
        },
        "candidate_data": [],
        "shadow_position_data": [],
        "connectivity_checks": {
            "NVDA": {
                "status": "OK",
                "detail": "real quote retrieved",
                "market_data": diagnostic,
            }
        },
        "warnings": [],
        "errors": [],
    }


def write_snapshot(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_valid_fresh_robinhood_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot())

    provider = JsonSnapshotProvider.from_path(path, now=NOW)

    assert provider.get_snapshot_metadata()["mcp_status"] == "CONNECTED"
    assert provider.get_account()["is_agentic_account"] is True
    assert provider.get_candidate_data("NVDA") == {}


def test_stale_snapshot_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(
        path,
        snapshot(
            generated_at=NOW
            - timedelta(seconds=config.SNAPSHOT_MAX_AGE_SECONDS + 1)
        ),
    )

    with pytest.raises(StaleSnapshotError):
        JsonSnapshotProvider.from_path(path, now=NOW)


def test_missing_snapshot_is_reported_separately(tmp_path: Path) -> None:
    with pytest.raises(MissingSnapshotError):
        JsonSnapshotProvider.from_path(tmp_path / "missing.json", now=NOW)


def test_mcp_unavailable_is_not_treated_as_strategy_decision(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot(mcp_status="MCP_UNAVAILABLE"))

    with pytest.raises(McpUnavailableError):
        JsonSnapshotProvider.from_path(path, now=NOW)


def test_mcp_not_authenticated_is_reported_separately(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot(mcp_status="MCP_NOT_AUTHENTICATED"))

    with pytest.raises(McpNotAuthenticatedError):
        JsonSnapshotProvider.from_path(path, now=NOW)


def test_market_closed_snapshot_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot(market_open=False))

    provider = JsonSnapshotProvider.from_path(path, now=NOW)

    assert provider.get_market_context()["status"] == "CLOSED"


def test_scanner_must_be_skipped_when_market_is_closed(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    value = snapshot(market_open=False)
    value["scanner"]["status"] = "RUN_FAILED"
    write_snapshot(path, value)

    with pytest.raises(RobinhoodDataError, match="closed-market scanner"):
        JsonSnapshotProvider.from_path(path, now=NOW)


def test_account_not_identified_is_reported_separately(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot(account_found=False))

    with pytest.raises(AccountNotIdentifiedError):
        JsonSnapshotProvider.from_path(path, now=NOW)


def test_refresher_records_mcp_unavailable_when_codex_is_missing(
    tmp_path: Path,
) -> None:
    refresher = CodexSnapshotRefresher(
        project_dir=Path.cwd(),
        snapshot_path=tmp_path / "snapshot.json",
        lock_path=tmp_path / "cycle.lock",
        which=lambda _: None,
        clock=lambda: NOW,
    )

    result = refresher.refresh()

    assert result.snapshot["mcp_status"] == "MCP_UNAVAILABLE"
    assert result.connected is False


def test_refresher_records_not_authenticated_without_running_exec(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Not logged in")

    refresher = CodexSnapshotRefresher(
        project_dir=Path.cwd(),
        snapshot_path=tmp_path / "snapshot.json",
        lock_path=tmp_path / "auth-cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    )

    result = refresher.refresh()

    assert result.snapshot["mcp_status"] == "MCP_NOT_AUTHENTICATED"
    assert all("exec" not in command for command in calls)


def test_refresher_uses_read_only_codex_exec_and_normalizes_output(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    cli_snapshot = snapshot()

    def fake_run(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "Name Url Status Auth\n"
                "robinhood-trading https://agent.robinhood.com/mcp/trading enabled Unknown\n",
                "",
            )
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(json.dumps(cli_snapshot), encoding="utf-8")
        assert kwargs["input"] is not None
        assert "Do not preview, review, place, replace, modify, cancel, or submit" in kwargs["input"]
        assert "AI_INTRADAY_MOMENTUM_V1" in kwargs["input"]
        assert "create_scan" in kwargs["input"]
        assert "update_scan_filters" in kwargs["input"]
        assert "run_scan" in kwargs["input"]
        return subprocess.CompletedProcess(command, 0, "", "")

    output = tmp_path / "snapshot.json"
    refresher = CodexSnapshotRefresher(
        project_dir=Path.cwd(),
        snapshot_path=output,
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    )

    result = refresher.refresh()

    exec_command = next(command for command in calls if "exec" in command)
    assert "--approve-for-me" in exec_command
    assert "--sandbox" not in exec_command
    config_values = [
        exec_command[index + 1]
        for index, value in enumerate(exec_command)
        if value == "--config"
    ]
    assert f'model_reasoning_effort="{config.CODEX_REASONING_EFFORT}"' in config_values
    allowlist_value = next(
        value for value in config_values
        if value.startswith("mcp_servers.robinhood-trading.enabled_tools=")
    )
    for allowed in (
        "get_accounts",
        "get_equity_quotes",
        "get_equity_orders",
        "get_scans",
        "create_scan",
        "update_scan_filters",
        "update_scan_config",
        "run_scan",
    ):
        assert f'"{allowed}"' in allowlist_value
    for forbidden in (
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        "review_option_order",
        "place_option_order",
        "cancel_option_order",
        "preview_crypto_order",
        "place_crypto_order",
        "cancel_crypto_order",
    ):
        assert forbidden not in allowlist_value
    assert "--output-schema" in exec_command
    assert "--output-last-message" in exec_command
    assert result.connected is True
    assert result.custom_mcp_configured is True
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["candidate_data"] == []
    assert stored["connectivity_checks"]["NVDA"]["market_data"]["current_price"] == 180.0


def test_bridge_rejects_unsafe_mcp_allowlist_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config,
        "ROBINHOOD_MCP_ENABLED_TOOLS",
        ("get_accounts", "place_equity_order"),
    )
    bridge = CodexMcpBridge(project_dir=Path.cwd())
    with pytest.raises(RuntimeError, match="unsafe or empty"):
        bridge.build_exec_command("codex", tmp_path / "output.json")


def test_snapshot_generated_at_is_atomic_persistence_time_after_long_collection(
    tmp_path: Path,
) -> None:
    current = [NOW]
    cli_snapshot = snapshot(generated_at=NOW)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(cli_snapshot), encoding="utf-8"
        )
        current[0] = NOW + timedelta(seconds=245)
        return subprocess.CompletedProcess(command, 0, "", "")

    output = tmp_path / "snapshot.json"
    result = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=output,
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: current[0],
    ).refresh()

    assert result.connected is True
    assert json.loads(output.read_text(encoding="utf-8"))["generated_at"] == (
        NOW + timedelta(seconds=245)
    ).isoformat().replace("+00:00", "Z")


def configured_result(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        0,
        "Name Url Status Auth\n"
        "robinhood-trading https://agent.robinhood.com/mcp/trading enabled Unknown\n",
        "",
    )


def run_bridge_output(tmp_path: Path, value: dict[str, Any]):
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(json.dumps(value), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    return CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=tmp_path / "snapshot.json",
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    ).refresh()


@pytest.mark.parametrize("action", ["REUSED", "CREATED", "UPDATED"])
def test_project_scanner_lifecycle_actions_are_accepted(
    tmp_path: Path, action: str
) -> None:
    value = snapshot(market_open=True)
    value["scanner"]["lifecycle_action"] = action

    result = run_bridge_output(tmp_path, value)

    assert result.connected is True
    assert result.snapshot["scanner"]["scan_name"] == config.PROJECT_SCANNER_NAME
    assert result.snapshot["scanner"]["lifecycle_action"] == action


def test_scanner_creation_failure_fails_closed(tmp_path: Path) -> None:
    value = snapshot(mcp_status="ROBINHOOD_ERROR", market_open=True)
    value["scanner"].update(
        status="CREATE_FAILED", lifecycle_action="FAILED", result_count=0
    )

    result = run_bridge_output(tmp_path, value)

    assert result.connected is False
    assert result.snapshot["scanner"]["status"] == "CREATE_FAILED"


def test_scanner_execution_failure_fails_closed(tmp_path: Path) -> None:
    value = snapshot(mcp_status="ROBINHOOD_ERROR", market_open=True)
    value["scanner"].update(
        status="RUN_FAILED", lifecycle_action="REUSED", result_count=0
    )

    result = run_bridge_output(tmp_path, value)

    assert result.connected is False
    assert result.snapshot["scanner"]["status"] == "RUN_FAILED"


def test_zero_scanner_results_is_valid_no_trade_and_excludes_diagnostic(
    tmp_path: Path,
) -> None:
    value = snapshot(market_open=True)
    result = run_bridge_output(tmp_path, value)
    provider = JsonSnapshotProvider.from_path(
        tmp_path / "snapshot.json", now=NOW
    )
    cycle = MarketCycle(
        provider,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
    ).run()

    assert result.connected is True
    assert result.snapshot["scanner"]["result_count"] == 0
    assert result.snapshot["candidate_data"] == []
    assert cycle["analyzed_candidates"] == []
    assert cycle["decision"]["type"] == "NO_TRADE"


def test_scanner_candidate_limit_is_enforced(tmp_path: Path) -> None:
    value = snapshot(market_open=True)
    candidates = []
    deep = []
    diagnostic = value["connectivity_checks"]["NVDA"]["market_data"]
    for index in range(config.MAX_CANDIDATES_TO_ANALYZE + 1):
        symbol = f"T{index:02d}"
        candidates.append(
            {"symbol": symbol, "instrument_type": "EQUITY", "columns": []}
        )
        item = dict(diagnostic)
        item["symbol"] = symbol
        deep.append(item)
    value["scanner"]["candidates"] = candidates
    value["scanner"]["result_count"] = len(candidates)
    value["candidate_data"] = deep

    result = run_bridge_output(tmp_path, value)

    assert result.connected is False
    assert result.bridge_status == "INVALID_MODEL_OUTPUT"


def test_connectivity_symbol_must_be_scanner_returned_to_enter_candidate_data(
    tmp_path: Path,
) -> None:
    value = snapshot(market_open=True)
    value["candidate_data"] = [
        value["connectivity_checks"]["NVDA"]["market_data"]
    ]

    result = run_bridge_output(tmp_path, value)

    assert result.connected is False
    assert result.bridge_status == "INVALID_MODEL_OUTPUT"


def test_market_direction_is_computed_from_spy_and_qqq(tmp_path: Path) -> None:
    value = snapshot(market_open=True)
    value["market"]["benchmarks"] = [
        {
            "symbol": symbol,
            "current_price": 101.0,
            "vwap": 100.0,
            "ema9": 100.5,
            "ema20": 99.5,
            "previous_close": 99.0,
            "intraday_change_percent": None,
            "candles": [],
            "unavailable_values": [],
        }
        for symbol in ("SPY", "QQQ")
    ]
    value["market"]["direction"] = "UNKNOWN"

    result = run_bridge_output(tmp_path, value)

    assert result.connected is True
    assert result.snapshot["market"]["direction"] == "BULLISH"
    assert result.snapshot["market"]["benchmarks"][0][
        "intraday_change_percent"
    ] == pytest.approx(2 / 99)


def test_codex_timeout_fails_closed(tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=tmp_path / "snapshot.json",
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    ).refresh()

    assert result.bridge_status == "CODEX_TIMEOUT"
    assert result.connected is False
    assert result.snapshot["mcp_status"] == "MCP_UNAVAILABLE"


def test_codex_nonzero_exit_fails_closed(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        return subprocess.CompletedProcess(command, 7, "", "MCP connection failed")

    result = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=tmp_path / "snapshot.json",
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    ).refresh()

    assert result.bridge_status == "CODEX_NONZERO_EXIT"
    assert result.exec_returncode == 7
    assert result.snapshot["mcp_status"] == "MCP_UNAVAILABLE"


def test_invalid_codex_json_replaces_old_snapshot_with_explicit_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "snapshot.json"
    output.write_text('{"old": true}', encoding="utf-8")

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text("not-json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=output,
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    ).refresh()

    stored = json.loads(output.read_text(encoding="utf-8"))
    assert result.bridge_status == "INVALID_JSON"
    assert stored["mcp_status"] == "ROBINHOOD_ERROR"
    assert stored["account"]["is_agentic_account"] is False


def test_successful_snapshot_replacement_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "snapshot.json"
    output.write_text('{"generation": "old"}\n', encoding="utf-8")
    cli_snapshot = snapshot()
    observations: list[dict[str, Any]] = []
    real_replace = __import__("os").replace

    def observe_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == output:
            observations.append(json.loads(output.read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr("agent.codex_mcp_bridge.os.replace", observe_replace)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        if command[1:3] == ["mcp", "list"]:
            return configured_result(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(cli_snapshot), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=output,
        lock_path=tmp_path / "cycle.lock",
        command_runner=fake_run,
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    ).refresh()

    assert result.connected is True
    assert observations == [{"generation": "old"}]
    assert json.loads(output.read_text(encoding="utf-8"))["mcp_status"] == "CONNECTED"


def test_overlapping_cycle_does_not_start_codex_or_replace_snapshot(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    output = tmp_path / "snapshot.json"
    output.write_text('{"generation": "existing"}\n', encoding="utf-8")
    bridge = CodexMcpBridge(
        project_dir=Path.cwd(),
        snapshot_path=output,
        lock_path=tmp_path / "cycle.lock",
        command_runner=lambda command, **_: calls.append(command),  # type: ignore[arg-type,return-value]
        which=lambda _: "/usr/local/bin/codex",
        clock=lambda: NOW,
    )

    with bridge.cycle_lock():
        result = bridge.refresh()

    assert result.bridge_status == "OVERLAPPING_CYCLE"
    assert calls == []
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "generation": "existing"
    }


def test_codex_output_schema_types_const_and_enum_nodes() -> None:
    schema = json.loads(
        (Path.cwd() / "schemas" / "market_snapshot.schema.json").read_text(
            encoding="utf-8"
        )
    )

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "const" in value or "enum" in value:
                assert "type" in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
