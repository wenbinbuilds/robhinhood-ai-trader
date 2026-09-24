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
from robinhood_mcp.pre_execution import DirectPreExecutionMarketDataProvider
from robinhood_mcp.client import ALLOWED_TOOLS
from agent.llm_reasoning_bridge import LlmReasoningBridge
from shadow.performance import ShadowPerformance
from shadow.session_audit import ShadowSessionAudit
from shadow.portfolio import ShadowPortfolio
from shadow.execution import ShadowExecutionEngine
from execution.shadow_executor import ShadowExecutor
from execution.execution_guard import read_kill_switch
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
from trading_runtime.observability import RuntimeDashboard, render_scalp_drilldown

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = PROJECT_DIR / "state" / "market_snapshot.json"
SHADOW_STATE = PROJECT_DIR / "state" / "shadow_portfolio.json"
SHADOW_TRADES = PROJECT_DIR / "state" / "shadow_trades.jsonl"


def load_rl_shadow_runtime():
    if config.RL_MODE not in {'OFFLINE', 'SHADOW_COMPARE', 'SHADOW_CONTROL'}:
        raise RuntimeError('unsupported RL_MODE; LIVE is never valid')
    if not config.RL_ENABLED or config.RL_MODE not in {'SHADOW_COMPARE', 'SHADOW_CONTROL'}:
        return None
    if not config.RL_MODEL_ID:
        raise RuntimeError('RL_MODEL_ID must be explicitly selected for shadow inference')
    from rl.training import load_model
    from rl.shadow_compare import RuntimeShadowCompare, ShadowComparator
    policy, normalizer, metadata = load_model(
        PROJECT_DIR / config.RL_MODEL_REGISTRY_PATH, config.RL_MODEL_ID)
    if metadata.registry_state not in {'SHADOW_COMPARE', 'SHADOW_APPROVED'}:
        raise RuntimeError('model registry state does not permit shadow inference')
    if config.RL_MODE == 'SHADOW_CONTROL' and metadata.registry_state != 'SHADOW_APPROVED':
        raise RuntimeError('SHADOW_CONTROL requires explicit SHADOW_APPROVED promotion')
    return RuntimeShadowCompare(policy, normalizer,
        ShadowComparator(PROJECT_DIR / config.RL_SHADOW_COMPARE_PATH))


def load_scalp_runtime(portfolio, context_store, snapshot_path, *, enabled=None,
                       debug=False, direct_client=None):
    scalp_enabled = config.SCALP_ENABLED if enabled is None else bool(enabled)
    if not scalp_enabled:
        return None
    if config.SCALP_MODE != 'SHADOW' or config.MODE != 'SHADOW_TRADING':
        raise RuntimeError('scalp runtime is SHADOW_TRADING only')
    from strategies.scalp.runtime import ScalpRuntime
    from strategies.scalp.market_data import ScalpMarketDataCache
    cache = ScalpMarketDataCache(snapshot_path, context_store)
    history_refresher = None
    if direct_client is not None:
        from strategies.scalp.history import ScalpHistoryRefresher
        history_refresher = ScalpHistoryRefresher(direct_client, cache.get)
    return ScalpRuntime(
        portfolio, cache.get, universe=cache.symbols,
        setup_path=PROJECT_DIR/config.SCALP_STATE_PATH,
        events_path=PROJECT_DIR/config.SCALP_EVENT_LOG_PATH,
        enabled=scalp_enabled, discovery_source=cache.source,
        kill_switch_path=PROJECT_DIR/config.LIVE_KILL_SWITCH_PATH,
        diagnostics_path=PROJECT_DIR/config.SCALP_DIAGNOSTICS_PATH,
        debug=debug, history_refresher=history_refresher)


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
    mode.add_argument("--rl-build-dataset", action="store_true", help="build an offline chronological RL dataset")
    mode.add_argument("--rl-train", action="store_true", help="train one offline PPO model")
    mode.add_argument("--rl-evaluate", action="store_true", help="evaluate a selected PPO model on unseen data")
    mode.add_argument("--rl-shadow-compare", action="store_true", help="write hypothetical PPO/baseline comparisons only")
    mode.add_argument("--rl-status", action="store_true", help="show local RL configuration and model registry")
    mode.add_argument("--scalp-status", action="store_true", help="show local scalp configuration and state")
    mode.add_argument("--scalp-summary", action="store_true", help="show strategy-attributed local scalp performance")
    mode.add_argument("--scalp-backtest", action="store_true", help="run chronological local scalp simulation")
    mode.add_argument(
        "--strategy-status", action="store_true",
        help="show local POSITION, SCALP, and combined shadow status",
    )
    mode.add_argument(
        "--scalp-drilldown", metavar="SYMBOL",
        help="show the latest persisted full scalp trace for one symbol",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"normalized read-only data file (default: {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument("--rl-input", type=Path, help="historical JSON or JSONL input for dataset building")
    parser.add_argument("--rl-dataset", type=Path, default=PROJECT_DIR / config.RL_DATASET_PATH)
    parser.add_argument("--rl-model-id", default=None, help="explicit immutable model ID")
    parser.add_argument("--rl-timesteps", type=int, default=10_000)
    parser.add_argument("--scalp-input", type=Path, help="historical JSON/JSONL rows for scalp backtest")
    parser.add_argument(
        "--scalp-shadow", action="store_true",
        help="enable SCALP_V1 for this --loop process in local shadow mode only",
    )
    parser.add_argument(
        "--scalp-debug", action="store_true",
        help="print per-candidate scalp traces; diagnostics are always persisted",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="enable verbose runtime diagnostics for all strategies",
    )
    return parser.parse_args(argv)


def _scalp_shadow_safety() -> tuple[bool, dict[str, object]]:
    kill = read_kill_switch(PROJECT_DIR / config.LIVE_KILL_SWITCH_PATH)
    status = {
        'mode': config.MODE,
        'scalp_mode': config.SCALP_MODE,
        'live_trading_enabled': config.LIVE_TRADING_ENABLED,
        'robinhood_execution_enabled': config.ROBINHOOD_EXECUTION_ENABLED,
        'kill_switch': kill.status,
        'trading_blocked': kill.trading_blocked,
    }
    safe = (
        config.MODE == 'SHADOW_TRADING'
        and config.SCALP_MODE == 'SHADOW'
        and config.LIVE_TRADING_ENABLED is False
        and config.ROBINHOOD_EXECUTION_ENABLED is False
        and kill.trading_blocked is True
    )
    return safe, status


def _scalp_command(args) -> int:
    from strategies.scalp.analytics import (
        scalp_summary, friction_sensitivity, strategy_attribution,
        scalp_session_summary,
    )
    if args.scalp_status:
        kill = read_kill_switch(PROJECT_DIR / config.LIVE_KILL_SWITCH_PATH)
        open_scalp = None
        try:
            portfolio = ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES)
            open_scalp = sum(
                position.strategy in {'SCALP', config.SCALP_STRATEGY_ID}
                for position in portfolio.snapshot().open_positions
            )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        payload = {'enabled': config.SCALP_ENABLED, 'mode': config.SCALP_MODE,
                   'strategy_id': config.SCALP_STRATEGY_ID,
                   'candidate_discovery_source': config.SCALP_DISCOVERY_SOURCE,
                   'candidate_seed_symbols': list(config.SCALP_DISCOVERY_SYMBOLS),
                   'max_quote_age_seconds': config.SCALP_MAX_QUOTE_AGE_SECONDS,
                   'max_spread_pct': config.SCALP_MAX_SPREAD_PCT,
                   'min_net_edge_pct': config.SCALP_MIN_EXPECTED_NET_EDGE,
                   'max_hold_seconds': config.SCALP_MAX_HOLD_SECONDS,
                   'max_trades_per_symbol': config.SCALP_MAX_TRADES_PER_SYMBOL,
                   'max_daily_loss_percent': config.SCALP_MAX_DAILY_LOSS_PERCENT,
                   'diagnostics_path': config.SCALP_DIAGNOSTICS_PATH,
                   'open_scalp_positions': open_scalp,
                   'runtime_override': '--loop --scalp-shadow',
                   'debug_runtime_override': '--loop --scalp-shadow --scalp-debug',
                   'safety': {'mode': config.MODE,
                              'live_trading_enabled': config.LIVE_TRADING_ENABLED,
                              'robinhood_execution_enabled': config.ROBINHOOD_EXECUTION_ENABLED,
                              'kill_switch': kill.status,
                              'trading_blocked': kill.trading_blocked},
                   'live_supported': False}
        print(json.dumps(payload, indent=2, sort_keys=True)); return 0
    try: portfolio = ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'SCALP STATE ERROR: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
    if args.scalp_summary:
        from watcher.quote_diagnostics import quote_provenance_summary
        state = portfolio.snapshot()
        print(json.dumps({'performance': scalp_summary(state.closed_positions),
                          'session': scalp_session_summary(
                              PROJECT_DIR / config.SCALP_DIAGNOSTICS_PATH,
                              state.closed_positions,
                          ),
                          'quote_freshness': quote_provenance_summary(
                              PROJECT_DIR / config.QUOTE_PROVENANCE_LOG_PATH,
                              strategy='SCALP',
                          ),
                          'attribution': strategy_attribution(portfolio)}, indent=2, sort_keys=True))
        return 0
    if args.scalp_backtest:
        if args.scalp_input is None:
            print('--scalp-input is required for backtest', file=sys.stderr); return 2
        try:
            if args.scalp_input.suffix == '.jsonl':
                with args.scalp_input.open() as handle:
                    rows = [json.loads(line) for line in handle if line.strip()]
            else:
                value = json.loads(args.scalp_input.read_text())
                rows = value if isinstance(value, list) else value.get('rows', [])
            from strategies.scalp.simulation import ScalpBacktester
            trades = ScalpBacktester().run(rows)
            print(json.dumps({'strategy_id': 'SCALP', 'trades': len(trades),
                              'performance': scalp_summary(trades),
                              'friction_sensitivity': friction_sensitivity(trades),
                              'claim': 'SIMULATED_NOT_EVIDENCE_OF_PROFITABILITY'},
                             indent=2, sort_keys=True, allow_nan=False))
            return 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'SCALP BACKTEST FAILED: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
    return 2


def _strategy_status() -> int:
    """Local-only two-strategy status; never constructs a Robinhood client."""

    from strategies.scalp.analytics import (
        scalp_session_summary, scalp_summary, strategy_attribution,
    )
    from watcher.quote_diagnostics import quote_provenance_summary
    try:
        portfolio = ShadowPortfolio(SHADOW_STATE, SHADOW_TRADES)
        state = portfolio.snapshot()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'STRATEGY STATE ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    try:
        candidates = CandidateContextStore(
            PROJECT_DIR / config.CANDIDATE_WATCHLIST_PATH
        ).snapshot()
    except (OSError, ValueError, json.JSONDecodeError):
        candidates = []
    attribution = strategy_attribution(portfolio)
    scalp_session = scalp_session_summary(
        PROJECT_DIR / config.SCALP_DIAGNOSTICS_PATH,
        state.closed_positions,
    )
    scalp_performance = scalp_summary(state.closed_positions)
    position = attribution['POSITION']
    scalp = attribution['SCALP']
    position_closed = [
        trade for trade in state.closed_positions
        if getattr(trade, 'strategy_display_name', '') == 'POSITION'
    ]
    payload = {
        'strategies': {
            'POSITION': {
                'status': 'ENABLED', 'internal_id': 'MOMENTUM',
                'style': 'LONGER_HORIZON', 'llm': 'ENABLED',
                'watch_threshold': config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE,
                'trade_threshold': config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
                'confirmation_count': config.FAST_ENTRY_CONFIRMATION_UPDATES,
                'minimum_rr': config.MIN_RISK_REWARD_RATIO,
                'candidates': len(candidates),
                'watchlist': sum(item.status == 'WATCH' for item in candidates),
                'confirmed': sum(
                    item.consecutive_qualifying_updates
                    >= config.FAST_ENTRY_CONFIRMATION_UPDATES
                    for item in candidates
                ),
                'entry_attempts': position['open_positions'] + position['trade_count'],
                'open_positions': position['open_positions'],
                'exits': len(position_closed),
                'realized_pnl': position['realized_pnl'],
                'unrealized_pnl': position['unrealized_pnl'],
            },
            'SCALP': {
                'status': ('ENABLED_SHADOW' if config.SCALP_ENABLED else 'DISABLED_DEFAULT'),
                'internal_id': 'SCALP', 'style': 'FAST_SHORT_HORIZON',
                'llm': 'DISABLED', 'max_hold_seconds': config.SCALP_MAX_HOLD_SECONDS,
                'signal_threshold': config.SCALP_MIN_SIGNAL_SCORE,
                'observations': scalp_session['candidate_observations'],
                'valid_fast_setups': scalp_session['funnel']['eligible_micro_signals'],
                'entries': scalp_session['shadow_entries'],
                'open_scalps': scalp['open_positions'],
                'normal_exits': scalp_session['exits'] - scalp_session['recovery_time_exits'],
                'time_exits': scalp_session['normal_time_exits'],
                'recovery_exits': scalp_session['recovery_time_exits'],
                'wins': scalp_performance['wins'],
                'losses': scalp_performance['losses'],
                'realized_pnl': scalp['realized_pnl'],
                'unrealized_pnl': scalp['unrealized_pnl'],
                'net_pnl': scalp_performance['net_pnl'],
            },
        },
        'portfolio': {
            'POSITION': position, 'SCALP': scalp,
            'TOTAL': {
                'equity': state.equity, 'cash': state.cash,
                'realized_pnl': state.realized_pnl,
                'unrealized_pnl': state.unrealized_pnl,
                'total_pnl': state.realized_pnl + state.unrealized_pnl,
            },
        },
        'quote_freshness': {
            strategy: quote_provenance_summary(
                PROJECT_DIR / config.QUOTE_PROVENANCE_LOG_PATH,
                strategy=strategy,
            )
            for strategy in ('POSITION', 'SCALP')
        },
        'safety': {
            'mode': config.MODE,
            'live_trading_enabled': config.LIVE_TRADING_ENABLED,
            'robinhood_execution_enabled': config.ROBINHOOD_EXECUTION_ENABLED,
            'live_supported': False,
        },
    }
    p = payload['strategies']['POSITION']
    s = payload['strategies']['SCALP']
    total = payload['portfolio']['TOTAL']
    print("TRADER — SHADOW MODE")
    print("Safety: SHADOW ONLY — LIVE EXECUTION BLOCKED")
    print(
        f"Portfolio: equity=${total['equity']:.2f} cash=${total['cash']:.2f} "
        f"realized=${total['realized_pnl']:+.2f} unrealized=${total['unrealized_pnl']:+.2f}"
    )
    print("POSITION")
    print(
        f"  status={p['status']} candidates={p['candidates']} watching={p['watchlist']} "
        f"confirmed={p['confirmed']} open={p['open_positions']} exits={p['exits']} "
        f"realized=${p['realized_pnl']:+.2f} unrealized=${p['unrealized_pnl']:+.2f}"
    )
    print(
        f"  thresholds: watch={p['watch_threshold']:.2f} trade={p['trade_threshold']:.2f} "
        f"confirmations={p['confirmation_count']} minimum_rr={p['minimum_rr']}"
    )
    print("SCALP")
    print(
        f"  status={s['status']} observations={s['observations']} eligible={s['valid_fast_setups']} "
        f"entries={s['entries']} open={s['open_scalps']} exits={s['normal_exits'] + s['recovery_exits']} "
        f"realized=${s['realized_pnl']:+.2f} unrealized=${s['unrealized_pnl']:+.2f}"
    )
    print(
        f"  signal_threshold={s['signal_threshold']:.2f} max_hold={s['max_hold_seconds']}s "
        f"wins={s['wins']} losses={s['losses']}"
    )
    return 0


def _scalp_drilldown(symbol: str) -> int:
    """Read-only inspection of the latest durable candidate observation."""

    wanted = symbol.upper()
    latest = None
    path = PROJECT_DIR / config.SCALP_DIAGNOSTICS_PATH
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("record_type") == "CANDIDATE_OBSERVATION"
                        and row.get("symbol") == wanted
                        and isinstance(row.get("payload"), Mapping)):
                    latest = row["payload"]
    except OSError as exc:
        print(f"SCALP DEBUG ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if latest is None:
        print(f"SCALP DEBUG: no persisted observation for {wanted}", file=sys.stderr)
        return 3
    print(render_scalp_drilldown(latest))
    return 0


def _rl_command(args) -> int:
    """Local offline commands have no Robinhood client or execution adapter."""
    from rl.dataset import HistoricalDatasetBuilder, chronological_split
    from rl.registry import ModelRegistry
    registry = ModelRegistry(PROJECT_DIR / config.RL_MODEL_REGISTRY_PATH)
    if args.rl_status:
        print(json.dumps({'enabled': config.RL_ENABLED, 'mode': config.RL_MODE,
                          'selected_model': config.RL_MODEL_ID, 'models': registry.status(),
                          'live_supported': False}, indent=2, sort_keys=True))
        return 0
    if args.rl_build_dataset:
        if args.rl_input is None:
            print('--rl-input is required for dataset building', file=sys.stderr); return 2
        try:
            if args.rl_input.suffix == '.jsonl':
                with args.rl_input.open() as handle:
                    rows = [json.loads(line) for line in handle if line.strip()]
            else:
                value = json.loads(args.rl_input.read_text())
                rows = value if isinstance(value, list) else value.get('rows', [])
            result = HistoricalDatasetBuilder().write(HistoricalDatasetBuilder().build(rows), args.rl_dataset)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'RL DATASET BUILD FAILED: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
        print(json.dumps(result, indent=2, sort_keys=True)); return 0
    if args.rl_train:
        from rl.training import train_ppo
        try: result = train_ppo(args.rl_dataset, registry.root, timesteps=args.rl_timesteps)
        except Exception as exc:
            print(f'RL TRAINING FAILED: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
        print(json.dumps(result, indent=2, sort_keys=True)); return 0
    model_id = args.rl_model_id or config.RL_MODEL_ID
    if not model_id:
        print('--rl-model-id is required; models are never selected automatically', file=sys.stderr); return 2
    from rl.training import evaluate_model, load_model
    if args.rl_evaluate:
        try: result = evaluate_model(args.rl_dataset, registry.root, model_id, split='test')
        except Exception as exc:
            print(f'RL EVALUATION FAILED: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)); return 0
    if args.rl_shadow_compare:
        from rl.evaluation import evaluate_policy, comparison
        from rl.policy import BaselinePolicy
        from rl.shadow_compare import ShadowComparator
        try:
            steps = HistoricalDatasetBuilder.read(args.rl_dataset)
            _, _, test = chronological_split(steps)
            policy, normalizer, _ = load_model(registry.root, model_id)
            _, baseline = evaluate_policy(test, BaselinePolicy())
            _, learned = evaluate_policy(test, policy, normalizer=normalizer)
            compared = comparison(baseline, learned)
            writer = ShadowComparator(PROJECT_DIR / config.RL_SHADOW_COMPARE_PATH)
            for row in compared['rows']:
                writer.record(timestamp=row['timestamp'], symbol=row['symbol'], episode_id=row['episode_id'],
                              baseline_action=row['baseline_action'], rl_action=row['rl_action'],
                              outcome_status=row['outcome_status'], outcome={'r': row['r']})
            print(json.dumps({'mode': 'SHADOW_COMPARE', 'portfolio_mutations': 0,
                              'decisions': compared['decisions'], 'disagreements': compared['disagreements']}, indent=2))
            return 0
        except Exception as exc:
            print(f'RL SHADOW COMPARE FAILED: {type(exc).__name__}: {exc}', file=sys.stderr); return 2
    return 2


def load_provider(path: Path) -> JsonSnapshotProvider:
    return JsonSnapshotProvider.from_path(path)


def print_cycle_diagnostics(result: Mapping[str, object]) -> None:
    """Best-effort observability that is isolated from cycle execution."""

    try:
        for line in cycle_diagnostic_lines(result):
            print(line)
    except Exception as exc:
        print(
            f"DIAGNOSTICS STATUS: DEGRADED ({type(exc).__name__}; cycle continued)"
        )


def run_analysis(
    snapshot: Path, shadow_portfolio: ShadowPortfolio | None = None,
    *, reasoning_provider=None, pre_execution_refresher=None,
) -> dict[str, object]:
    provider = load_provider(snapshot)
    return MarketCycle(
        provider,
        state_path=PROJECT_DIR / "state" / "session.json",
        logs_dir=PROJECT_DIR / "logs",
        shadow_portfolio=shadow_portfolio,
        pre_execution_refresher=pre_execution_refresher,
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
    scalp_enabled: bool = False,
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
    if event_orchestrator is not None and shadow_portfolio is not None:
        event_orchestrator.attach_shadow_portfolio(shadow_portfolio)
        event_orchestrator.reconcile_position_state(now=cycle_started_at)
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
                bridge.refresh(
                    shadow_symbols=shadow_symbols,
                    scalp_symbols=(config.SCALP_DISCOVERY_SYMBOLS if scalp_enabled else ()),
                )
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
                    pre_execution_refresher=(
                        DirectPreExecutionMarketDataProvider(
                            direct_client, project_dir=PROJECT_DIR
                        )
                        if direct_client is not None else None
                    ),
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
    reasoning_root = result.get("llm_reasoning")
    reasoning_trace_for_timing = (
        reasoning_root.get("trace") if isinstance(reasoning_root, Mapping) else {}
    )
    if not isinstance(reasoning_trace_for_timing, Mapping):
        reasoning_trace_for_timing = {}
    reasoning = reasoning_trace_for_timing.get("reasoning_duration_seconds", 0)
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
        context_store.replace(
            contexts, now=analysis_started_at, exclude_symbols=open_symbols,
            # Technical context gets a fresh 300-second watch TTL even when
            # qualitative research is reused. Its original generation time is
            # persisted separately on CandidateContext.
            preserve_research=False,
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
        analyzed = [
            row for row in result.get("analyzed_candidates", [])
            if isinstance(row, Mapping)
        ]
        combined_scores = [
            float(score) for row in analyzed
            if isinstance(row.get("coordinator_decision"), Mapping)
            and (score := row["coordinator_decision"].get("combined_score"))
                is not None
        ]
        trade_ready = sum(
            isinstance(row.get("coordinator_decision"), Mapping)
            and row["coordinator_decision"].get("decision") == "TRADE_CANDIDATE"
            for row in analyzed
        )
        primary = (
            "SCORE_BELOW_WATCH_THRESHOLD"
            if analyzed and not contexts and combined_scores
            and max(combined_scores) < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE
            else "HARD_OR_CONTEXT_GATE" if analyzed and not contexts
            else "NONE"
        )
        print("POSITION STATUS")
        print(
            f"candidates={len(analyzed)} watch_admitted={len(contexts)} "
            f"trade_ready={trade_ready} "
            f"top_combined_score={max(combined_scores) if combined_scores else None} "
            f"primary_bottleneck={primary}"
        )
    atomic_json(PROJECT_DIR / "state" / "slow_loop_status.json", timing)
    log_path = PROJECT_DIR / "logs" / f"{cycle_started_at.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "SLOW_CYCLE_TIMING", "mode": config.MODE, **timing}) + "\n")
    print(f"ANALYSIS STATUS: {result['decision']['type']}")
    print_cycle_diagnostics(result)
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
    audit = ShadowSessionAudit(PROJECT_DIR).summarize()
    counts = audit["funnel"]["counts"]
    geometry = audit['entry_geometry']
    print('ENTRY GEOMETRY: ' + json.dumps({
        key: geometry[key] for key in (
            'attempts_with_geometry_evidence', 'RR_AT_RESEARCH', 'RR_AT_ENTRY',
            'RR_DEGRADATION', 'rr_failures_crossing_boundary_due_to_entry_drift',
            'refreshed_geometry_restored_valid_rr', 'correctly_rejected_extended',
            'historical_entry_drift_boundary_crossings',
        )
    }, sort_keys=True))
    print(f"SHADOW SESSION {audit['session_date']}")
    session_labels = (
        ("Candidates scanned", "scanner_candidates"),
        ("Unique candidates", "unique_symbols_discovered"),
        ("Slow analyses", "slow_analyses_completed"),
        ("True-hard rejected", "true_hard_rejected"),
        ("Below slow 0.60", "slow_score_below_0.60"),
        ("Fast watch admitted", "fast_watch_admitted"),
        ("Reached dynamic 0.70", "dynamic_reached_0.70"),
        ("Reached dynamic 0.72", "dynamic_reached_0.72"),
        ("Confirmed crossings", "confirmed_threshold_crossings"),
        ("Pre-execution attempts", "pre_execution_attempts"),
        ("Pre-execution passed", "pre_execution_passed"),
        ("Risk approvals", "risk_approvals"),
        ("Entries", "shadow_entries"),
        ("Exits", "shadow_exits"),
    )
    for label, key in session_labels:
        print(f"{label}: {counts.get(key, 0)}")
    print("Primary lost-opportunity reasons:")
    rejected = audit.get("rejections", {})
    reason_counts: dict[str, int] = {}
    for category in (
        "TERMINAL_HARD_REJECTION", "SCORE_BELOW_THRESHOLD",
        "TEMPORARY_BLOCK", "INFRASTRUCTURE_FAILURE",
    ):
        for reason, count in rejected.get(category, {}).get("reasons", {}).items():
            reason_counts[reason] = reason_counts.get(reason, 0) + int(count)
    for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:5]:
        print(f"  {reason}: {count}")
    quote_rate = audit.get("quote_reliability", {}).get("success_rate_percent")
    cache_rate = audit.get("llm_cache", {}).get("hit_rate_percent")
    print(f"Quote success rate: {'UNAVAILABLE' if quote_rate is None else str(quote_rate) + '%'}")
    print(f"LLM cache hit rate: {'UNAVAILABLE' if cache_rate is None else str(cache_rate) + '%'}")
    frequency = audit.get("frequency", {})
    print(
        "Observed frequency: "
        f"candidates/hour={frequency.get('scanner_candidates_per_hour')} "
        f"fast-watch/hour={frequency.get('fast_watch_admissions_per_hour')} "
        f"trade-candidates/hour={frequency.get('trade_candidates_per_hour')} "
        f"entries/hour={frequency.get('shadow_entries_per_hour')}"
    )
    print("OFFLINE COUNTERFACTUALS (runtime settings unchanged)")
    for item in audit.get("counterfactuals", []):
        print(
            f"  watch={item['watch_threshold']:.2f} trade={item['trade_threshold']:.2f} "
            f"additional_watch={item['additional_watch_opportunities']} "
            f"additional_trade_candidates={item['additional_trade_candidates_observed']} "
            "hypothetical_pnl=UNAVAILABLE"
        )
    if audit.get("warnings"):
        print("Audit warnings:")
        for warning in audit["warnings"]:
            print(f"  {warning}")
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
    if any((args.rl_build_dataset, args.rl_train, args.rl_evaluate,
            args.rl_shadow_compare, args.rl_status)):
        return _rl_command(args)
    if any((args.scalp_status, args.scalp_summary, args.scalp_backtest)):
        return _scalp_command(args)
    if args.scalp_drilldown:
        return _scalp_drilldown(args.scalp_drilldown)
    if args.strategy_status:
        return _strategy_status()
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
    if args.scalp_shadow and not args.loop:
        print("--scalp-shadow is supported only with --loop", file=sys.stderr)
        return 2
    scalp_enabled = bool(config.SCALP_ENABLED or args.scalp_shadow)
    debug_runtime = bool(args.debug or args.scalp_debug)
    if debug_runtime and (not args.loop or not scalp_enabled):
        print("--scalp-debug requires an enabled shadow scalp --loop", file=sys.stderr)
        return 2
    if scalp_enabled:
        scalp_safe, scalp_safety = _scalp_shadow_safety()
        if not scalp_safe:
            print(
                "SCALP SHADOW START REFUSED: "
                + json.dumps(scalp_safety, sort_keys=True),
                file=sys.stderr,
            )
            return 2
    if args.loop and debug_runtime:
        print("STRATEGIES:")
        print("POSITION:")
        print("status=ENABLED")
        print("style=LONGER_HORIZON")
        print("LLM=ENABLED")
        print(f"watch_threshold={config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:.2f}")
        print(f"trade_threshold={config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:.2f}")
        print("SCALP:")
        print("status=" + ("ENABLED_SHADOW" if scalp_enabled else "DISABLED"))
        print("style=FAST_SHORT_HORIZON")
        print("LLM=DISABLED")
        print(f"max_hold={config.SCALP_MAX_HOLD_SECONDS}s")
        print(f"signal_threshold={config.SCALP_MIN_SIGNAL_SCORE:.2f}")
    elif args.loop:
        print("TRADER — SHADOW MODE | SHADOW ONLY | LIVE EXECUTION BLOCKED", flush=True)
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
                event_orchestrator.attach_shadow_portfolio(portfolio)
                event_orchestrator.reconcile_position_state(
                    now=datetime.now(timezone.utc)
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
                                    pre_execution_refresher=DirectPreExecutionMarketDataProvider(
                                        client, project_dir=PROJECT_DIR
                                    ),
                                    rl_shadow_runtime=load_rl_shadow_runtime(),
                                    debug=debug_runtime,
                                )
                                if context_store is not None else None
                            )
                            watcher = FastPositionWatcher(
                                portfolio, quote_provider, ShadowExecutor(ShadowExecutionEngine(portfolio)),
                                status_path=PROJECT_DIR / "state" / "fast_watcher_status.json",
                                events_path=PROJECT_DIR / "logs" / "fast_watcher.jsonl",
                                candidate_watcher=candidate_watcher,
                                event_orchestrator=event_orchestrator,
                                scalp_runtime=load_scalp_runtime(
                                    portfolio, context_store, snapshot,
                                    enabled=scalp_enabled, debug=debug_runtime,
                                    direct_client=client,
                                ),
                                debug=debug_runtime,
                                dashboard=(None if debug_runtime else RuntimeDashboard(
                                    portfolio, context_store,
                                    PROJECT_DIR / config.LIVE_KILL_SWITCH_PATH,
                                )),
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
                                    scalp_enabled=scalp_enabled,
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
                        rl_shadow_runtime=load_rl_shadow_runtime(),
                        debug=debug_runtime,
                    )
                    if context_store is not None else None
                )
                watcher = FastPositionWatcher(
                    portfolio, SnapshotQuoteProvider(snapshot), ShadowExecutor(ShadowExecutionEngine(portfolio)),
                    status_path=PROJECT_DIR / "state" / "fast_watcher_status.json",
                    events_path=PROJECT_DIR / "logs" / "fast_watcher.jsonl",
                    candidate_watcher=candidate_watcher,
                    event_orchestrator=event_orchestrator,
                    scalp_runtime=load_scalp_runtime(
                        portfolio, context_store, snapshot,
                        enabled=scalp_enabled, debug=debug_runtime,
                    ),
                    debug=debug_runtime,
                    dashboard=(None if debug_runtime else RuntimeDashboard(
                        portfolio, context_store,
                        PROJECT_DIR / config.LIVE_KILL_SWITCH_PATH,
                    )),
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
                        scalp_enabled=scalp_enabled,
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
