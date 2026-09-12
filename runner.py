"""Local entry point for non-trading Robinhood analysis and shadow simulation."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Mapping, Sequence

import config
from agent.market_cycle import (
    JsonSnapshotProvider,
    MarketCycle,
    SnapshotValidationError,
    is_regular_market_hours,
)
from agent.codex_mcp_bridge import (
    CodexMcpBridge,
    CycleAlreadyRunningError,
    diagnostic_lines,
    LocalCycleLock,
)
from robinhood_mcp import (
    DirectRobinhoodMcpClient,
    DirectMcpUnavailable,
    RobinhoodAuthenticationRequired,
)
from robinhood_mcp.normalization import normalized_account, normalized_quotes
from robinhood_mcp.snapshot import DirectSnapshotCollector
from robinhood_mcp.client import ALLOWED_TOOLS
from agent.llm_reasoning_bridge import LlmReasoningBridge
from shadow.performance import ShadowPerformance
from shadow.portfolio import ShadowPortfolio
from shadow.execution import ShadowExecutionEngine
from execution.shadow_executor import ShadowExecutor
from watcher.fast_watcher import FastPositionWatcher
from watcher.quote_provider import RobinhoodDirectQuoteProvider, SnapshotQuoteProvider
from watcher.scheduler import SlowMarketLoop
from watcher.status import print_status
from watcher.storage import atomic_json
from agent.cycle_diagnostics import cycle_diagnostic_lines
from agent.candidate_context import CandidateContextStore, contexts_from_cycle
from watcher.candidate_watcher import FastCandidateWatcher
from event_driven.orchestrator import ShadowEventOrchestrator
from event_driven.reasoning import EventDrivenReasoningProvider

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = PROJECT_DIR / "state" / "market_snapshot.json"
SHADOW_STATE = PROJECT_DIR / "state" / "shadow_portfolio.json"
SHADOW_TRADES = PROJECT_DIR / "state" / "shadow_trades.jsonl"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local Robinhood research or shadow simulation without orders."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run exactly one cycle")
    mode.add_argument("--loop", action="store_true", help="run during market hours")
    mode.add_argument("--shadow-status", action="store_true", help="read-only two-speed monitoring status")
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="refresh through the configured Robinhood MCP provider and run one cycle",
    )
    mode.add_argument(
        "--shadow-summary",
        action="store_true",
        help="print local shadow portfolio performance without contacting Robinhood",
    )
    mode.add_argument(
        "--reset-shadow",
        action="store_true",
        help="interactively reset only local shadow portfolio files",
    )
    mode.add_argument(
        "--robinhood-auth",
        action="store_true",
        help="authorize this Python MCP client in a browser; never invokes an order tool",
    )
    mode.add_argument(
        "--robinhood-mcp-check",
        action="store_true",
        help="verify direct MCP tools, Agentic account, and one read-only quote",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"normalized read-only data file (default: {DEFAULT_SNAPSHOT})",
    )
    return parser.parse_args(argv)


def load_provider(path: Path) -> JsonSnapshotProvider:
    return JsonSnapshotProvider.from_path(path)


def run_analysis(
    snapshot: Path, shadow_portfolio: ShadowPortfolio | None = None,
    *, reasoning_provider=None,
) -> dict[str, object]:
    provider = load_provider(snapshot)
    return MarketCycle(
        provider,
        state_path=PROJECT_DIR / "state" / "session.json",
        logs_dir=PROJECT_DIR / "logs",
        shadow_portfolio=shadow_portfolio,
        reasoning_bridge=(reasoning_provider or LlmReasoningBridge(project_dir=PROJECT_DIR)),
    ).run()


def refresh_and_run(
    snapshot: Path,
    shadow_portfolio: ShadowPortfolio | None = None,
    *,
    direct_client: DirectRobinhoodMcpClient | None = None,
    context_store: CandidateContextStore | None = None,
    event_orchestrator: ShadowEventOrchestrator | None = None,
    reasoning_provider=None,
) -> tuple[int, dict[str, object] | None]:
    cycle_started_at = datetime.now(timezone.utc)
    cycle_start = time.monotonic()
    if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP":
        if direct_client is None:
            raise RuntimeError("DIRECT_MCP requires an active persistent client")
        bridge = DirectSnapshotCollector(
            direct_client, project_dir=PROJECT_DIR, snapshot_path=snapshot
        )
        cycle_lock = LocalCycleLock(PROJECT_DIR / "state" / "market_cycle.lock")
    elif config.ROBINHOOD_DATA_PROVIDER == "LEGACY_CODEX_MCP":
        bridge = CodexMcpBridge(project_dir=PROJECT_DIR, snapshot_path=snapshot)
        cycle_lock = bridge.cycle_lock()
    else:
        raise RuntimeError("unsupported ROBINHOOD_DATA_PROVIDER")
    try:
        shadow_portfolio = shadow_portfolio or (
            ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES)
            if config.MODE == "SHADOW_TRADING"
            else None
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail = f"local shadow state is invalid: {type(exc).__name__}"
        _log_refresh_failure(
            {"data_source": "ROBINHOOD_MCP", "mcp_status": "ROBINHOOD_ERROR"},
            "INVALID_SHADOW_STATE",
            detail=detail,
        )
        print("DATA SOURCE: ROBINHOOD_MCP")
        print("MCP STATUS: NOT_CONTACTED")
        print("BRIDGE STATUS: INVALID_SHADOW_STATE")
        print(f"ANALYSIS STATUS: NOT_RUN ({detail})")
        return 3, None
    shadow_symbols = (
        [position.symbol for position in shadow_portfolio.snapshot().open_positions]
        if shadow_portfolio is not None
        else []
    )
    try:
        # Hold the same nonblocking lock through collection, validation,
        # deterministic analysis, and logging. This also prevents a second
        # process from replacing the snapshot while this cycle consumes it.
        with cycle_lock:
            print(
                ("DIRECT ROBINHOOD MCP: CONNECTED\n" if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP" else "")
                + "SNAPSHOT REFRESH: STARTED "
                + (f"(direct, target={snapshot})" if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP"
                   else f"(legacy Codex, overall_timeout={config.SNAPSHOT_OVERALL_TIMEOUT_SECONDS}s, target={snapshot})"),
                flush=True,
            )
            snapshot_start = time.monotonic()
            snapshot_started_at = datetime.now(timezone.utc)
            refresh = (
                bridge.refresh(shadow_symbols=shadow_symbols)
                if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP"
                else bridge.refresh(acquire_lock=False, shadow_symbols=shadow_symbols)
            )
            snapshot_duration = time.monotonic() - snapshot_start
            snapshot_completed_at = datetime.now(timezone.utc)
            print("SNAPSHOT REFRESH: FINISHED", flush=True)
            for line in diagnostic_lines(
                refresh.snapshot,
                bridge_status=refresh.bridge_status,
                custom_mcp_configured=(
                    None if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP"
                    else getattr(refresh, "custom_mcp_configured", False)
                ),
            ):
                print(line)

            if not refresh.connected:
                failed_timing = {
                    "snapshot_started_at": snapshot_started_at.isoformat(),
                    "snapshot_completed_at": snapshot_completed_at.isoformat(),
                    "snapshot_duration": snapshot_duration,
                    "analysis_started_at": None,
                    "reasoning_duration": None,
                    "local_analysis_duration": None,
                    "total_cycle_duration": time.monotonic() - cycle_start,
                    "next_cycle_target": None,
                    "status": "RESEARCH_FAILED",
                }
                atomic_json(PROJECT_DIR / "state" / "slow_loop_status.json", failed_timing)
                _log_refresh_failure(refresh.snapshot, refresh.bridge_status, timing=failed_timing)
                print("ANALYSIS STATUS: NOT_RUN")
                return 3, None
            if refresh.bridge_status == "BENCHMARK_FAILED":
                failed_timing = {
                    "snapshot_started_at": snapshot_started_at.isoformat(),
                    "snapshot_completed_at": snapshot_completed_at.isoformat(),
                    "snapshot_duration": snapshot_duration,
                    "analysis_started_at": None,
                    "reasoning_duration": None,
                    "local_analysis_duration": None,
                    "total_cycle_duration": time.monotonic() - cycle_start,
                    "next_cycle_target": None,
                    "status": "INFRASTRUCTURE_BLOCKED",
                }
                atomic_json(PROJECT_DIR / "state" / "slow_loop_status.json", failed_timing)
                _log_refresh_failure(refresh.snapshot, refresh.bridge_status, timing=failed_timing)
                print("ANALYSIS STATUS: INFRASTRUCTURE_BLOCKED")
                return 3, None
            if event_orchestrator is not None:
                event_orchestrator.ingest_scanner_snapshot(
                    refresh.snapshot, now=snapshot_completed_at
                )
            try:
                analysis_started_at = datetime.now(timezone.utc)
                result = run_analysis(
                    snapshot, shadow_portfolio,
                    reasoning_provider=reasoning_provider,
                )
            except SnapshotValidationError as exc:
                _log_refresh_failure(
                    refresh.snapshot,
                    exc.status,
                    detail=str(exc),
                )
                print(f"SNAPSHOT STATUS: {exc.status}")
                print(f"ANALYSIS STATUS: NOT_RUN ({exc})")
                return 3, None
    except CycleAlreadyRunningError as exc:
        _log_refresh_failure(
            {"data_source": "ROBINHOOD_MCP", "mcp_status": "MCP_UNAVAILABLE"},
            "OVERLAPPING_CYCLE",
            detail=str(exc),
        )
        print("DATA SOURCE: ROBINHOOD_MCP")
        print("MCP STATUS: MCP_UNAVAILABLE")
        print("BRIDGE STATUS: OVERLAPPING_CYCLE")
        print(f"ANALYSIS STATUS: NOT_RUN ({exc})")
        return 3, None
    except Exception as exc:
        # Acquisition/orchestration failures must never fall through to an old
        # snapshot. Keep the message credential-free and fail closed.
        detail = f"bridge failed: {type(exc).__name__}"
        _log_refresh_failure(
            {"data_source": "ROBINHOOD_MCP", "mcp_status": "ROBINHOOD_ERROR"},
            "BRIDGE_ERROR",
            detail=detail,
        )
        print("DATA SOURCE: ROBINHOOD_MCP")
        print("MCP STATUS: ROBINHOOD_ERROR")
        print("BRIDGE STATUS: BRIDGE_ERROR")
        print(f"ANALYSIS STATUS: NOT_RUN ({detail})")
        return 3, None

    total = time.monotonic() - cycle_start
    reasoning = result.get("llm_reasoning", {}).get("trace", {}).get("reasoning_duration_seconds", 0)
    timing = dict(cycle_started_at=cycle_started_at.isoformat(), snapshot_started_at=snapshot_started_at.isoformat(),
                  snapshot_completed_at=snapshot_completed_at.isoformat(), analysis_started_at=analysis_started_at.isoformat(),
                  candidate_quotes=[{"symbol": r.get("symbol"), "quote_as_of": r.get("quote_as_of"),
                                     "candidate_quote_retrieved_at": r.get("candidate_quote_retrieved_at")}
                                    for r in refresh.snapshot.get("candidate_data", [])], snapshot_duration=snapshot_duration,
                  reasoning_duration=reasoning, local_analysis_duration=max(0, total - snapshot_duration - reasoning),
                  total_cycle_duration=total, status="CYCLE_COMPLETED", next_cycle_target=None)
    result["cycle_timing"] = timing
    if config.MODE == "SHADOW_TRADING" and config.SHADOW_ENTRY_VIA_FAST_WATCHLIST:
        context_store = context_store or CandidateContextStore(
            PROJECT_DIR / config.CANDIDATE_WATCHLIST_PATH
        )
        contexts = contexts_from_cycle(result, now=analysis_started_at)
        open_symbols = (
            [position.symbol for position in shadow_portfolio.snapshot().open_positions]
            if shadow_portfolio is not None else []
        )
        reasoning_trace = result.get("llm_reasoning", {}).get("trace", {})
        context_store.replace(
            contexts, now=analysis_started_at, exclude_symbols=open_symbols,
            preserve_research=(
                isinstance(reasoning_trace, Mapping)
                and reasoning_trace.get("status") == "CACHED"
            ),
        )
        if event_orchestrator is not None:
            event_orchestrator.ingest_slow_cycle(
                result, contexts, now=analysis_started_at,
                snapshot=refresh.snapshot, scanner_already_ingested=True,
            )
        result["candidate_contexts"] = [context.to_dict() for context in contexts]
        print("FAST-WATCH CONTEXT:")
        if contexts:
            for context in contexts:
                print(
                    f"{context.symbol} slow score: {context.slow_context_score:.3f} "
                    f"context TTL: {config.CANDIDATE_CONTEXT_TTL_SECONDS}s watchlist: YES "
                    f"admission: {context.metadata.get('admission_reason')}"
                )
                if context.signal_quality_failures:
                    print(
                        "  SIGNAL QUALITY WARNINGS: "
                        + ", ".join(context.signal_quality_failures)
                    )
        else:
            print(
                "NONE "
                f"(no completed slow result met admission score "
                f"{config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE})"
            )
    atomic_json(PROJECT_DIR / "state" / "slow_loop_status.json", timing)
    log_path = PROJECT_DIR / "logs" / f"{cycle_started_at.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "SLOW_CYCLE_TIMING", "mode": config.MODE, **timing}) + "\n")
    print(f"ANALYSIS STATUS: {result['decision']['type']}")
    for line in cycle_diagnostic_lines(result):
        print(line)
    if config.MODE == "SHADOW_TRADING":
        summary = result.get("shadow_portfolio")
        if isinstance(summary, Mapping):
            print(f"SHADOW EQUITY: {summary.get('current_equity')}")
            print(f"SHADOW CASH: {summary.get('cash')}")
            print(f"SHADOW OPEN POSITIONS: {summary.get('open_positions')}")
        print(
            "SHADOW ENTRIES OPENED: "
            f"{len(result.get('new_shadow_positions_opened', []))}"
        )
        print(
            "SHADOW POSITIONS EXITED: "
            f"{len(result.get('shadow_positions_exited', []))}"
        )
    return 0, result


def robinhood_auth() -> int:
    """Run only this application's independent browser OAuth setup."""

    try:
        with DirectRobinhoodMcpClient(interactive_auth=True):
            pass
    except Exception as exc:
        print(f"ROBINHOOD DIRECT MCP: AUTH FAILED ({type(exc).__name__})")
        print("No Robinhood order or account mutation was attempted.")
        return 3
    print("ROBINHOOD DIRECT MCP: AUTHENTICATED")
    print(f"SERVER: {config.ROBINHOOD_MCP_SERVER_URL}")
    return 0


def robinhood_mcp_check() -> int:
    """Read-only direct connectivity check; never exposes or invokes order tools."""

    started = time.monotonic()
    try:
        with DirectRobinhoodMcpClient() as client:
            accounts = client.call_readonly("get_accounts")
            account, _ = normalized_account(accounts.value)
            quote_started = time.monotonic()
            quote = client.get_quotes([config.CONNECTIVITY_CHECK_SYMBOL])
            quote_latency = time.monotonic() - quote_started
            rows = normalized_quotes(
                quote.value,
                [config.CONNECTIVITY_CHECK_SYMBOL],
                retrieved_at=datetime.now(timezone.utc),
            )
            read_only_available = sorted(
                name for name in client.tools
                if name in ALLOWED_TOOLS
            )
            quote_ok = config.CONNECTIVITY_CHECK_SYMBOL in rows
            print("DIRECT MCP CONNECTION: OK")
            print("AUTH: OK")
            print(f"READ-ONLY/SCANNER TOOLS DISCOVERED: {len(read_only_available)}")
            print("SAFE TOOL INVENTORY: " + ", ".join(read_only_available))
            print(f"AGENTIC ACCOUNT: {'FOUND' if account['is_agentic_account'] else 'NOT_FOUND'}")
            print(f"QUOTE TEST: {'OK' if quote_ok else 'FAILED'}")
            print(f"QUOTE LATENCY: {quote_latency:.3f}s")
            print(f"LATENCY: {time.monotonic() - started:.3f}s")
            return 0 if quote_ok else 3
    except RobinhoodAuthenticationRequired:
        print("DIRECT MCP CONNECTION: NOT_AUTHENTICATED")
        print("Run: python runner.py --robinhood-auth")
        return 3
    except DirectMcpUnavailable as exc:
        print("DIRECT MCP CONNECTION: UNAVAILABLE")
        print(f"ERROR: {type(exc).__name__}")
        return 3


def print_shadow_summary() -> int:
    try:
        portfolio = ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"SHADOW SUMMARY UNAVAILABLE: invalid local state ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 3
    summary = ShadowPerformance().summarize(portfolio.state)
    print("SHADOW TRADING SUMMARY")
    labels = (
        ("Starting capital", "starting_capital"),
        ("Current equity", "current_equity"),
        ("Net P&L", "total_net_pnl"),
        ("Return", "return_percent"),
        ("Open positions", "open_positions"),
        ("Closed trades", "closed_trades"),
        ("Win rate", "win_rate"),
        ("Average win", "average_win"),
        ("Average loss", "average_loss"),
        ("Profit factor", "profit_factor"),
        ("Expectancy", "expectancy_per_trade"),
        ("Maximum drawdown", "maximum_drawdown"),
    )
    for label, key in labels:
        value = summary.get(key)
        print(f"{label}: {'UNAVAILABLE' if value is None else value}")
    print("Recent closed trades:")
    for trade in portfolio.state.closed_positions[-5:]:
        print(
            f"  {trade.symbol} {trade.exit_reason} net={trade.net_pnl:.2f} "
            f"held={trade.holding_time_minutes:.1f}m"
        )
    return 0


def reset_shadow_state() -> int:
    confirmation = input(
        "Type RESET SHADOW to delete only local shadow portfolio/trade state: "
    )
    if confirmation != "RESET SHADOW":
        print("Shadow reset cancelled.")
        return 1
    removed: list[str] = []
    for path in (SHADOW_STATE, SHADOW_TRADES):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    print("Shadow state reset. Robinhood was not contacted.")
    if removed:
        print("Removed: " + ", ".join(removed))
    return 0


def _log_refresh_failure(
    snapshot: Mapping[str, object],
    bridge_status: str,
    *,
    detail: str | None = None,
    timing: Mapping[str, object] | None = None,
) -> None:
    value = snapshot
    if not detail:
        raw_detail = value.get("status_detail")
        detail = raw_detail if isinstance(raw_detail, str) else "snapshot refresh failed"
    now = datetime.now(timezone.utc)
    record = {
        "timestamp": now.isoformat(),
        "mode": config.MODE,
        "event": "SNAPSHOT_REFRESH_FAILED",
        "data_source": value.get("data_source", "ROBINHOOD_MCP"),
        "mcp_status": value.get("mcp_status", "ROBINHOOD_ERROR"),
        "bridge_status": bridge_status,
        "error": detail,
        "analysis_only": True,
        "executed": False,
        "cycle_timing": dict(timing) if timing else None,
    }
    logs_dir = PROJECT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{now.date().isoformat()}.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 12):
        print("Python 3.12 or newer is required.", file=sys.stderr)
        return 2
    if config.MODE not in {"ANALYSIS_ONLY", "SHADOW_TRADING"}:
        print("Unsupported MODE; failing closed.", file=sys.stderr)
        return 2

    args = parse_args(argv)
    if args.robinhood_auth:
        return robinhood_auth()
    if args.robinhood_mcp_check:
        return robinhood_mcp_check()
    if args.shadow_status:
        return print_status(
            SHADOW_STATE,
            PROJECT_DIR / "state" / "fast_watcher_status.json",
            PROJECT_DIR / "state" / "slow_loop_status.json",
            PROJECT_DIR / "state" / "candidate_states.json",
        )
    if args.shadow_summary:
        return print_shadow_summary()
    try:
        # One process owns the shared shadow state for the WHOLE loop lifetime,
        # including time between slow cycles. Also excludes --once and reset.
        with LocalCycleLock(PROJECT_DIR / "state" / "shadow_runner.lock"):
            if args.reset_shadow:
                return reset_shadow_state()
            snapshot = args.snapshot.resolve()
            if args.once or args.refresh:
                event_orchestrator = (
                    ShadowEventOrchestrator(
                        state_path=PROJECT_DIR / "state" / "candidate_states.json",
                        event_log_path=PROJECT_DIR / "logs" / "market_events.jsonl",
                        terminal_output=True,
                    ) if config.MODE == "SHADOW_TRADING" else None
                )
                reasoning_provider = EventDrivenReasoningProvider(
                    LlmReasoningBridge(project_dir=PROJECT_DIR)
                )
                if event_orchestrator is not None:
                    event_orchestrator.start()
                if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP":
                    try:
                        with DirectRobinhoodMcpClient() as client:
                            status, _ = refresh_and_run(
                                snapshot, direct_client=client,
                                event_orchestrator=event_orchestrator,
                                reasoning_provider=reasoning_provider,
                            )
                            return status
                    except RobinhoodAuthenticationRequired:
                        print("DATA SOURCE: ROBINHOOD_MCP")
                        print("MCP STATUS: MCP_NOT_AUTHENTICATED")
                        print("BRIDGE STATUS: DIRECT_MCP_NOT_AUTHENTICATED")
                        print("ANALYSIS STATUS: NOT_RUN")
                        print("Run: python runner.py --robinhood-auth")
                        return 3
                    except DirectMcpUnavailable as exc:
                        print("DATA SOURCE: ROBINHOOD_MCP")
                        print("MCP STATUS: MCP_UNAVAILABLE")
                        print("BRIDGE STATUS: DIRECT_MCP_UNAVAILABLE")
                        print(f"ANALYSIS STATUS: NOT_RUN ({type(exc).__name__})")
                        return 3
                    finally:
                        if event_orchestrator is not None:
                            event_orchestrator.stop()
                try:
                    status, _ = refresh_and_run(
                        snapshot, event_orchestrator=event_orchestrator,
                        reasoning_provider=reasoning_provider,
                    )
                    return status
                finally:
                    if event_orchestrator is not None:
                        event_orchestrator.stop()
            portfolio = ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES) if config.MODE == "SHADOW_TRADING" else None
            context_store = (
                CandidateContextStore(PROJECT_DIR / config.CANDIDATE_WATCHLIST_PATH)
                if portfolio is not None and config.SHADOW_ENTRY_VIA_FAST_WATCHLIST
                else None
            )
            def has_shadow_work():
                if portfolio and portfolio.snapshot().open_positions:
                    return True
                now = datetime.now(timezone.utc)
                return bool(context_store and any(not item.expired_at(now) for item in context_store.snapshot()))
            watcher = None
            event_orchestrator = (
                ShadowEventOrchestrator(
                    state_path=PROJECT_DIR / "state" / "candidate_states.json",
                    event_log_path=PROJECT_DIR / "logs" / "market_events.jsonl",
                    terminal_output=True,
                ) if portfolio is not None else None
            )
            if event_orchestrator is not None and portfolio is not None:
                event_orchestrator.recover_open_positions(
                    portfolio.snapshot().open_positions
                )
            reasoning_provider = EventDrivenReasoningProvider(
                LlmReasoningBridge(project_dir=PROJECT_DIR)
            )
            if config.ROBINHOOD_DATA_PROVIDER == "DIRECT_MCP":
                try:
                    with DirectRobinhoodMcpClient() as client:
                        if portfolio is not None and config.FAST_WATCHER_ENABLED:
                            if config.FAST_QUOTE_PROVIDER != "DIRECT_MCP":
                                print("Unsupported FAST_QUOTE_PROVIDER for direct mode; failing closed.")
                                return 2
                            quote_provider = RobinhoodDirectQuoteProvider(client)
                            candidate_watcher = (
                                FastCandidateWatcher(
                                    context_store,
                                    ShadowExecutionEngine(portfolio),
                                    events_path=PROJECT_DIR / "logs" / "fast_candidate_watcher.jsonl",
                                    score_history_path=PROJECT_DIR / config.CANDIDATE_SCORE_HISTORY_PATH,
                                    event_orchestrator=event_orchestrator,
                                )
                                if context_store is not None else None
                            )
                            watcher = FastPositionWatcher(
                                portfolio, quote_provider, ShadowExecutor(ShadowExecutionEngine(portfolio)),
                                status_path=PROJECT_DIR / "state" / "fast_watcher_status.json",
                                events_path=PROJECT_DIR / "logs" / "fast_watcher.jsonl",
                                candidate_watcher=candidate_watcher,
                                event_orchestrator=event_orchestrator,
                            )
                            print("FAST QUOTE MODE: DIRECT_ROBINHOOD_MCP (performance not yet validated as REALTIME_FAST)")
                        if event_orchestrator is not None:
                            event_orchestrator.start()
                        try:
                            return SlowMarketLoop(
                                lambda: refresh_and_run(
                                    snapshot, portfolio, direct_client=client,
                                    context_store=context_store,
                                    event_orchestrator=event_orchestrator,
                                    reasoning_provider=reasoning_provider,
                                ), watcher=watcher,
                                has_positions=has_shadow_work,
                                status_path=PROJECT_DIR / "state" / "slow_loop_status.json",
                                events_path=PROJECT_DIR / "logs" / "slow_loop.jsonl",
                            ).run()
                        finally:
                            if event_orchestrator is not None:
                                event_orchestrator.stop()
                except RobinhoodAuthenticationRequired:
                    print("DIRECT MCP NOT AUTHENTICATED. Run: python runner.py --robinhood-auth")
                    return 3
                except DirectMcpUnavailable as exc:
                    print(f"DIRECT MCP UNAVAILABLE: {type(exc).__name__}")
                    return 3
            if portfolio is not None and config.FAST_WATCHER_ENABLED:
                candidate_watcher = (
                    FastCandidateWatcher(
                        context_store,
                        ShadowExecutionEngine(portfolio),
                        events_path=PROJECT_DIR / "logs" / "fast_candidate_watcher.jsonl",
                        score_history_path=PROJECT_DIR / config.CANDIDATE_SCORE_HISTORY_PATH,
                        event_orchestrator=event_orchestrator,
                    )
                    if context_store is not None else None
                )
                watcher = FastPositionWatcher(
                    portfolio, SnapshotQuoteProvider(snapshot), ShadowExecutor(ShadowExecutionEngine(portfolio)),
                    status_path=PROJECT_DIR / "state" / "fast_watcher_status.json",
                    events_path=PROJECT_DIR / "logs" / "fast_watcher.jsonl",
                    candidate_watcher=candidate_watcher,
                    event_orchestrator=event_orchestrator,
                )
                print("FAST QUOTE MODE: DEGRADED_SNAPSHOT (legacy provider)")
            if event_orchestrator is not None:
                event_orchestrator.start()
            try:
                return SlowMarketLoop(
                    lambda: refresh_and_run(
                        snapshot, portfolio, context_store=context_store,
                        event_orchestrator=event_orchestrator,
                        reasoning_provider=reasoning_provider,
                    ), watcher=watcher,
                    has_positions=has_shadow_work,
                    status_path=PROJECT_DIR / "state" / "slow_loop_status.json",
                    events_path=PROJECT_DIR / "logs" / "slow_loop.jsonl",
                ).run()
            finally:
                if event_orchestrator is not None:
                    event_orchestrator.stop()
    except CycleAlreadyRunningError:
        print("OVERLAPPING_RUNNER: another process owns shadow state.")
        return 3
    except KeyboardInterrupt:
        print("\nAnalysis loop stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
