"""Direct MCP tests use only in-memory fakes; Robinhood is never contacted."""

from __future__ import annotations

import asyncio
import ast
import json
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.client.auth import PKCEParameters
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import config
from robinhood_mcp.auth import (
    CLIENT_ENTRY,
    KEYCHAIN_SERVICE,
    TOKEN_ENTRY,
    KeychainTokenStorage,
    LocalOAuthCallback,
    make_oauth_provider,
)
from robinhood_mcp.client import (
    DirectRobinhoodMcpClient,
    READ_ONLY_TOOLS,
    historical_arguments,
    quote_arguments,
)
from robinhood_mcp.errors import DirectMcpUnavailable, UnsafeToolError
from robinhood_mcp.models import McpTool, ToolCall
from robinhood_mcp.normalization import (
    normalized_account,
    normalized_criteria,
    normalized_historicals,
    normalized_quotes,
)
from robinhood_mcp.snapshot import DirectSnapshotCollector
from robinhood_mcp.pre_execution import DirectPreExecutionMarketDataProvider
from watcher.quote_provider import RobinhoodDirectQuoteProvider

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


class MemoryKeyring:
    def __init__(self): self.values = {}
    def get_password(self, service, username): return self.values.get((service, username))
    def set_password(self, service, username, password): self.values[(service, username)] = password
    def delete_password(self, service, username): self.values.pop((service, username), None)


def test_secure_keychain_storage_roundtrip_and_refresh_replacement():
    backend = MemoryKeyring()
    storage = KeychainTokenStorage(backend)
    original = OAuthToken(access_token="access-one", refresh_token="refresh-one", expires_in=60)
    refreshed = OAuthToken(access_token="access-two", refresh_token="refresh-two", expires_in=60)
    client = OAuthClientInformationFull(client_id="registered-client")
    asyncio.run(storage.set_tokens(original))
    asyncio.run(storage.set_client_info(client))
    assert storage.has_credentials()
    assert asyncio.run(storage.get_tokens()).access_token == "access-one"
    asyncio.run(storage.set_tokens(refreshed))
    assert asyncio.run(storage.get_tokens()).access_token == "access-two"
    assert backend.values[(KEYCHAIN_SERVICE, TOKEN_ENTRY)].startswith("{")
    assert backend.values[(KEYCHAIN_SERVICE, CLIENT_ENTRY)].startswith("{")


def test_official_sdk_oauth_metadata_and_pkce_are_used():
    storage = KeychainTokenStorage(MemoryKeyring())
    provider = make_oauth_provider(storage, interactive=False)
    metadata = provider.context.client_metadata
    assert provider.context.server_url == config.ROBINHOOD_MCP_SERVER_URL
    assert metadata.grant_types == ["authorization_code", "refresh_token"]
    assert metadata.scope == "internal"
    assert str(metadata.redirect_uris[0]).startswith("http://127.0.0.1:")
    pair = PKCEParameters.generate()
    assert len(pair.code_verifier) >= 43 and pair.code_challenge != pair.code_verifier


def test_callback_handling_returns_code_without_logging_it():
    callback = LocalOAuthCallback("127.0.0.1", 8765)
    event = __import__("threading").Event()
    event.set()
    callback.server = SimpleNamespace(
        callback_event=event,
        callback_params={"code": ["private-code"], "state": ["expected-state"]},
    )
    assert callback.wait()["state"] == ["expected-state"]


def test_tool_arguments_use_discovered_schema_only():
    quote = McpTool("get_equity_quotes", "", {"type": "object", "properties": {"symbols": {"type": "array"}}, "required": ["symbols"]})
    historical = McpTool("get_equity_historicals", "", {
        "type": "object", "properties": {
            "symbol": {"type": "string"},
            "interval": {"type": "string", "enum": ["5minute", "day"]},
            "span": {"type": "string", "enum": ["day", "week"]},
            "bounds": {"type": "string", "enum": ["regular", "extended"]},
        }, "required": ["symbol", "interval", "span", "bounds"],
    })
    assert quote_arguments(quote, ["SPY", "QQQ"]) == {"symbols": ["SPY", "QQQ"]}
    assert historical_arguments(historical, "SPY") == {
        "symbol": "SPY", "interval": "5minute", "span": "day", "bounds": "regular"
    }


def test_nullable_array_symbol_schema_is_batched():
    quote = McpTool("get_equity_quotes", "", {
        "type": "object", "properties": {"symbols": {"type": ["null", "array"], "items": {"type": "string"}}},
        "required": ["symbols"],
    })
    historical = McpTool("get_equity_historicals", "", {
        "type": "object", "properties": {
            "symbols": {"type": ["null", "array"], "items": {"type": "string"}},
            "start_time": {"type": "string"},
        }, "required": ["symbols", "start_time"],
    })
    assert quote_arguments(quote, ["SPY", "QQQ"]) == {"symbols": ["SPY", "QQQ"]}
    assert historical_arguments(historical, "SPY", now=NOW)["symbols"] == ["SPY"]


def candle_rows(symbol: str) -> dict[str, Any]:
    rows = []
    for index in range(50):
        at = NOW - timedelta(minutes=5 * (49 - index))
        close = 100 + index / 10
        rows.append({
            "begins_at": at.isoformat(), "open_price": close - .1,
            "high_price": close + .2, "low_price": close - .2,
            "close_price": close, "volume": 10000 + index,
            "interpolated": False,
        })
    return {"symbol": symbol, "previous_close": 99, "historicals": rows}


class FakeDirectClient:
    connection_latency_seconds = 0.02

    def __init__(self, *, fail_history: str | None = None, account_found: bool = True):
        self.fail_history = fail_history
        self.account_found = account_found
        self.tools = {name: McpTool(name, "", {"type": "object", "properties": {}}) for name in READ_ONLY_TOOLS}
        self.tools["run_scan"] = McpTool("run_scan", "", {"type": "object", "properties": {"scan_name": {"type": "string"}}, "required": ["scan_name"]})
        self.calls = []

    def require_tool(self, name): return self.tools[name]

    def call_readonly(self, name, arguments=None):
        self.calls.append(name)
        values = {
            "get_accounts": {"accounts": [{"nickname": "Agentic" if self.account_found else "Individual", "account_type": "individual", "state": "active"}]},
            "get_portfolio": {"portfolio_value": "10000", "buying_power": "5000", "unleveraged_buying_power": "5000", "cash": "5000", "equity_value": "5000", "currency": "USD"},
            "get_equity_positions": {"positions": []},
            "get_equity_orders": {"orders": []},
            "get_realized_pnl": {"realized_pnl": 0},
            "get_scans": {"scans": [{"name": config.PROJECT_SCANNER_NAME, "scan_id": "safe-scan-id", "filters": list(config.SCANNER_CRITERIA)}]},
        }
        return ToolCall(name, arguments or {}, values[name], .01)

    def run_project_scan(self, *, scan_id, account=None):
        self.calls.append("run_scan")
        rows = [
            {"symbol": "ACME", "last": 101, "volume": 2_000_000, "average_volume": 1_000_000, "relative_volume": 2, "percent_change": 3},
            {"symbol": "BETA", "last": 55, "volume": 1_500_000, "average_volume": 1_000_000, "relative_volume": 1.5, "percent_change": 2},
        ]
        return ToolCall("run_scan", {"scan_name": config.PROJECT_SCANNER_NAME}, {"results": rows}, .02)

    def get_quotes(self, symbols):
        self.calls.append("get_equity_quotes")
        rows = [{"symbol": symbol, "last_trade_price": 105, "bid_price": 104.9,
                 "ask_price": 105.1, "updated_at": NOW.isoformat(), "previous_close": 100}
                for symbol in symbols]
        return ToolCall("get_equity_quotes", {"symbols": list(symbols)}, {"results": rows}, .02)

    def get_historicals_many(self, symbols):
        self.calls.extend(f"history:{symbol}" for symbol in symbols)
        return [
            DirectMcpUnavailable("mock") if symbol == self.fail_history
            else ToolCall("get_equity_historicals", {"symbol": symbol}, candle_rows(symbol), .01)
            for symbol in symbols
        ]

    def get_historicals(self, symbol):
        self.calls.append(f"history:{symbol}")
        return ToolCall("get_equity_historicals", {"symbol": symbol}, candle_rows(symbol), .01)


def test_direct_snapshot_generation_indicators_atomic_and_no_secrets(tmp_path):
    output = tmp_path / "state" / "market_snapshot.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"old": true}')
    collector = DirectSnapshotCollector(
        FakeDirectClient(), project_dir=Path.cwd(), snapshot_path=output,
        timing_path=tmp_path / "timing.json", metadata_cache_path=tmp_path / "cache.json",
        clock=lambda: NOW,
    )
    result = collector.refresh()
    stored = json.loads(output.read_text())
    assert result.connected and result.bridge_status == "OK"
    assert stored["mcp_access_path"] == "DIRECT_MCP"
    assert stored["account"]["is_agentic_account"] is True
    assert stored["scanner"]["result_count"] == 2
    assert [row["symbol"] for row in stored["candidate_data"]] == ["ACME", "BETA"]
    assert stored["candidate_data"][0]["ema9"] is not None
    serialized = output.read_text().lower()
    assert "access_token" not in serialized and "refresh_token" not in serialized
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_pre_execution_provider_fetches_only_selected_symbol(tmp_path):
    client = FakeDirectClient()
    row = DirectPreExecutionMarketDataProvider(
        client, project_dir=tmp_path, market_direction="BULLISH",
        clock=lambda: NOW,
    ).refresh_symbol("ACME", now=NOW)
    assert client.calls == ["history:ACME", "get_equity_quotes"]
    assert row["symbol"] == "ACME"
    assert row["quote_as_of"] == NOW.isoformat()
    assert row["candles"]


def test_one_candidate_history_failure_is_isolated(tmp_path):
    output = tmp_path / "snapshot.json"
    result = DirectSnapshotCollector(
        FakeDirectClient(fail_history="BETA"), project_dir=Path.cwd(), snapshot_path=output,
        timing_path=tmp_path / "timing.json", metadata_cache_path=tmp_path / "cache.json",
        clock=lambda: NOW
    ).refresh()
    assert result.connected and result.bridge_status == "CANDIDATE_COLLECTION_PARTIAL"
    rows = {row["symbol"]: row for row in result.snapshot["candidate_data"]}
    assert rows["ACME"]["collection_status"] == "COMPLETE"
    assert rows["BETA"]["collection_status"] == "DATA_UNAVAILABLE"


def test_missing_agentic_account_fails_core_closed(tmp_path):
    result = DirectSnapshotCollector(
        FakeDirectClient(account_found=False), project_dir=Path.cwd(), snapshot_path=tmp_path / "snapshot.json",
        timing_path=tmp_path / "timing.json", metadata_cache_path=tmp_path / "cache.json",
        clock=lambda: NOW
    ).refresh()
    assert not result.connected
    assert result.bridge_status == "DIRECT_MCP_DATA_ERROR"
    assert result.snapshot["mcp_status"] == "ROBINHOOD_ERROR"


def test_local_historical_normalization_rejects_interpolated_bars():
    value = candle_rows("ACME")
    value["historicals"][0]["interpolated"] = True
    normalized = normalized_historicals(value, "ACME")
    assert len(normalized["candles"]) == 49


def test_saved_scanner_title_is_recognized_from_live_get_scans_shape():
    from robinhood_mcp.normalization import find_project_scan
    row = {"title": config.PROJECT_SCANNER_NAME, "scan_id": "safe-id", "filter_summary": []}
    assert find_project_scan({"data": {"scans": [row]}}) is row


def test_scanner_normalization_preserves_robinhood_filter_enum_over_label():
    criteria = normalized_criteria({"filter_summary": [{
        "filter_type": "Asset type",
        "filter_type_enum": "FILTER_TYPE_INSTRUMENT_TYPE",
        "values": ["STOCK"],
    }]})
    assert criteria[0]["filter_type"] == "Asset type"
    assert criteria[0]["filter_type_enum"] == "FILTER_TYPE_INSTRUMENT_TYPE"


def test_live_quote_shape_uses_robinhood_venue_last_trade_time():
    exchange_time = "2026-09-10T14:59:59.123456789Z"
    rows = normalized_quotes({"results": [{
        "symbol": "ACME",
        "last_trade_price": "101.25",
        "bid_price": "101.24",
        "ask_price": "101.26",
        "venue_last_trade_time": exchange_time,
        "venue_bid_time": "2026-09-10T14:59:58Z",
        "venue_ask_time": "2026-09-10T14:59:58Z",
    }]}, ["ACME"], retrieved_at=NOW, request_started_at=NOW - timedelta(seconds=1))
    assert rows["ACME"]["quote_as_of"] == exchange_time
    assert rows["ACME"]["quote_timestamp_field"] == "venue_last_trade_time"
    assert rows["ACME"]["quote_freshness_source"] == "exchange_timestamp"
    assert rows["ACME"]["quote_request_started_at"] is not None


def test_quote_without_exchange_time_keeps_retrieval_timestamp_explicit():
    rows = normalized_quotes({"results": [{
        "symbol": "ACME", "last_trade_price": "101.25",
    }]}, ["ACME"], retrieved_at=NOW)
    assert rows["ACME"]["quote_as_of"] is None
    assert rows["ACME"]["quote_freshness_source"] == "retrieval_timestamp"
    assert rows["ACME"]["quote_retrieved_at"].endswith("Z")


def test_nested_previous_close_price_cannot_overwrite_live_quote():
    rows = normalized_quotes({"results": [{
        "symbol": "ACME", "last_trade_price": "101.25",
        "bid_price": "101.24", "ask_price": "101.26",
        "venue_last_trade_time": NOW.isoformat(),
        "previous_close": "97.00",
        "adjusted_previous_close": {
            "symbol": "ACME", "date": "2026-09-09", "price": "97.00",
            "source": "sip-list-exchange-close", "interpolated": False,
        },
    }]}, ["ACME"], retrieved_at=NOW)
    assert rows["ACME"]["current_price"] == 101.25
    assert rows["ACME"]["bid"] == 101.24
    assert rows["ACME"]["ask"] == 101.26
    assert rows["ACME"]["quote_freshness_source"] == "exchange_timestamp"


def test_direct_quote_provider_keeps_source_timestamp_and_measures():
    provider = RobinhoodDirectQuoteProvider(FakeDirectClient(), clock=lambda: NOW)
    quotes = provider.get_quotes(["ACME"])
    assert quotes["ACME"].timestamp == NOW
    assert quotes["ACME"].source == "DIRECT_ROBINHOOD_MCP"
    assert provider.mode == "DIRECT_MCP_UNVALIDATED"
    assert provider.metrics["request_count"] == 1
    assert provider.metrics["median_latency_seconds"] is not None
    assert provider.metrics["p95_latency_seconds"] is not None
    assert provider.metrics["median_quote_age_seconds"] == 0
    assert provider.metrics["p95_quote_age_seconds"] == 0
    assert provider.metrics["suitable_for_configured_fast_watcher"] is False


class Stored:
    def has_credentials(self): return True


class FakeSdkContext(AbstractAsyncContextManager):
    def __init__(self): self.closed = False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    async def list_tools(self, **kwargs):
        schema = {"type": "object", "properties": {}}
        return SimpleNamespace(tools=[SimpleNamespace(name="get_accounts", description="", input_schema=schema)])

    async def call_tool(self, name, arguments):
        return SimpleNamespace(is_error=False, structured_content={"accounts": []})


def test_connection_tool_discovery_clean_shutdown_and_order_rejection():
    context = FakeSdkContext()
    client = DirectRobinhoodMcpClient(storage=Stored(), sdk_factory=lambda provider: context)
    client.start()
    assert "get_accounts" in client.tools
    with pytest.raises(UnsafeToolError):
        client.require_tool("place_equity_order")
    client.close()
    assert context.closed


def test_missing_credentials_does_not_start_connection():
    class Empty:
        def has_credentials(self): return False
    with pytest.raises(Exception, match="robinhood-auth"):
        DirectRobinhoodMcpClient(storage=Empty(), sdk_factory=lambda provider: None).start()


def test_transient_read_reconnects_once_and_cleanly_recovers(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_RETRY_BASE_SECONDS", 0)
    contexts = []

    class Context(FakeSdkContext):
        def __init__(self, fail):
            super().__init__()
            self.fail = fail
            self.calls = 0
        async def call_tool(self, name, arguments):
            self.calls += 1
            if self.fail:
                raise OSError("transient mocked disconnect")
            return SimpleNamespace(is_error=False, structured_content={"accounts": [{"nickname": "Agentic"}]})

    def factory(provider):
        context = Context(fail=not contexts)
        contexts.append(context)
        return context

    client = DirectRobinhoodMcpClient(storage=Stored(), sdk_factory=factory)
    with client:
        call = client.call_readonly("get_accounts")
        assert normalized_account(call.value)[0]["is_agentic_account"] is True
    assert len(contexts) == 2
    assert all(context.closed for context in contexts)


def test_scanner_mutation_is_not_automatically_retried(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_RETRY_BASE_SECONDS", 0)

    class ScannerContext(FakeSdkContext):
        def __init__(self):
            super().__init__()
            self.calls = 0
        async def list_tools(self, **kwargs):
            return SimpleNamespace(tools=[SimpleNamespace(
                name="run_scan", description="", input_schema={
                    "type": "object", "properties": {"scan_name": {"type": "string"}},
                    "required": ["scan_name"],
                }
            )])
        async def call_tool(self, name, arguments):
            self.calls += 1
            raise OSError("mocked scanner failure")

    context = ScannerContext()
    client = DirectRobinhoodMcpClient(storage=Stored(), sdk_factory=lambda provider: context)
    with client:
        with pytest.raises(DirectMcpUnavailable):
            client.run_project_scan(scan_id=None)
    assert context.calls == 1


def test_no_codex_or_cookie_paths_in_direct_source():
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "robinhood_mcp").glob("*.py"))
    source = "\n".join(path.read_text() for path in paths).lower()
    assert ".codex" not in source
    assert "browser_cookie" not in source
    imported = {
        alias.name.split(".")[0]
        for path in paths
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "subprocess" not in imported
    assert "place_equity" in source  # appears only in the explicit deny marker
    assert "codex exec" not in source
