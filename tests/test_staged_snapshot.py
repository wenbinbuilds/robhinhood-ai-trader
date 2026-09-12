import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from agent.codex_mcp_bridge import CodexMcpBridge

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)


def core(candidates=None, *, connected=True):
    candidates = candidates or []
    return {
        "generated_at": NOW.isoformat(),
        "mcp_status": "CONNECTED" if connected else "ROBINHOOD_ERROR",
        "status_detail": "real data" if connected else "core failed",
        "account": {"is_agentic_account": connected, "nickname": "Agentic" if connected else None, "account_type": "individual" if connected else None, "brokerage_trading_type": "cash", "state": "active" if connected else None},
        "portfolio": {"portfolio_value": "10000", "buying_power": "5000", "unleveraged_buying_power": "5000", "cash": "5000", "equity_value": "5000", "currency": "USD"},
        "positions": [], "open_orders": [], "daily_realized_pnl": "0",
        "market": {"status": "OPEN", "is_regular_session": True, "as_of": NOW.isoformat()},
        "scanner": {"status": "OK", "scan_name": config.PROJECT_SCANNER_NAME, "scan_id": "safe-scan", "lifecycle_action": "REUSED", "criteria": [], "sort_configuration": dict(config.SCANNER_SORT), "result_count": len(candidates), "candidates": candidates},
        "warnings": [], "errors": [],
    }


def scanner_row(symbol, change, relative=1.5, volume=1_000_000):
    return {"symbol": symbol, "instrument_type": "EQUITY", "last": 20.0, "volume": volume, "average_volume": 800_000, "relative_volume": relative, "percent_change": change}


def bars():
    start = NOW - timedelta(minutes=250)
    return [{"begins_at": (start + timedelta(minutes=5*i)).isoformat(), "open": 20+i*.1, "high": 20.2+i*.1, "low": 19.9+i*.1, "close": 20.1+i*.1, "volume": 10000.0, "interpolated": False} for i in range(50)]


def market_row(symbol):
    return {"symbol": symbol, "current_price": 25.0, "bid": 24.99, "ask": 25.01, "quote_as_of": NOW.isoformat(), "quote_retrieved_at": NOW.isoformat(), "previous_close": 24.0, "relative_volume": 1.5, "sector": "Technology", "industry": "Software", "candles": bars(), "unavailable_values": []}


class FakeRunner:
    def __init__(self, core_value, *, fail_batch=None, nonzero_stage=None, unavailable_symbols=()):
        self.core_value = core_value
        self.fail_batch = fail_batch
        self.nonzero_stage = nonzero_stage
        self.unavailable_symbols = set(unavailable_symbols)
        self.exec_calls = []

    def __call__(self, command, **kwargs):
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in", "")
        if command[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(command, 0, "robinhood-trading url enabled Unknown", "")
        self.exec_calls.append((command, kwargs))
        schema = Path(command[command.index("--output-schema") + 1]).name
        output = Path(command[command.index("--output-last-message") + 1])
        if schema == "snapshot_core.schema.json":
            value = self.core_value
            stage = "CORE"
        else:
            prompt = kwargs["input"]
            symbols = prompt.split("SYMBOLS (exactly): ", 1)[1].splitlines()[0].split(", ")
            stage = "BENCHMARK" if symbols == ["SPY", "QQQ", "NVDA"] else "BATCH"
            if self.fail_batch and self.fail_batch in symbols:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            value = {
                "generated_at": NOW.isoformat(),
                "rows": [market_row(symbol) for symbol in symbols if symbol not in self.unavailable_symbols],
                "failures": [{"symbol": symbol, "status": "DATA_UNAVAILABLE", "detail": "fixture failure"} for symbol in symbols if symbol in self.unavailable_symbols],
                "warnings": [],
            }
        if self.nonzero_stage == stage:
            return subprocess.CompletedProcess(command, 7, "", "ERROR_REPORTED")
        output.write_text(json.dumps(value), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")


def bridge(tmp_path, runner):
    return CodexMcpBridge(project_dir=Path.cwd(), snapshot_path=tmp_path / "snapshot.json", lock_path=tmp_path / "lock", command_runner=runner, which=lambda _: "/usr/local/bin/codex", clock=lambda: NOW, staged=True)


def test_staged_success_ranks_deduplicates_batches_and_atomically_writes(tmp_path, monkeypatch):
    candidates = [scanner_row("BBB", .02), scanner_row("AAA", .05), scanner_row("AAA", .09), scanner_row("CCC", .03), scanner_row("DDD", .01)]
    runner = FakeRunner(core(candidates))
    output = tmp_path / "snapshot.json"
    output.write_text('{"old":true}', encoding="utf-8")
    observations = []
    real_replace = __import__("os").replace
    def observe(src, dst):
        if Path(dst) == output:
            observations.append(json.loads(output.read_text()))
        real_replace(src, dst)
    monkeypatch.setattr("agent.codex_mcp_bridge.os.replace", observe)
    result = bridge(tmp_path, runner).refresh()
    assert result.connected
    assert observations == [{"old": True}]
    assert [row["symbol"] for row in result.snapshot["scanner"]["candidates"]] == ["AAA", "CCC", "BBB", "DDD"]
    assert [len(call[1]["input"].split("SYMBOLS (exactly): ")) for call in runner.exec_calls]
    assert len(runner.exec_calls) == 2  # one core plus one compact market-data process
    assert "NVDA" not in runner.exec_calls[1][1]["input"]
    assert all(row["collection_status"] == "COMPLETE" for row in result.snapshot["candidate_data"])
    assert "raw" not in json.dumps(result.snapshot).lower()
    assert result.snapshot["candidate_data"][0]["level2"] is None
    assert "level2" in result.snapshot["candidate_data"][0]["unavailable_values"]


def test_candidate_batch_timeout_preserves_other_candidates(tmp_path):
    candidates = [scanner_row(symbol, .10-index*.01) for index, symbol in enumerate(("AAA", "BBB", "CCC", "DDD"))]
    result = bridge(tmp_path, FakeRunner(core(candidates), fail_batch="BBB")).refresh()
    assert result.connected
    rows = {row["symbol"]: row for row in result.snapshot["candidate_data"]}
    assert rows["AAA"]["collection_status"] == "DATA_UNAVAILABLE"
    assert rows["BBB"]["collection_status"] == "DATA_UNAVAILABLE"
    assert rows["CCC"]["collection_status"] == "DATA_UNAVAILABLE"
    assert rows["DDD"]["collection_status"] == "DATA_UNAVAILABLE"
    assert result.bridge_status == "CANDIDATE_COLLECTION_PARTIAL"
    assert len(result.snapshot["market"]["benchmarks"]) == 2


def test_one_candidate_failure_preserves_other_candidates(tmp_path):
    candidates = [scanner_row("AAA", .05), scanner_row("BBB", .04)]
    result = bridge(tmp_path, FakeRunner(core(candidates), unavailable_symbols={"BBB"})).refresh()
    rows = {row["symbol"]: row for row in result.snapshot["candidate_data"]}
    assert result.connected
    assert result.bridge_status == "CANDIDATE_COLLECTION_PARTIAL"
    assert rows["AAA"]["collection_status"] == "COMPLETE"
    assert rows["BBB"]["collection_status"] == "DATA_UNAVAILABLE"


def test_core_failure_fails_entire_cycle(tmp_path):
    result = bridge(tmp_path, FakeRunner(core(connected=False))).refresh()
    assert not result.connected
    assert result.bridge_status == "CORE_FAILED"


def test_missing_mcp_market_status_is_replaced_by_local_exchange_calendar(tmp_path):
    value = core()
    value["market"] = {"status": "UNKNOWN", "is_regular_session": None, "as_of": NOW.isoformat()}
    result = bridge(tmp_path, FakeRunner(value)).refresh()
    assert result.connected
    assert result.snapshot["market"]["status"] == "OPEN"
    assert result.snapshot["market"]["is_regular_session"] is True


def test_core_nonzero_exit_fails_entire_cycle(tmp_path):
    result = bridge(tmp_path, FakeRunner(core(), nonzero_stage="CORE")).refresh()
    assert not result.connected
    assert result.bridge_status == "CORE_START_FAILED"


def test_candidate_limit_is_applied_before_deep_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_CANDIDATES_TO_ANALYZE", 2)
    candidates = [scanner_row(f"T{i}", .1-i*.001) for i in range(8)]
    runner = FakeRunner(core(candidates))
    result = bridge(tmp_path, runner).refresh()
    assert result.connected
    assert len(result.snapshot["candidate_data"]) == 2
    assert len(runner.exec_calls) == 2


def test_staged_tool_allowlists_contain_no_order_mutations(tmp_path):
    runner = FakeRunner(core([scanner_row("AAA", .05)]))
    result = bridge(tmp_path, runner).refresh()
    assert result.connected
    commands = "\n".join(" ".join(call[0]) for call in runner.exec_calls)
    for forbidden in ("place_equity_order", "review_equity_order", "cancel_equity_order", "replace_order", "close_position"):
        assert forbidden not in commands
    assert "get_equity_orders" in commands


def test_overall_timeout_fails_closed_before_benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SNAPSHOT_OVERALL_TIMEOUT_SECONDS", 0)
    result = bridge(tmp_path, FakeRunner(core())).refresh()
    assert result.connected
    assert result.bridge_status == "BENCHMARK_FAILED"
    assert result.snapshot["mcp_status"] == "CONNECTED"


def test_combined_timeout_retries_spy_and_qqq_individually(tmp_path):
    runner = FakeRunner(core([scanner_row("AAA", .05)]), fail_batch="AAA")
    result = bridge(tmp_path, runner).refresh()
    assert result.connected
    assert len(runner.exec_calls) == 4
    prompts = [call[1]["input"] for call in runner.exec_calls]
    assert "SYMBOLS (exactly): SPY\n" in prompts[2]
    assert "SYMBOLS (exactly): QQQ\n" in prompts[3]
    assert [row["symbol"] for row in result.snapshot["market"]["benchmarks"]] == ["SPY", "QQQ"]


def test_spy_fallback_failure_preserves_core_and_qqq(tmp_path):
    original = core([scanner_row("AAA", .05)])
    result = bridge(tmp_path, FakeRunner(original, fail_batch="SPY")).refresh()
    assert result.connected
    assert result.bridge_status == "BENCHMARK_FAILED"
    assert result.snapshot["mcp_status"] == "CONNECTED"
    assert result.snapshot["account"]["is_agentic_account"] is True
    assert result.snapshot["scanner"]["status"] == "OK"
    benchmarks = {row["symbol"]: row for row in result.snapshot["market"]["benchmarks"]}
    assert benchmarks["SPY"]["current_price"] is None
    assert benchmarks["QQQ"]["current_price"] == 25.0


def test_qqq_fallback_failure_is_explicit(tmp_path):
    result = bridge(tmp_path, FakeRunner(core(), fail_batch="QQQ")).refresh()
    assert result.bridge_status == "BENCHMARK_FAILED"
    warnings = " ".join(result.snapshot["warnings"])
    # Benchmark failure is represented in the normalized benchmark itself;
    # scanner/account state remains valid and connected.
    qqq = next(row for row in result.snapshot["market"]["benchmarks"] if row["symbol"] == "QQQ")
    assert qqq["current_price"] is None
    assert result.snapshot["scanner"]["status"] == "OK"
