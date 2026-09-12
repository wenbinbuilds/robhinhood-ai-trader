"""Read-only local status: never initializes portfolios or refreshes prices."""
import json
from datetime import datetime, timezone
from pathlib import Path

import config
from watcher.models import timestamp


def read_object(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"error": "INVALID_STATE"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"error": "INVALID_STATE"}


def shadow_dashboard_projection(portfolio, fast, slow, watchlist=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    def age(value):
        at = timestamp(value)
        return (now - at).total_seconds() if at else None
    heartbeat_age = age(fast.get("heartbeat"))
    status = fast.get("status", "NOT_RUNNING")
    quote_age = age(fast.get("last_quote_timestamp"))
    if status not in {"STOPPED", "NOT_RUNNING"} and (heartbeat_age is None or heartbeat_age < 0 or heartbeat_age > max(10, config.FAST_QUOTE_INTERVAL_SECONDS * 3)):
        status = "NOT_RUNNING_OR_UNRESPONSIVE"
    elif status == "ACTIVE" and (quote_age is None or not 0 <= quote_age <= config.FAST_QUOTE_MAX_AGE_SECONDS):
        status = "DEGRADED"
    positions = []
    for row in portfolio.get("open_positions", []):
        current = row.get("last_price")
        at_age = age(row.get("last_price_timestamp"))
        positions.append({**row, "quote_age": at_age,
                          "monitoring_status": row.get("monitoring_status") if status == "ACTIVE" and at_age is not None and 0 <= at_age <= config.FAST_QUOTE_MAX_AGE_SECONDS else "PRICE_MONITORING_DEGRADED",
                          "distance_to_stop": current - row["stop"] if isinstance(current, (int, float)) else None,
                          "distance_to_target": row["target"] - current if isinstance(current, (int, float)) else None})
    candidates = []
    raw_candidates = (watchlist or {}).get("candidates", []) if isinstance(watchlist, dict) else []
    if isinstance(raw_candidates, list):
        for row in raw_candidates:
            if not isinstance(row, dict):
                continue
            is_canonical_state = "state" in row and "slow_alpha_score" in row
            latest_quote = row.get("latest_quote", {}) if is_canonical_state else {}
            if not isinstance(latest_quote, dict):
                latest_quote = {}
            candidates.append({
                "symbol": row.get("symbol"),
                "research_timestamp": row.get("research_timestamp"),
                "context_age": age(row.get("research_timestamp")),
                "slow_score": row.get("slow_alpha_score") if is_canonical_state else row.get("slow_context_score"),
                "live_score": row.get("live_market_score"),
                "slow_weight": row.get("slow_weight"),
                "live_weight": row.get("live_weight"),
                "dynamic_score": row.get("combined_alpha_score") if is_canonical_state else row.get("dynamic_score"),
                "trade_threshold": config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
                "current_price": row.get("current_price", latest_quote.get("mark_price")),
                "research_price": row.get("research_price") if is_canonical_state else row.get("analysis_price"),
                "status": row.get("status", "WATCH"),
                "state": row.get("state") if is_canonical_state else row.get("candidate_state", "SETUP_FORMING"),
                "quote_age": age(row.get("latest_quote_timestamp") if is_canonical_state else row.get("last_updated_at")),
                "last_event": row.get("last_event") if is_canonical_state else (
                    row.get("state_transition_history", [])[-1].get("reason")
                    if isinstance(row.get("state_transition_history"), list)
                    and row.get("state_transition_history")
                    and isinstance(row.get("state_transition_history")[-1], dict)
                    else None
                ),
                "technical_score": row.get("technical_score"),
                "technical_confidence": row.get("technical_confidence"),
                "qualitative_score": row.get("qualitative_score"),
                "news_score": row.get("news_score"),
                "sector_score": row.get("sector_score"),
                "market_score": row.get("market_score"),
                "context_expiration": row.get("context_expiration"),
                "eligible_for_fast_watch": row.get("eligible_for_fast_watch"),
                "admission_reason": row.get("admission_reason"),
                "true_hard_gate_failures": row.get("true_hard_gate_failures", []),
                "signal_quality_failures": row.get("signal_quality_failures", []),
            })
    universe = candidates
    watchlist_rows = [row for row in candidates if row["state"] in {"DISCOVERED", "WATCHLIST"}]
    setup_rows = [row for row in candidates if row["state"] in {"SETUP_FORMING", "INFRASTRUCTURE_BLOCKED"}]
    ready_rows = [row for row in candidates if row["state"] in {"TRADE_READY", "RISK_APPROVED"}]
    return dict(mode=config.MODE, slow=slow, fast={**fast, "status": status, "quote_age": quote_age,
                "enabled": config.FAST_WATCHER_ENABLED,
                "provider": fast.get("provider", "PERIODIC_ROBINHOOD_SNAPSHOT"),
                "mode": fast.get("mode", "DEGRADED_SNAPSHOT"),
                "poll_interval": config.FAST_QUOTE_INTERVAL_SECONDS}, positions=positions,
                universe=universe, watchlist=candidates,
                watchlist_state=watchlist_rows, setup_forming=setup_rows,
                trade_ready=ready_rows, open_positions=positions,
                recently_closed=list(portfolio.get("closed_positions", []))[-5:],
                warnings=[v["error"] for v in (portfolio, fast, slow) if "error" in v], controls=[])


def print_status(state_path: Path, fast_path: Path, slow_path: Path, watchlist_path: Path | None = None):
    watchlist_path = watchlist_path or state_path.parent / "candidate_states.json"
    view = shadow_dashboard_projection(read_object(state_path), read_object(fast_path), read_object(slow_path), read_object(watchlist_path))
    slow, fast = view["slow"], view["fast"]
    print(f"MODE: {view['mode']}")
    print(f"SLOW LOOP: {slow.get('status', 'NOT_RUNNING')}")
    for name in ("cycle_started_at", "snapshot_duration", "reasoning_duration", "local_analysis_duration", "total_cycle_duration", "next_cycle_target"):
        print(f"  {name}: {slow.get(name, 'UNAVAILABLE')}")
    print("FAST LOOP:")
    for name in ("enabled", "provider", "mode", "status", "poll_interval", "last_quote_timestamp", "quote_age", "open_symbols", "candidate_symbols", "unmonitored_symbols", "metrics", "provider_metrics"):
        print(f"  {name}: {fast.get(name, 'UNAVAILABLE')}")
    print("UNIVERSE:")
    print("  " + (", ".join(row["symbol"] for row in view["universe"]) or "NONE"))
    for heading, key in (("WATCHLIST", "watchlist_state"),
                         ("SETUP FORMING", "setup_forming"),
                         ("TRADE READY", "trade_ready")):
        print(f"{heading}:")
        for candidate in view[key]:
            print(
                f"  {candidate['symbol']} slow={candidate['slow_score']} "
                f"live={candidate['live_score']} combined={candidate['dynamic_score']} "
                f"context_age={candidate['context_age']} quote_age={candidate['quote_age']} "
                f"current={candidate['current_price']} research={candidate['research_price']} "
                f"technical_confidence={candidate['technical_confidence']} "
                f"last_event={candidate['last_event']} admission={candidate['admission_reason']} "
                f"true_hard_failures={candidate['true_hard_gate_failures']} "
                f"signal_warnings={candidate['signal_quality_failures']}"
            )
        if not view[key]:
            print("  NONE")
    print("OPEN POSITIONS:")
    for p in view["open_positions"]:
        print(f"  {p['symbol']} entry={p['entry_price']} current={p.get('last_price')} bid={p.get('current_bid')} ask={p.get('current_ask')} "
              f"stop={p['stop']} target={p['target']} P&L={p.get('unrealized_pnl')} "
              f"distance_to_stop={p['distance_to_stop']} distance_to_target={p['distance_to_target']} "
              f"last_price_timestamp={p.get('last_price_timestamp')} quote_age={p['quote_age']} mark_method={p.get('mark_price_method')} status={p['monitoring_status']}")
    if not view["open_positions"]:
        print("  NONE")
    print("RECENTLY CLOSED:")
    for trade in view["recently_closed"]:
        print(
            f"  {trade.get('symbol')} exit={trade.get('exit_reason')} "
            f"net={trade.get('net_pnl')} at={trade.get('exit_timestamp')}"
        )
    if not view["recently_closed"]:
        print("  NONE")
    if view["warnings"]:
        print("WARNINGS: " + ", ".join(view["warnings"]))
    return 3 if view["warnings"] else 0
