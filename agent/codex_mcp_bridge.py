"""Non-trading Codex CLI bridge for normalized Robinhood market snapshots.

The bridge is the only application module that knows how to invoke Codex or
locate the ``robinhood-trading`` MCP server. It has no order APIs; its only
permitted mutation is maintenance of the one project-owned saved scanner. Codex
returns schema-constrained JSON to a unique temporary output file; this module
validates it and atomically replaces the persistent snapshot.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

import config
from agent.market_cycle import JsonSnapshotProvider, SnapshotValidationError
from agent.market_calendar import regular_session
from agent.collection_telemetry import run_collection
from agent.staged_snapshot import (
    InstrumentMetadataCache,
    benchmark_bundle,
    candidate_bundle,
    final_scanner,
    rank_scanner_candidates,
)
from watcher.storage import atomic_json

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

ALLOWED_MCP_STATUSES = {
    "CONNECTED",
    "MCP_UNAVAILABLE",
    "MCP_NOT_AUTHENTICATED",
    "ROBINHOOD_ERROR",
}

# Independently audited against Robinhood's published tool inventory. Keeping
# this separate from config makes an accidental config-only addition fail
# closed before Codex starts.
SAFE_ROBINHOOD_MCP_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_realized_pnl",
        "get_pnl_trade_history",
        "search",
        "get_equity_historicals",
        "get_equity_fundamentals",
        "get_equity_price_book",
        "get_equity_technical_indicators",
        "get_earnings_results",
        "get_earnings_calendar",
        "get_indexes",
        "get_index_quotes",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
        "get_equity_tradability",
        "get_scans",
        "get_scanner_filter_specs",
        "create_scan",
        "run_scan",
        "update_scan_filters",
        "update_scan_config",
    }
)

CORE_MCP_TOOLS = frozenset(
    {
        "get_accounts", "get_portfolio", "get_realized_pnl",
        "get_equity_positions", "get_equity_orders", "get_scans",
        "get_scanner_filter_specs", "create_scan", "run_scan",
        "update_scan_filters", "update_scan_config",
    }
)
MARKET_DATA_MCP_TOOLS = frozenset(
    {"get_equity_quotes", "get_equity_historicals"}
)


class CycleAlreadyRunningError(RuntimeError):
    """Raised when another process owns the local market-cycle lock."""


class LocalCycleLock:
    """A nonblocking, process-safe lock whose kernel state survives stale files."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    def __enter__(self) -> LocalCycleLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CycleAlreadyRunningError(
                f"another analysis cycle holds {self.path}"
            ) from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        self._descriptor = descriptor
        return self

    def __exit__(self, *_: object) -> None:
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None


@dataclass(frozen=True)
class RefreshResult:
    snapshot: Mapping[str, Any]
    snapshot_path: Path
    bridge_status: str
    codex_cli_found: bool
    codex_authenticated: bool
    custom_mcp_configured: bool
    exec_returncode: int | None

    @property
    def connected(self) -> bool:
        return (
            self.bridge_status in {
                "OK", "BENCHMARK_FAILED", "CANDIDATE_COLLECTION_PARTIAL"
            }
            and self.snapshot.get("mcp_status") == "CONNECTED"
        )


class CodexMcpBridge:
    """Collect one fresh snapshot through non-trading ``codex exec``."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        snapshot_path: str | Path = "state/market_snapshot.json",
        prompt_path: str | Path = "prompts/refresh_snapshot.md",
        schema_path: str | Path = "schemas/market_snapshot.schema.json",
        lock_path: str | Path = "state/market_cycle.lock",
        command_runner: CommandRunner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], datetime] | None = None,
        staged: bool | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.snapshot_path = self._resolve(snapshot_path)
        self.prompt_path = self._resolve(prompt_path)
        self.schema_path = self._resolve(schema_path)
        self.lock_path = self._resolve(lock_path)
        self.command_runner = command_runner
        self.which = which
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Existing injected runners model the original single response. Tests
        # that exercise stages opt in explicitly; production is always staged.
        self.staged = command_runner is subprocess.run if staged is None else staged

    def _resolve(self, path: str | Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.project_dir / value

    def cycle_lock(self) -> LocalCycleLock:
        """Return the lock used to prevent overlapping collection/cycle work."""

        return LocalCycleLock(self.lock_path)

    def refresh(
        self,
        *,
        acquire_lock: bool = True,
        shadow_symbols: Sequence[str] = (),
    ) -> RefreshResult:
        """Refresh the snapshot, optionally acquiring the process lock."""

        if acquire_lock:
            try:
                with self.cycle_lock():
                    return self._refresh_unlocked(shadow_symbols=shadow_symbols)
            except CycleAlreadyRunningError:
                snapshot = self._failure_snapshot(
                    "MCP_UNAVAILABLE",
                    "another analysis cycle is already running; refresh was not started",
                )
                # The lock owner may be replacing the snapshot. Do not race it.
                return RefreshResult(
                    snapshot=snapshot,
                    snapshot_path=self.snapshot_path,
                    bridge_status="OVERLAPPING_CYCLE",
                    codex_cli_found=False,
                    codex_authenticated=False,
                    custom_mcp_configured=False,
                    exec_returncode=None,
                )
        return self._refresh_unlocked(shadow_symbols=shadow_symbols)

    def _refresh_unlocked(self, *, shadow_symbols: Sequence[str] = ()) -> RefreshResult:
        started_at = self._now()
        codex = self.which("codex")
        if not codex:
            return self._fail(
                "MCP_UNAVAILABLE",
                "CODEX_NOT_INSTALLED",
                "Codex CLI is not installed or is not on PATH",
                codex_found=False,
                codex_authenticated=False,
                custom_mcp=False,
            )

        try:
            login = self._run([codex, "login", "status"], timeout=30)
        except subprocess.TimeoutExpired:
            return self._fail(
                "MCP_UNAVAILABLE",
                "CODEX_TIMEOUT",
                "Codex CLI login-status check timed out",
                codex_found=True,
                codex_authenticated=False,
                custom_mcp=False,
            )
        except (OSError, subprocess.SubprocessError):
            return self._fail(
                "MCP_UNAVAILABLE",
                "CODEX_START_FAILED",
                "Codex CLI login-status check could not be started",
                codex_found=True,
                codex_authenticated=False,
                custom_mcp=False,
            )

        authenticated = (
            login.returncode == 0
            and "logged in" in (login.stdout + login.stderr).lower()
        )
        if not authenticated:
            return self._fail(
                "MCP_NOT_AUTHENTICATED",
                "CODEX_NOT_AUTHENTICATED",
                "Codex CLI is not authenticated; run codex login",
                codex_found=True,
                codex_authenticated=False,
                custom_mcp=False,
                returncode=login.returncode,
            )

        try:
            mcp_list = self._run([codex, "mcp", "list"], timeout=30)
        except subprocess.TimeoutExpired:
            return self._fail(
                "MCP_UNAVAILABLE",
                "CODEX_TIMEOUT",
                "Codex MCP configuration check timed out",
                codex_found=True,
                codex_authenticated=True,
                custom_mcp=False,
            )
        except (OSError, subprocess.SubprocessError):
            return self._fail(
                "MCP_UNAVAILABLE",
                "MCP_UNAVAILABLE",
                "Codex could not inspect its MCP configuration",
                codex_found=True,
                codex_authenticated=True,
                custom_mcp=False,
            )

        mcp_output = mcp_list.stdout + mcp_list.stderr
        custom_mcp = bool(
            mcp_list.returncode == 0
            and re.search(
                r"(?mi)^\s*robinhood-trading\s+.*\benabled\b", mcp_output
            )
        )
        if not custom_mcp:
            return self._fail(
                "MCP_UNAVAILABLE",
                "MCP_UNAVAILABLE",
                "enabled Codex MCP server robinhood-trading was not found",
                codex_found=True,
                codex_authenticated=True,
                custom_mcp=False,
                returncode=mcp_list.returncode,
            )

        if self.staged:
            return self._refresh_staged(
                codex,
                started_at=started_at,
                shadow_symbols=shadow_symbols,
            )

        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = self._temporary_path(".codex-output-", ".json")
        try:
            try:
                prompt = self.prompt_path.read_text(encoding="utf-8")
                prompt += (f"\nANALYSIS HISTORY TARGET: {config.ANALYSIS_CANDLES_TO_RETAIN} real 5-minute bars per candidate; "
                           f"unchanged minimum: {config.MIN_ANALYSIS_5_MINUTE_CANDLES}. Do not truncate for display.\n")
                normalized_shadow_symbols = sorted(
                    {
                        symbol.upper().strip()
                        for symbol in shadow_symbols
                        if re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", symbol.strip())
                    }
                )
                prompt += (
                    "\n\nOpen local shadow positions requiring fresh monitoring data: "
                    + (", ".join(normalized_shadow_symbols) if normalized_shadow_symbols else "NONE")
                    + ". Return their fresh bundles only in shadow_position_data."
                )
                command = self.build_exec_command(codex, output_path)
                completed = self._run(
                    command,
                    timeout=config.CODEX_EXEC_TIMEOUT_SECONDS,
                    input_text=prompt,
                )
            except subprocess.TimeoutExpired:
                return self._fail(
                    "MCP_UNAVAILABLE",
                    "CODEX_TIMEOUT",
                    f"codex exec exceeded {config.CODEX_EXEC_TIMEOUT_SECONDS} seconds",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                )
            except (OSError, subprocess.SubprocessError):
                return self._fail(
                    "MCP_UNAVAILABLE",
                    "CODEX_START_FAILED",
                    "codex exec could not be started",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                )

            if completed.returncode != 0:
                status = self._classify_failure(completed.stdout + completed.stderr)
                return self._fail(
                    status,
                    "CODEX_NONZERO_EXIT",
                    "codex exec did not complete the analysis-only Robinhood refresh",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                    returncode=completed.returncode,
                )
            if not output_path.exists():
                return self._fail(
                    "ROBINHOOD_ERROR",
                    "MISSING_OUTPUT",
                    "codex exec completed without producing snapshot output",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                    returncode=completed.returncode,
                )

            try:
                snapshot = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._fail(
                    "ROBINHOOD_ERROR",
                    "INVALID_JSON",
                    "codex exec returned invalid snapshot JSON",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                    returncode=completed.returncode,
                )
            if not isinstance(snapshot, Mapping):
                return self._fail(
                    "ROBINHOOD_ERROR",
                    "INVALID_MODEL_OUTPUT",
                    "codex exec returned a non-object JSON value",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                    returncode=completed.returncode,
                )

            try:
                self._validate_collection_timestamp(snapshot, started_at)
                normalized = self._normalize_snapshot(snapshot)
                normalized["generated_at"] = self._timestamp(self._now())
                self._validate_schema(normalized)
                JsonSnapshotProvider._reject_sensitive_keys(normalized)
                self._validate_refresh_envelope(normalized, started_at)
            except SnapshotValidationError as exc:
                return self._fail(
                    "ROBINHOOD_ERROR",
                    "INVALID_MODEL_OUTPUT",
                    f"snapshot validation failed: {exc}",
                    codex_found=True,
                    codex_authenticated=True,
                    custom_mcp=True,
                    returncode=completed.returncode,
                )

            self._write_snapshot(normalized)
            if normalized.get("mcp_status") == "CONNECTED":
                try:
                    JsonSnapshotProvider.from_path(
                        self.snapshot_path,
                        now=self._now(),
                        max_age_seconds=config.SNAPSHOT_MAX_AGE_SECONDS,
                    )
                except SnapshotValidationError as exc:
                    return self._fail(
                        "ROBINHOOD_ERROR",
                        "INVALID_SNAPSHOT",
                        f"persisted snapshot failed analysis validation: {exc}",
                        codex_found=True,
                        codex_authenticated=True,
                        custom_mcp=True,
                        returncode=completed.returncode,
                    )

            return RefreshResult(
                snapshot=normalized,
                snapshot_path=self.snapshot_path,
                bridge_status="OK",
                codex_cli_found=True,
                codex_authenticated=True,
                custom_mcp_configured=True,
                exec_returncode=completed.returncode,
            )
        finally:
            output_path.unlink(missing_ok=True)

    def _refresh_staged(
        self,
        codex: str,
        *,
        started_at: datetime,
        shadow_symbols: Sequence[str],
    ) -> RefreshResult:
        """Collect core, benchmarks, and small candidate batches, then merge."""

        wall_start = time.monotonic()
        stage_records: list[dict[str, Any]] = []
        total_deadline = wall_start + config.SNAPSHOT_OVERALL_TIMEOUT_SECONDS
        print("SNAPSHOT CORE: START", flush=True)
        try:
            core, record = self._invoke_stage(
                codex,
                "CORE",
                self._resolve("prompts/snapshot_core.md"),
                self._resolve("schemas/snapshot_core.schema.json"),
                config.CORE_SNAPSHOT_TIMEOUT_SECONDS,
                CORE_MCP_TOOLS,
            )
            stage_records.append(record)
            self._validate_collection_timestamp(core, started_at)
            JsonSnapshotProvider._reject_sensitive_keys(core)
            self._validate_core(core)
            # The enabled Robinhood inventory has no dedicated session-status
            # tool. Exchange-clock classification is deterministic local data,
            # not model inference or a cached quote.
            status, is_regular, _ = regular_session(self._now())
            core = dict(core)
            core["market"] = {
                "status": status,
                "is_regular_session": is_regular,
                "as_of": self._timestamp(self._now()),
            }
        except subprocess.TimeoutExpired as exc:
            stage_records.append(self._timeout_record("CORE", exc, time.monotonic() - wall_start))
            return self._stage_fail("CORE_TIMEOUT", "core snapshot stage timed out", stage_records, wall_start)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._stage_fail("CORE_START_FAILED", f"core stage failed: {type(exc).__name__}", stage_records, wall_start)
        except SnapshotValidationError as exc:
            reported_status = core.get("mcp_status") if isinstance(core, Mapping) else None
            return self._stage_fail(
                "CORE_FAILED", f"core snapshot invalid: {exc}", stage_records, wall_start,
                mcp_status=(reported_status if reported_status in ALLOWED_MCP_STATUSES else "ROBINHOOD_ERROR"),
            )
        print(f"SNAPSHOT CORE: OK ({record['total_duration_seconds']:.1f}s)", flush=True)

        raw_scanner = core.get("scanner", {})
        raw_candidates = raw_scanner.get("candidates", []) if isinstance(raw_scanner, Mapping) else []
        ranked = rank_scanner_candidates(
            [row for row in raw_candidates if isinstance(row, Mapping)]
        ) if core["market"]["is_regular_session"] else []
        print(f"SCANNER: {raw_scanner.get('result_count', 0)} RESULTS", flush=True)
        print("TOP CANDIDATES: " + (", ".join(row["symbol"] for row in ranked) or "NONE"), flush=True)

        candidate_symbols = [row["symbol"] for row in ranked]
        normalized_shadow = sorted({
            str(symbol).upper().strip() for symbol in shadow_symbols
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", str(symbol).strip())
        })
        deep_symbols = list(dict.fromkeys(candidate_symbols + normalized_shadow))
        requested_symbols = list(dict.fromkeys(["SPY", "QQQ"] + deep_symbols))
        print("MARKET DATA: START (SPY, QQQ + ranked candidates)", flush=True)
        market_stage_start = time.monotonic()
        rows: dict[str, Mapping[str, Any]] = {}
        failures: dict[str, str] = {}
        primary_timed_out = False
        try:
            market_data, record = self._invoke_stage(
                codex,
                "MARKET_DATA",
                self._resolve("prompts/snapshot_market_data.md"),
                self._resolve("schemas/snapshot_market_data.schema.json"),
                min(config.BENCHMARK_SNAPSHOT_TIMEOUT_SECONDS, self._remaining(total_deadline)),
                MARKET_DATA_MCP_TOOLS,
                suffix=self._market_prompt(requested_symbols, include_metadata=False),
            )
            stage_records.append(record)
            rows, failures = self._market_stage_rows(market_data, set(requested_symbols))
        except subprocess.TimeoutExpired as exc:
            primary_timed_out = True
            elapsed = time.monotonic() - market_stage_start
            stage_records.append(self._timeout_record("MARKET_DATA", exc, elapsed))
            failures.update({symbol: "compact market-data stage timed out" for symbol in deep_symbols})
            print(f"MARKET DATA: TIMEOUT ({elapsed:.1f}s); RETRYING BENCHMARKS INDIVIDUALLY", flush=True)
        except subprocess.CalledProcessError as exc:
            status = self._classify_failure((exc.stdout or "") + (exc.stderr or ""))
            if status in {"MCP_UNAVAILABLE", "MCP_NOT_AUTHENTICATED"}:
                return self._stage_fail(
                    "MARKET_DATA_CONNECTION_FAILED", "market-data MCP connection failed",
                    stage_records, wall_start, confirmed_core=core, ranked=ranked,
                    mcp_status=status,
                )
            failures.update({symbol: "compact market-data process failed" for symbol in requested_symbols})
        except (OSError, subprocess.SubprocessError) as exc:
            failures.update({symbol: f"compact market-data stage failed: {type(exc).__name__}" for symbol in requested_symbols})
        except SnapshotValidationError as exc:
            failures.update({symbol: f"compact market-data output invalid: {exc}" for symbol in requested_symbols})

        if primary_timed_out:
            for symbol in ("SPY", "QQQ"):
                print(f"BENCHMARK FALLBACK {symbol}: START", flush=True)
                fallback_start = time.monotonic()
                label = f"BENCHMARK_FALLBACK_{symbol}"
                try:
                    fallback, record = self._invoke_stage(
                        codex, label, self._resolve("prompts/snapshot_market_data.md"),
                        self._resolve("schemas/snapshot_market_data.schema.json"),
                        min(config.BENCHMARK_FALLBACK_TIMEOUT_SECONDS, self._remaining(total_deadline)),
                        MARKET_DATA_MCP_TOOLS,
                        suffix=self._market_prompt((symbol,), include_metadata=False),
                    )
                    stage_records.append(record)
                    fallback_rows, fallback_failures = self._market_stage_rows(fallback, {symbol})
                    rows.update(fallback_rows)
                    failures.update(fallback_failures)
                    if symbol in fallback_rows:
                        failures.pop(symbol, None)
                    elif symbol not in fallback_failures:
                        failures[symbol] = "individual benchmark retry omitted symbol"
                    print(f"BENCHMARK FALLBACK {symbol}: {'OK' if symbol in rows else 'DATA_UNAVAILABLE'} ({record['total_duration_seconds']:.1f}s)", flush=True)
                except subprocess.TimeoutExpired as exc:
                    elapsed = time.monotonic() - fallback_start
                    failures[symbol] = "individual benchmark retry timed out"
                    stage_records.append(self._timeout_record(label, exc, elapsed))
                    print(f"BENCHMARK FALLBACK {symbol}: DATA_UNAVAILABLE ({elapsed:.1f}s)", flush=True)
                except (OSError, subprocess.SubprocessError, SnapshotValidationError) as exc:
                    failures[symbol] = f"individual benchmark retry failed: {type(exc).__name__}"
                    stage_records.append({"stage": label, "status": "FAILED", "total_duration_seconds": round(time.monotonic() - fallback_start, 6)})
                    print(f"BENCHMARK FALLBACK {symbol}: DATA_UNAVAILABLE", flush=True)
        else:
            print(f"MARKET DATA: OK ({record['total_duration_seconds']:.1f}s)", flush=True)

        for symbol in requested_symbols:
            if symbol not in rows and symbol not in failures:
                failures[symbol] = "market-data stage omitted symbol"
        data_rows = {symbol: row for symbol, row in rows.items() if symbol in deep_symbols}
        data_failures = {symbol: failures[symbol] for symbol in deep_symbols if symbol in failures}

        merge_start = time.monotonic()
        cache = InstrumentMetadataCache(
            self._resolve(config.INSTRUMENT_METADATA_CACHE_PATH), now=self._now()
        )
        merge_now = self._now()
        benchmark_rows = [benchmark_bundle(symbol, rows.get(symbol), now=merge_now) for symbol in ("SPY", "QQQ")]
        market = {
            **core["market"], "direction": "UNKNOWN", "benchmarks": benchmark_rows,
            "volatility_context": None,
        }
        scanner = final_scanner(core, ranked)
        scanner_lookup = {row["symbol"]: row for row in ranked}
        candidate_data = [
            candidate_bundle(symbol, data_rows.get(symbol), scanner_row=scanner_lookup.get(symbol),
                             market_direction="UNKNOWN", cache=cache, failure=data_failures.get(symbol), now=merge_now,
                             max_quote_age_seconds=config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS,
                             strategy_id="MOMENTUM")
            for symbol in candidate_symbols
        ]
        shadow_data = [
            candidate_bundle(symbol, data_rows.get(symbol), scanner_row=scanner_lookup.get(symbol),
                             market_direction="UNKNOWN", cache=cache, failure=data_failures.get(symbol), now=merge_now,
                             max_quote_age_seconds=config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS,
                             strategy_id=None)
            for symbol in normalized_shadow
        ]
        snapshot: dict[str, Any] = {
            "data_source": "ROBINHOOD_MCP", "generated_at": self._timestamp(self._now()),
            "mcp_status": "CONNECTED", "mcp_access_path": "CUSTOM_MCP",
            "status_detail": "real read-only Robinhood data retrieved in bounded stages",
            "account": core["account"], "portfolio": core["portfolio"],
            "positions": core["positions"], "open_orders": core["open_orders"],
            "daily_realized_pnl": core["daily_realized_pnl"], "market": market,
            "scanner": scanner, "candidate_data": candidate_data,
            "scalp_candidate_data": [],
            "shadow_position_data": shadow_data,
            "connectivity_checks": {},
            "warnings": list(core.get("warnings", []))
                + [f"{symbol}: DATA_UNAVAILABLE ({detail})" for symbol, detail in sorted(data_failures.items())],
            "errors": list(core.get("errors", [])),
        }
        try:
            snapshot = self._normalize_snapshot(snapshot)
            direction = snapshot["market"]["direction"]
            for section in ("candidate_data", "scalp_candidate_data", "shadow_position_data"):
                for row in snapshot[section]:
                    row["market_direction"] = direction
            parse_done = time.monotonic()
            validation_start = time.monotonic()
            self._validate_schema(snapshot)
            JsonSnapshotProvider._reject_sensitive_keys(snapshot)
            self._validate_refresh_envelope(snapshot, started_at)
            schema_duration = time.monotonic() - validation_start
            write_start = time.monotonic()
            self._write_snapshot(snapshot)
            atomic_duration = time.monotonic() - write_start
            cache.save()
            JsonSnapshotProvider.from_path(self.snapshot_path, now=self._now(), max_age_seconds=config.SNAPSHOT_MAX_AGE_SECONDS)
        except SnapshotValidationError as exc:
            return self._stage_fail(
                "SNAPSHOT_MERGE_FAILED", f"merged snapshot invalid: {exc}",
                stage_records, wall_start, confirmed_core=core, ranked=ranked,
                mcp_status="CONNECTED",
            )
        merge_duration = parse_done - merge_start
        total = time.monotonic() - wall_start
        stage_records.append({
            "stage": "MERGE", "status": "OK", "json_parse_duration_seconds": 0.0,
            "schema_validation_duration_seconds": round(schema_duration, 6),
            "atomic_write_duration_seconds": round(atomic_duration, 6),
            "total_duration_seconds": round(merge_duration + schema_duration + atomic_duration, 6),
            "candidate_count": len(candidate_symbols),
            "candle_count": sum(len(row["candles"]) for row in candidate_data),
            "serialized_output_bytes": self.snapshot_path.stat().st_size,
        })
        benchmark_failed = any(symbol in failures for symbol in ("SPY", "QQQ"))
        bridge_status = (
            "BENCHMARK_FAILED" if benchmark_failed
            else "CANDIDATE_COLLECTION_PARTIAL" if data_failures
            else "OK"
        )
        self._save_timings(stage_records, total, bridge_status)
        print("SNAPSHOT MERGE: OK", flush=True)
        for symbol, detail in sorted(data_failures.items()):
            print(f"CANDIDATE {symbol}: DATA_UNAVAILABLE ({detail})", flush=True)
        print(f"SNAPSHOT TOTAL: {total:.1f}s", flush=True)
        return RefreshResult(snapshot, self.snapshot_path, bridge_status, True, True, True, 0)

    def _invoke_stage(
        self,
        codex: str,
        stage: str,
        prompt_path: Path,
        schema_path: Path,
        timeout: int,
        tools: frozenset[str],
        *,
        suffix: str = "",
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        if timeout <= 0:
            raise subprocess.TimeoutExpired([codex, "exec"], timeout)
        output = self._temporary_path(f".codex-{stage.lower()}-", ".json")
        started = time.monotonic()
        process_started_at = self._timestamp(self._now())
        prompt = prompt_path.read_text(encoding="utf-8") + suffix
        try:
            completed = self._run(
                self.build_exec_command(codex, output, schema_path=schema_path, enabled_tools=tools),
                timeout=timeout, input_text=prompt, stage=stage,
            )
            if completed.returncode != 0:
                raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
            parse_start = time.monotonic()
            try:
                raw = output.read_bytes()
                value = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotValidationError(f"{stage} returned invalid JSON") from exc
            parse_duration = time.monotonic() - parse_start
            if not isinstance(value, Mapping):
                raise SnapshotValidationError(f"{stage} returned a non-object")
            validation_start = time.monotonic()
            self._validate_against(value, schema_path)
            JsonSnapshotProvider._reject_sensitive_keys(value)
            schema_duration = time.monotonic() - validation_start
            record = {
                "stage": stage, "status": "OK", "timeout_seconds": timeout,
                "codex_process_started_at": process_started_at,
                "prompt_bytes": len(prompt.encode("utf-8")), "serialized_output_bytes": len(raw),
                "json_parse_duration_seconds": round(parse_duration, 6),
                "schema_validation_duration_seconds": round(schema_duration, 6),
                "total_duration_seconds": round(time.monotonic() - started, 6),
                "candidate_count": len(value.get("rows", value.get("scanner", {}).get("candidates", []))),
                "candle_count": sum(len(row.get("candles", [])) for row in value.get("rows", []) if isinstance(row, Mapping)),
                "news_context_retrieval_duration_seconds": 0.0,
            }
            if stage in {"BENCHMARK", "MARKET_DATA"} or stage.startswith("BENCHMARK_FALLBACK"):
                record["benchmark_data_duration_seconds"] = record["total_duration_seconds"]
            elif stage.startswith("CANDIDATE_BATCH"):
                record["candidate_data_duration_seconds"] = record["total_duration_seconds"]
            telemetry = getattr(completed, "telemetry_summary", None)
            if isinstance(telemetry, Mapping):
                for name in (
                    "mcp_connection_time_seconds", "time_to_first_mcp_call_seconds",
                    "final_response_or_process_exit_wait_seconds", "mcp_calls_started",
                    "mcp_calls_completed", "structured_output_first_seen_seconds",
                    "structured_output_bytes", "stdout_event_counts", "timeout_phase",
                ):
                    record[name] = telemetry.get(name)
                record["final_response_generation_duration_seconds"] = telemetry.get(
                    "final_response_or_process_exit_wait_seconds"
                )
                record["final_response_measurement_note"] = (
                    "measured from last completed MCP call to process exit; JSONL cannot separate generation from service/process wait"
                )
                intervals = telemetry.get("tool_intervals", [])
                if isinstance(intervals, list):
                    record["scanner_retrieval_duration_seconds"] = self._interval_span(
                        intervals, {"get_scans", "get_scanner_filter_specs", "create_scan", "update_scan_filters", "update_scan_config", "run_scan"}
                    )
                    record["account_data_duration_seconds"] = self._interval_span(
                        intervals, {"get_accounts", "get_portfolio", "get_realized_pnl", "get_equity_positions", "get_equity_orders"}
                    )
            return value, record
        finally:
            output.unlink(missing_ok=True)

    def _validate_against(self, value: Mapping[str, Any], schema_path: Path) -> None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.absolute_path) or "root"
            raise SnapshotValidationError(f"schema violation at {location}: {first.message}")

    def _validate_core(self, core: Mapping[str, Any]) -> None:
        if core.get("mcp_status") != "CONNECTED":
            raise SnapshotValidationError(f"core MCP status is {core.get('mcp_status')}")
        if not isinstance(core.get("account"), Mapping) or core["account"].get("is_agentic_account") is not True:
            raise SnapshotValidationError("Agentic account was not positively identified")
        market = core.get("market")
        if not isinstance(market, Mapping):
            raise SnapshotValidationError("market status object unavailable")
        scanner = core.get("scanner")
        if not isinstance(scanner, Mapping) or scanner.get("status") != "OK":
            raise SnapshotValidationError("scanner unavailable")
        if scanner.get("scan_name") != config.PROJECT_SCANNER_NAME:
            raise SnapshotValidationError("project scanner identity missing")

    def _market_stage_rows(self, value: Mapping[str, Any], expected: set[str]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
        rows: dict[str, Mapping[str, Any]] = {}
        failures: dict[str, str] = {}
        for row in value.get("rows", []):
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in expected or symbol in rows:
                raise SnapshotValidationError("market stage returned unexpected or duplicate symbol")
            rows[symbol] = row
        for failure in value.get("failures", []):
            symbol = str(failure.get("symbol", "")).upper()
            if symbol not in expected or symbol in failures or symbol in rows:
                raise SnapshotValidationError("market stage returned invalid failure symbol")
            failures[symbol] = str(failure.get("detail") or "data unavailable")
        return rows, failures

    @staticmethod
    def _market_prompt(symbols: Sequence[str], *, include_metadata: bool) -> str:
        return (
            "\nSYMBOLS (exactly): " + ", ".join(symbols)
            + f"\nCANDLE LIMIT: {config.ANALYSIS_CANDLES_TO_RETAIN}."
            + (" Retrieve sector and industry." if include_metadata else " Sector and industry may be null.")
        )

    @staticmethod
    def _remaining(deadline: float) -> int:
        return max(0, int(deadline - time.monotonic()))

    @staticmethod
    def _timeout_record(stage: str, exc: subprocess.TimeoutExpired, elapsed: float) -> dict[str, Any]:
        record: dict[str, Any] = {
            "stage": stage, "status": "TIMEOUT",
            "total_duration_seconds": round(elapsed, 6),
        }
        telemetry = getattr(exc, "telemetry_summary", None)
        if isinstance(telemetry, Mapping):
            for name in (
                "mcp_connection_time_seconds", "time_to_first_mcp_call_seconds",
                "final_response_or_process_exit_wait_seconds", "mcp_calls_started",
                "mcp_calls_completed", "structured_output_first_seen_seconds",
                "structured_output_bytes", "stdout_event_counts", "timeout_phase",
            ):
                record[name] = telemetry.get(name)
        return record

    @staticmethod
    def _interval_span(intervals: Sequence[Sequence[Any]], tools: set[str]) -> float | None:
        selected = [item for item in intervals if len(item) == 3 and item[0] in tools]
        if not selected:
            return None
        return round(max(float(item[2]) for item in selected) - min(float(item[1]) for item in selected), 6)

    def _save_timings(self, stages: list[dict[str, Any]], total: float, status: str) -> None:
        atomic_json(self._resolve(config.SNAPSHOT_STAGE_TIMINGS_PATH), {
            "generated_at": self._timestamp(self._now()), "status": status,
            "total_duration_seconds": round(total, 6), "stages": stages,
        })

    def _stage_fail(
        self,
        bridge_status: str,
        detail: str,
        stages: list[dict[str, Any]],
        wall_start: float,
        *,
        confirmed_core: Mapping[str, Any] | None = None,
        ranked: Sequence[Mapping[str, Any]] = (),
        mcp_status: str | None = None,
    ) -> RefreshResult:
        total = time.monotonic() - wall_start
        self._save_timings(stages, total, bridge_status)
        print(f"SNAPSHOT FAILED: {bridge_status} ({total:.1f}s)", flush=True)
        if confirmed_core is not None:
            status = mcp_status or "CONNECTED"
            market = confirmed_core.get("market", {})
            snapshot = {
                "data_source": "ROBINHOOD_MCP",
                "generated_at": self._timestamp(self._now()),
                "mcp_status": status,
                "mcp_access_path": "CUSTOM_MCP",
                "status_detail": detail,
                "account": confirmed_core["account"],
                "portfolio": confirmed_core["portfolio"],
                "positions": confirmed_core["positions"],
                "open_orders": confirmed_core["open_orders"],
                "daily_realized_pnl": confirmed_core["daily_realized_pnl"],
                "market": {**market, "direction": "UNKNOWN", "benchmarks": [], "volatility_context": None},
                "scanner": final_scanner(confirmed_core, ranked),
                "candidate_data": [], "scalp_candidate_data": [], "shadow_position_data": [],
                "connectivity_checks": {}, "warnings": [], "errors": [detail],
            }
            self._write_snapshot(snapshot)
            return RefreshResult(snapshot, self.snapshot_path, bridge_status, True, True, True, None)
        status = mcp_status or (
            "MCP_NOT_AUTHENTICATED" if "AUTH" in bridge_status
            else "MCP_UNAVAILABLE" if "CONNECTION" in bridge_status or "START" in bridge_status
            else "ROBINHOOD_ERROR"
        )
        return self._fail(status, bridge_status, detail, codex_found=True, codex_authenticated=True, custom_mcp=True)

    def build_exec_command(
        self,
        codex: str,
        output_path: Path,
        *,
        schema_path: Path | None = None,
        enabled_tools: Sequence[str] | None = None,
    ) -> list[str]:
        """Build safe argv with a structural Robinhood MCP tool allowlist."""

        configured_tools = tuple(enabled_tools or config.ROBINHOOD_MCP_ENABLED_TOOLS)
        unsafe = set(configured_tools) - SAFE_ROBINHOOD_MCP_TOOLS
        if not configured_tools or unsafe:
            raise RuntimeError(
                "unsafe or empty robinhood-trading MCP allowlist; failing closed"
            )
        enabled_tools = json.dumps(
            list(configured_tools), separators=(",", ":")
        )

        return [
            codex,
            "exec",
            "--json",
            "--approve-for-me",
            "--skip-git-repo-check",
            "--config",
            f'model_reasoning_effort="{config.CODEX_REASONING_EFFORT}"',
            "--config",
            f"mcp_servers.robinhood-trading.enabled_tools={enabled_tools}",
            "-C",
            str(self.project_dir),
            "--output-schema",
            str(schema_path or self.schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]

    def _run(
        self,
        command: Sequence[str],
        *,
        timeout: int,
        input_text: str | None = None,
        stage: str = "FULL",
    ) -> subprocess.CompletedProcess[str]:
        if self.command_runner is subprocess.run and len(command) > 1 and command[1] == "exec":
            output_path = None
            if "--output-last-message" in command:
                output_path = command[command.index("--output-last-message") + 1]
            return run_collection(list(command), cwd=self.project_dir, input_text=input_text,
                                  timeout=timeout,
                                  log_path=self.project_dir / "logs" / "snapshot_collection_events.jsonl",
                                  allowed_tools=SAFE_ROBINHOOD_MCP_TOOLS,
                                  stage=stage,
                                  output_path=output_path)
        return self.command_runner(
            list(command),
            cwd=self.project_dir,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _temporary_path(self, prefix: str, suffix: str) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=self.snapshot_path.parent,
        )
        os.close(descriptor)
        path = Path(name)
        path.unlink(missing_ok=True)
        return path

    def _validate_schema(self, snapshot: Mapping[str, Any]) -> None:
        try:
            schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
            errors = sorted(
                Draft202012Validator(schema).iter_errors(snapshot),
                key=lambda error: list(error.absolute_path),
            )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SnapshotValidationError(
                f"snapshot schema could not be loaded: {type(exc).__name__}"
            ) from exc
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.absolute_path) or "root"
            raise SnapshotValidationError(
                f"schema violation at {location}: {first.message}"
            )

    def _validate_refresh_envelope(
        self, snapshot: Mapping[str, Any], started_at: datetime
    ) -> None:
        if snapshot.get("data_source") != "ROBINHOOD_MCP":
            raise SnapshotValidationError("data_source must be ROBINHOOD_MCP")
        status = snapshot.get("mcp_status")
        if status not in ALLOWED_MCP_STATUSES:
            raise SnapshotValidationError(f"invalid mcp_status: {status!r}")
        if status != "CONNECTED":
            return

        scanner = snapshot.get("scanner")
        market = snapshot.get("market")
        market_open = isinstance(market, Mapping) and market.get("is_regular_session") is True
        permitted_scanner_statuses = {"OK"} if market_open else {"OK", "SKIPPED_MARKET_CLOSED"}
        if not isinstance(scanner, Mapping) or scanner.get("status") not in permitted_scanner_statuses:
            raise SnapshotValidationError("connected snapshot requires a successful scanner retrieval")
        if scanner.get("scan_name") != config.PROJECT_SCANNER_NAME:
            raise SnapshotValidationError("snapshot does not use the project scanner")
        permitted_lifecycle = {"REUSED", "CREATED", "UPDATED"} if scanner.get("status") == "OK" else {"REUSED", "SKIPPED"}
        if scanner.get("lifecycle_action") not in permitted_lifecycle:
            raise SnapshotValidationError("scanner lifecycle action is missing")

        candidates = scanner.get("candidates")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise SnapshotValidationError("scanner candidates are malformed")
        if len(candidates) > config.MAX_CANDIDATES_TO_ANALYZE:
            raise SnapshotValidationError("scanner candidate limit was exceeded")
        result_count = scanner.get("result_count")
        if not isinstance(result_count, int) or result_count < len(candidates):
            raise SnapshotValidationError("scanner result_count is inconsistent")
        scanner_symbols = {
            str(item.get("symbol", "")).upper()
            for item in candidates
            if isinstance(item, Mapping)
        }
        if "" in scanner_symbols or len(scanner_symbols) != len(candidates):
            raise SnapshotValidationError("scanner candidate symbols are invalid or duplicated")

        candidate_data = snapshot.get("candidate_data")
        if not isinstance(candidate_data, Sequence) or isinstance(
            candidate_data, (str, bytes)
        ):
            raise SnapshotValidationError("candidate_data is malformed")
        deep_symbols = {
            str(item.get("symbol", "")).upper()
            for item in candidate_data
            if isinstance(item, Mapping)
        }
        if len(deep_symbols) != len(candidate_data):
            raise SnapshotValidationError("candidate_data symbols are invalid or duplicated")
        if not deep_symbols.issubset(scanner_symbols):
            raise SnapshotValidationError(
                "candidate_data contains a symbol not returned by the scanner"
            )
        if market_open and deep_symbols != scanner_symbols:
            raise SnapshotValidationError(
                "every open-market scanner candidate requires deeper analysis data"
            )
        if (
            config.CONNECTIVITY_CHECK_SYMBOL in deep_symbols
            and config.CONNECTIVITY_CHECK_SYMBOL not in scanner_symbols
        ):
            raise SnapshotValidationError(
                "connectivity symbol cannot be used as a strategy fallback"
            )
        shadow_data = snapshot.get("shadow_position_data")
        if not isinstance(shadow_data, Sequence) or isinstance(shadow_data, (str, bytes)):
            raise SnapshotValidationError("shadow_position_data is malformed")
        shadow_symbols = [
            str(item.get("symbol", "")).upper()
            for item in shadow_data
            if isinstance(item, Mapping)
        ]
        if len(shadow_symbols) != len(shadow_data) or len(set(shadow_symbols)) != len(shadow_symbols):
            raise SnapshotValidationError("shadow-position symbols are invalid or duplicated")

    def _validate_collection_timestamp(
        self, snapshot: Mapping[str, Any], started_at: datetime
    ) -> None:
        """Reject missing or fabricated collection timestamps before normalization."""

        generated = JsonSnapshotProvider._parse_timestamp(
            snapshot.get("generated_at"), "generated_at"
        )
        if generated < started_at - timedelta(seconds=30):
            raise SnapshotValidationError("generated_at predates this refresh")
        if generated > self._now() + timedelta(seconds=30):
            raise SnapshotValidationError("generated_at is in the future")

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize objective market direction without broker implementation details."""

        normalized = dict(snapshot)
        market = normalized.get("market")
        if not isinstance(market, Mapping):
            return normalized
        market_copy = dict(market)
        raw_benchmarks = market_copy.get("benchmarks")
        benchmarks: list[Any] = []
        positive = 0
        negative = 0
        if isinstance(raw_benchmarks, Sequence) and not isinstance(
            raw_benchmarks, (str, bytes)
        ):
            for raw in raw_benchmarks:
                if not isinstance(raw, Mapping):
                    benchmarks.append(raw)
                    continue
                item = dict(raw)
                current = CodexMcpBridge._number(item.get("current_price"))
                previous = CodexMcpBridge._number(item.get("previous_close"))
                change = CodexMcpBridge._number(item.get("intraday_change_percent"))
                if change is None and current is not None and previous not in (None, 0):
                    change = (current - previous) / previous
                    item["intraday_change_percent"] = change
                comparisons = (
                    (current, CodexMcpBridge._number(item.get("vwap"))),
                    (
                        CodexMcpBridge._number(item.get("ema9")),
                        CodexMcpBridge._number(item.get("ema20")),
                    ),
                    (change, 0.0),
                )
                for left, right in comparisons:
                    if left is None or right is None or left == right:
                        continue
                    if left > right:
                        positive += 1
                    else:
                        negative += 1
                benchmarks.append(item)
        market_copy["benchmarks"] = benchmarks
        if positive + negative < 2:
            direction = "UNKNOWN"
        elif positive > negative:
            direction = "BULLISH"
        elif negative > positive:
            direction = "BEARISH"
        else:
            direction = "MIXED"
        market_copy["direction"] = direction
        normalized["market"] = market_copy
        for section in ("candidate_data", "scalp_candidate_data", "shadow_position_data"):
            rows = normalized.get(section, [])
            if isinstance(rows, list):
                normalized[section] = [
                    {**row, "candidate_quote_retrieved_at": row.get("candidate_quote_retrieved_at")}
                    if isinstance(row, Mapping) else row for row in rows
                ]
        checks = normalized.get("connectivity_checks", {})
        if isinstance(checks, dict):
            for check in checks.values():
                if isinstance(check, dict) and isinstance(check.get("market_data"), dict):
                    check["market_data"].setdefault("candidate_quote_retrieved_at", None)
        return normalized

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and number not in (float("inf"), float("-inf")) else None

    def _write_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.snapshot_path.name}.",
            suffix=".tmp",
            dir=self.snapshot_path.parent,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.snapshot_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _fail(
        self,
        mcp_status: str,
        bridge_status: str,
        detail: str,
        *,
        codex_found: bool,
        codex_authenticated: bool,
        custom_mcp: bool,
        returncode: int | None = None,
    ) -> RefreshResult:
        snapshot = self._failure_snapshot(mcp_status, detail)
        self._write_snapshot(snapshot)
        return RefreshResult(
            snapshot=snapshot,
            snapshot_path=self.snapshot_path,
            bridge_status=bridge_status,
            codex_cli_found=codex_found,
            codex_authenticated=codex_authenticated,
            custom_mcp_configured=custom_mcp,
            exec_returncode=returncode,
        )

    def _failure_snapshot(self, status: str, detail: str) -> dict[str, Any]:
        timestamp = self._now().isoformat().replace("+00:00", "Z")
        return {
            "data_source": "ROBINHOOD_MCP",
            "generated_at": timestamp,
            "mcp_status": status,
            "mcp_access_path": "UNKNOWN",
            "status_detail": detail,
            "account": {
                "is_agentic_account": False,
                "nickname": None,
                "account_type": None,
                "brokerage_trading_type": None,
                "state": None,
            },
            "portfolio": {
                "portfolio_value": None,
                "buying_power": None,
                "unleveraged_buying_power": None,
                "cash": None,
                "equity_value": None,
                "currency": None,
            },
            "positions": [],
            "open_orders": [],
            "daily_realized_pnl": None,
            "market": {
                "status": "UNKNOWN",
                "is_regular_session": None,
                "as_of": timestamp,
                "direction": "UNKNOWN",
                "benchmarks": [],
                "volatility_context": None,
            },
            "scanner": {
                "status": "NOT_ATTEMPTED",
                "scan_name": None,
                "scan_id": None,
                "lifecycle_action": "NOT_ATTEMPTED",
                "criteria": [],
                "sort_configuration": {"column": None, "direction": None},
                "result_count": 0,
                "candidates": [],
            },
            "candidate_data": [],
            "shadow_position_data": [],
            "connectivity_checks": {},
            "warnings": [],
            "errors": [detail],
        }

    @staticmethod
    def _classify_failure(output: str) -> str:
        lowered = output.lower()
        if any(
            marker in lowered
            for marker in (
                "not authenticated",
                "authentication required",
                "unauthorized",
                "oauth",
                "401",
                "login required",
            )
        ):
            return "MCP_NOT_AUTHENTICATED"
        if any(
            marker in lowered
            for marker in (
                "mcp",
                "tool inventory",
                "failed to connect",
                "connection failed",
                "could not resolve",
                "failed to lookup",
            )
        ):
            return "MCP_UNAVAILABLE"
        return "ROBINHOOD_ERROR"

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# Kept as a compatibility name for callers written against the first bridge.
CodexSnapshotRefresher = CodexMcpBridge


def diagnostic_lines(
    snapshot: Mapping[str, Any],
    *,
    bridge_status: str | None = None,
    custom_mcp_configured: bool | None = None,
) -> list[str]:
    account = snapshot.get("account", {})
    portfolio = snapshot.get("portfolio", {})
    market = snapshot.get("market", {})
    scanner = snapshot.get("scanner", {})
    checks = snapshot.get("connectivity_checks", {})
    positions = snapshot.get("positions", [])

    account_found = (
        isinstance(account, Mapping) and account.get("is_agentic_account") is True
    )
    portfolio_value = (
        portfolio.get("portfolio_value", portfolio.get("total_value"))
        if isinstance(portfolio, Mapping)
        else None
    )
    buying_power = (
        portfolio.get("buying_power") if isinstance(portfolio, Mapping) else None
    )
    market_status = (
        market.get("status", "UNKNOWN") if isinstance(market, Mapping) else "UNKNOWN"
    )
    scanner_status = (
        scanner.get("status", "UNKNOWN")
        if isinstance(scanner, Mapping)
        else "UNKNOWN"
    )
    scanner_name = (
        scanner.get("scan_name", "UNAVAILABLE")
        if isinstance(scanner, Mapping)
        else "UNAVAILABLE"
    )
    scanner_action = (
        scanner.get("lifecycle_action", "UNAVAILABLE")
        if isinstance(scanner, Mapping)
        else "UNAVAILABLE"
    )
    scanner_results = (
        scanner.get("result_count", "UNAVAILABLE")
        if isinstance(scanner, Mapping)
        else "UNAVAILABLE"
    )
    scan_age = None
    if isinstance(scanner, Mapping):
        try:
            queried = datetime.fromisoformat(
                str(scanner.get("query_executed_at", "")).replace("Z", "+00:00")
            )
            if queried.tzinfo is not None:
                scan_age = max(0.0, (datetime.now(timezone.utc) - queried.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    position_count: int | str = (
        len(positions)
        if isinstance(positions, Sequence) and not isinstance(positions, (str, bytes))
        else "UNKNOWN"
    )
    collection_timed_out = bridge_status == "CODEX_TIMEOUT"
    if collection_timed_out:
        # Failure-envelope defaults are not observations about the account or
        # scanner. Tools may have completed before final response generation hung.
        position_count = "UNKNOWN"
        scanner_status = "UNKNOWN_COLLECTION_INCOMPLETE"
        scanner_action = scanner_results = "UNAVAILABLE"

    lines = [
        f"DATA SOURCE: {snapshot.get('data_source', 'UNKNOWN')}",
        f"MCP STATUS: {'UNKNOWN_COLLECTION_TIMEOUT' if collection_timed_out else snapshot.get('mcp_status', 'UNKNOWN')}",
    ]
    if bridge_status is not None:
        lines.append(f"BRIDGE STATUS: {bridge_status}")
    lines.extend(
        [
            f"AGENTIC ACCOUNT: {'NOT_VERIFIED' if collection_timed_out else 'FOUND' if account_found else 'NOT_FOUND'}",
            "PORTFOLIO VALUE: "
            + str(portfolio_value if portfolio_value is not None else "UNAVAILABLE"),
            "BUYING POWER: "
            + str(buying_power if buying_power is not None else "UNAVAILABLE"),
            f"POSITIONS: {position_count}",
            f"MARKET STATUS: {market_status}",
            f"SCANNER: {scanner_status}",
            f"SCAN NAME: {scanner_name}",
            f"SCAN ACTION: {scanner_action}",
            f"SCAN RESULTS: {scanner_results}",
        ]
    )
    if isinstance(scanner, Mapping) and scanner.get("status") == "OK":
        provider_raw = scanner.get("provider_raw_count", scanner_results)
        after_provider = scanner.get("after_provider_filters_count", scanner_results)
        after_local = scanner.get("after_local_filters_count", len(scanner.get("candidates", [])))
        top_n = scanner.get("top_n_count", len(scanner.get("candidates", [])))
        lines.append(
            "MOMENTUM SCANNER: "
            f"provider_raw={provider_raw} "
            f"saved_definitions={scanner.get('saved_definition_count', 'UNAVAILABLE')} "
            f"after_provider_filters={after_provider} "
            f"after_local_filters={after_local} top_n={top_n} "
            f"scan_age={'UNAVAILABLE' if scan_age is None else f'{scan_age:.1f}s'} "
            f"scan_config={scanner_name}"
        )
        lines.append(
            "SCAN SEMANTICS: "
            f"definition={scanner_action} "
            f"results={scanner.get('results_source', 'UNAVAILABLE')}"
        )
    candidate_rows = snapshot.get("candidate_data", [])
    if isinstance(candidate_rows, Sequence) and not isinstance(candidate_rows, (str, bytes)):
        provider_ok = sum(
            isinstance(row, Mapping) and row.get("provider_status") == "OK"
            for row in candidate_rows
        )
        fresh = sum(
            isinstance(row, Mapping) and row.get("quote_status") == "FRESH"
            for row in candidate_rows
        )
        stale = sum(
            isinstance(row, Mapping) and row.get("quote_status") == "STALE"
            for row in candidate_rows
        )
        unavailable = len(candidate_rows) - fresh - stale
        lines.append(
            "MOMENTUM QUOTES: "
            f"provider_ok={provider_ok}/{len(candidate_rows)} fresh={fresh} "
            f"stale={stale} unavailable={unavailable} "
            f"freshness_limit={config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS}s"
        )
    if bridge_status == "BENCHMARK_FAILED":
        lines.append("BENCHMARK: FAILED")
    if custom_mcp_configured is not None:
        lines.append(
            "CUSTOM MCP robinhood-trading: "
            + ("CONFIGURED" if custom_mcp_configured else "NOT_CONFIGURED")
        )
    return lines
