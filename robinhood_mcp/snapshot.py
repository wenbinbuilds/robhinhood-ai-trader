"""Direct factual snapshot collection and atomic Python normalization."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

import config
from agent.market_calendar import regular_session
from agent.market_cycle import JsonSnapshotProvider, SnapshotValidationError
from agent.staged_snapshot import (
    InstrumentMetadataCache,
    benchmark_bundle,
    candidate_bundle,
    rank_scanner_candidates,
)
from robinhood_mcp.client import DirectRobinhoodMcpClient, account_read_arguments
from robinhood_mcp.errors import (
    DirectMcpUnavailable,
    RobinhoodAuthenticationRequired,
    RobinhoodResponseError,
)
from robinhood_mcp.models import DirectTiming, ToolCall
from robinhood_mcp.normalization import (
    find_project_scan,
    normalized_account,
    normalized_historicals,
    normalized_orders,
    normalized_portfolio,
    normalized_positions,
    normalized_quotes,
    normalized_realized_pnl,
    normalized_scanner,
    safe_scan_id,
    scanner_candidates,
)
from watcher.storage import atomic_json


@dataclass(frozen=True)
class DirectRefreshResult:
    snapshot: Mapping[str, Any]
    snapshot_path: Path
    bridge_status: str
    data_provider: str = "DIRECT_MCP"

    @property
    def connected(self) -> bool:
        return self.snapshot.get("mcp_status") == "CONNECTED" and self.bridge_status in {
            "OK", "CANDIDATE_COLLECTION_PARTIAL"
        }

    # Compatibility attributes consumed only by the existing diagnostic helper.
    codex_cli_found: bool = False
    codex_authenticated: bool = False
    custom_mcp_configured: bool = False
    exec_returncode: int | None = None


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _market_direction(benchmarks: Sequence[Mapping[str, Any]]) -> str:
    positive = negative = 0
    for item in benchmarks:
        current = item.get("current_price")
        change = item.get("intraday_change_percent")
        comparisons = (
            (current, item.get("vwap")),
            (item.get("ema9"), item.get("ema20")),
            (change, 0.0),
        )
        for left, right in comparisons:
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)) or left == right:
                continue
            if left > right:
                positive += 1
            else:
                negative += 1
    if positive + negative < 2:
        return "UNKNOWN"
    if positive > negative:
        return "BULLISH"
    if negative > positive:
        return "BEARISH"
    return "MIXED"


class DirectSnapshotCollector:
    """Build one schema-valid snapshot without any model or subprocess call."""

    def __init__(
        self,
        client: DirectRobinhoodMcpClient,
        *,
        project_dir: str | Path,
        snapshot_path: str | Path = "state/market_snapshot.json",
        schema_path: str | Path = "schemas/market_snapshot.schema.json",
        timing_path: str | Path | None = None,
        metadata_cache_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.project_dir = Path(project_dir).resolve()
        self.snapshot_path = self._resolve(snapshot_path)
        self.schema_path = self._resolve(schema_path)
        self.timing_path = self._resolve(timing_path or config.ROBINHOOD_DIRECT_TIMINGS_PATH)
        self.metadata_cache_path = self._resolve(
            metadata_cache_path or config.INSTRUMENT_METADATA_CACHE_PATH
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _resolve(self, path: str | Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.project_dir / value

    def refresh(self, *, shadow_symbols: Sequence[str] = ()) -> DirectRefreshResult:
        started = time.monotonic()
        timing = DirectTiming(connection_latency_seconds=self.client.connection_latency_seconds)
        try:
            snapshot, bridge_status = self._collect(timing, shadow_symbols=shadow_symbols)
            self._validate(snapshot)
            write_start = time.monotonic()
            atomic_json(self.snapshot_path, snapshot)
            write_duration = time.monotonic() - write_start
            timing.snapshot_total_seconds = time.monotonic() - started
            timing.calls.append({"stage": "atomic_write", "duration_seconds": round(write_duration, 6)})
            JsonSnapshotProvider.from_path(
                self.snapshot_path,
                now=_utc(self.clock),
                max_age_seconds=config.SNAPSHOT_MAX_AGE_SECONDS,
            )
            self._save_timing(timing, bridge_status)
            print(f"SNAPSHOT TOTAL: {timing.snapshot_total_seconds:.3f}s")
            return DirectRefreshResult(snapshot, self.snapshot_path, bridge_status)
        except RobinhoodAuthenticationRequired:
            return self._fail("MCP_NOT_AUTHENTICATED", "DIRECT_MCP_NOT_AUTHENTICATED", started, timing)
        except DirectMcpUnavailable:
            return self._fail("MCP_UNAVAILABLE", "DIRECT_MCP_UNAVAILABLE", started, timing)
        except (RobinhoodResponseError, SnapshotValidationError, ValueError, TypeError, OSError):
            return self._fail("ROBINHOOD_ERROR", "DIRECT_MCP_DATA_ERROR", started, timing)

    def _call(
        self,
        name: str,
        *,
        required: bool = True,
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolCall | None:
        if name not in self.client.tools:
            if required:
                self.client.require_tool(name)
            return None
        try:
            return self.client.call_readonly(name, arguments)
        except (DirectMcpUnavailable, RobinhoodResponseError):
            if required:
                raise
            return None

    @staticmethod
    def _record(timing: DirectTiming, stage: str, call: ToolCall | None) -> None:
        if call is not None:
            timing.calls.append({
                "stage": stage,
                "tool": call.name,
                "duration_seconds": round(call.duration_seconds, 6),
            })

    def _collect(self, timing: DirectTiming, *, shadow_symbols: Sequence[str]) -> tuple[dict[str, Any], str]:
        accounts = self._call("get_accounts")
        self._record(timing, "account", accounts)
        account, raw_account = normalized_account(accounts.value)

        def account_args(name: str) -> Mapping[str, Any]:
            return account_read_arguments(
                self.client.require_tool(name), raw_account, now=_utc(self.clock)
            )

        portfolio_call = self._call("get_portfolio", arguments=account_args("get_portfolio"))
        positions_call = self._call("get_equity_positions", arguments=account_args("get_equity_positions"))
        orders_call = self._call("get_equity_orders", arguments=account_args("get_equity_orders"))
        realized_call = (
            self._call("get_realized_pnl", required=False, arguments=account_args("get_realized_pnl"))
            if "get_realized_pnl" in self.client.tools else None
        )
        for stage, call in (("portfolio", portfolio_call), ("positions", positions_call),
                            ("orders", orders_call), ("realized_pnl", realized_call)):
            self._record(timing, stage, call)
        portfolio = normalized_portfolio(portfolio_call.value)
        positions = normalized_positions(positions_call.value)
        orders = normalized_orders(orders_call.value)
        realized = normalized_realized_pnl(realized_call.value) if realized_call else None
        print("CORE:")
        print("account: OK")
        print("portfolio: OK")
        print("positions: OK")

        scanner_started = time.monotonic()
        scans_call = self._call("get_scans")
        self._record(timing, "scanner_list", scans_call)
        project_scan = find_project_scan(scans_call.value)
        if project_scan is None:
            raise RobinhoodResponseError(
                "project scanner is missing; scanner maintenance must be run explicitly"
            )
        run_call = self.client.run_project_scan(
            scan_id=safe_scan_id(project_scan), account=raw_account
        )
        self._record(timing, "scanner_run", run_call)
        raw_candidates = scanner_candidates(run_call.value)
        timing.scanner_latency_seconds = time.monotonic() - scanner_started
        print("scanner: OK")
        print(f"scanner results: {len(raw_candidates)}")
        print(f"core/scanner duration: {timing.scanner_latency_seconds:.3f}s")

        now = _utc(self.clock)
        market_status, is_regular, _ = regular_session(now)
        ranked = rank_scanner_candidates(raw_candidates) if is_regular else []
        print("TOP CANDIDATES: " + (", ".join(row["symbol"] for row in ranked) or "NONE"))
        scanner = normalized_scanner(project_scan, run_call.value, ranked)
        if not is_regular:
            scanner["status"] = "SKIPPED_MARKET_CLOSED"
            scanner["lifecycle_action"] = "SKIPPED"
            scanner["candidates"] = []

        normalized_shadow = sorted(
            {
                str(symbol).upper().strip()
                for symbol in shadow_symbols
                if str(symbol).strip()
            }
        )
        candidate_symbols = [row["symbol"] for row in ranked]
        deep_symbols = list(dict.fromkeys(candidate_symbols + normalized_shadow))
        requested = list(dict.fromkeys(["SPY", "QQQ"] + deep_symbols))

        candidate_started = time.monotonic()
        quote_request_started_at = _utc(self.clock)
        quote_started = time.monotonic()
        quote_call = self.client.get_quotes(requested)
        quote_retrieved_at = _utc(self.clock)
        timing.quote_latency_seconds = time.monotonic() - quote_started
        self._record(timing, "quotes", quote_call)
        quotes = normalized_quotes(
            quote_call.value,
            requested,
            retrieved_at=quote_retrieved_at,
            request_started_at=quote_request_started_at,
        )

        historical_started = time.monotonic()
        history_calls = self.client.get_historicals_many(requested)
        timing.historical_latency_seconds = time.monotonic() - historical_started
        history: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        for symbol, call in zip(requested, history_calls):
            if isinstance(call, BaseException):
                failures[symbol] = "historical data unavailable"
                continue
            self._record(timing, f"historical:{symbol}", call)
            history[symbol] = normalized_historicals(call.value, symbol)
        timing.candidate_collection_latency_seconds = time.monotonic() - candidate_started
        successful_histories = sum(not isinstance(call, BaseException) for call in history_calls)
        print("MARKET DATA:")
        print("quotes: OK")
        print(f"histories: {successful_histories}/{len(requested)}")
        print(f"SPY: {'OK' if 'SPY' in history else 'DATA_UNAVAILABLE'}")
        print(f"QQQ: {'OK' if 'QQQ' in history else 'DATA_UNAVAILABLE'}")
        print(f"market-data duration: {timing.candidate_collection_latency_seconds:.3f}s")

        rows: dict[str, dict[str, Any]] = {}
        for symbol in requested:
            quote = quotes.get(symbol)
            historical = history.get(symbol)
            if quote is None:
                failures[symbol] = "quote unavailable"
            if historical is None:
                failures.setdefault(symbol, "historical data unavailable")
            if quote is not None and historical is not None:
                rows[symbol] = {**historical, **quote,
                                "previous_close": quote.get("previous_close") or historical.get("previous_close"),
                                "relative_volume": historical.get("relative_volume")}

        indicator_started = time.monotonic()
        benchmark_rows = [benchmark_bundle(symbol, rows.get(symbol), now=now) for symbol in ("SPY", "QQQ")]
        direction = _market_direction(benchmark_rows)
        cache = InstrumentMetadataCache(
            self.metadata_cache_path, now=now
        )
        scanner_lookup = {row["symbol"]: row for row in ranked}
        candidate_data = [
            candidate_bundle(
                symbol,
                rows.get(symbol),
                scanner_row=scanner_lookup.get(symbol),
                market_direction=direction,
                cache=cache,
                failure=failures.get(symbol),
                now=now,
            )
            for symbol in candidate_symbols
        ]
        shadow_data = [
            candidate_bundle(
                symbol,
                rows.get(symbol),
                scanner_row=scanner_lookup.get(symbol),
                market_direction=direction,
                cache=cache,
                failure=failures.get(symbol),
                now=now,
            )
            for symbol in normalized_shadow
        ]
        timing.indicator_calculation_latency_seconds = time.monotonic() - indicator_started
        print("LOCAL INDICATORS: OK")
        cache.save()

        normalize_started = time.monotonic()
        warnings = [
            f"{symbol}: DATA_UNAVAILABLE ({detail})"
            for symbol, detail in sorted(failures.items())
        ]
        snapshot = {
            "data_source": "ROBINHOOD_MCP",
            "generated_at": _timestamp(_utc(self.clock)),
            "mcp_status": "CONNECTED",
            "mcp_access_path": "DIRECT_MCP",
            "status_detail": "real Robinhood facts collected directly with the official MCP Python SDK",
            "account": account,
            "portfolio": portfolio,
            "positions": positions,
            "open_orders": orders,
            "daily_realized_pnl": realized,
            "market": {
                "status": market_status,
                "is_regular_session": is_regular,
                "as_of": _timestamp(now),
                "direction": direction,
                "benchmarks": benchmark_rows,
                "volatility_context": None,
            },
            "scanner": scanner,
            "candidate_data": candidate_data,
            "shadow_position_data": shadow_data,
            "connectivity_checks": {},
            "warnings": warnings,
            "errors": [],
        }
        timing.snapshot_normalization_latency_seconds = time.monotonic() - normalize_started
        partial_candidates = any(symbol in failures for symbol in deep_symbols)
        return snapshot, "CANDIDATE_COLLECTION_PARTIAL" if partial_candidates else "OK"

    def _validate(self, snapshot: Mapping[str, Any]) -> None:
        JsonSnapshotProvider._reject_sensitive_keys(snapshot)
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(snapshot), key=lambda error: list(error.absolute_path))
        if errors:
            location = ".".join(str(item) for item in errors[0].absolute_path) or "root"
            raise SnapshotValidationError(f"schema violation at {location}: {errors[0].message}")

    def _save_timing(self, timing: DirectTiming, status: str) -> None:
        atomic_json(
            self.timing_path,
            {
                "generated_at": _timestamp(_utc(self.clock)),
                "provider": "DIRECT_MCP",
                "status": status,
                **timing.as_dict(),
            },
        )

    def _fail(self, mcp_status: str, bridge_status: str, started: float, timing: DirectTiming) -> DirectRefreshResult:
        now = _utc(self.clock)
        snapshot = {
            "data_source": "ROBINHOOD_MCP",
            "generated_at": _timestamp(now),
            "mcp_status": mcp_status,
            "mcp_access_path": "DIRECT_MCP",
            "status_detail": bridge_status,
            "account": {"is_agentic_account": False, "nickname": None, "account_type": None,
                        "brokerage_trading_type": None, "state": None},
            "portfolio": {"portfolio_value": None, "buying_power": None,
                          "unleveraged_buying_power": None, "cash": None,
                          "equity_value": None, "currency": None},
            "positions": [], "open_orders": [], "daily_realized_pnl": None,
            "market": {"status": "UNKNOWN", "is_regular_session": None, "as_of": _timestamp(now),
                       "direction": "UNKNOWN", "benchmarks": [], "volatility_context": None},
            "scanner": {"status": "NOT_ATTEMPTED", "scan_name": None, "scan_id": None,
                        "lifecycle_action": "NOT_ATTEMPTED", "criteria": [],
                        "sort_configuration": {"column": None, "direction": None},
                        "result_count": 0, "candidates": []},
            "candidate_data": [], "shadow_position_data": [], "connectivity_checks": {},
            "warnings": [], "errors": [bridge_status],
        }
        atomic_json(self.snapshot_path, snapshot)
        timing.snapshot_total_seconds = time.monotonic() - started
        self._save_timing(timing, bridge_status)
        print(f"SNAPSHOT TOTAL: {timing.snapshot_total_seconds:.3f}s")
        return DirectRefreshResult(snapshot, self.snapshot_path, bridge_status)
