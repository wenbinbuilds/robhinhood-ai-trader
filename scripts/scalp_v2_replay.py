#!/usr/bin/env python3
"""Causal V1-control versus HYBRID_SCALP V2 replay and forward labeling."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import median
import sys
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from strategies.scalp_v2.research import HybridScalpResearchEngine


HORIZONS = (15, 30, 60, 120, 180)
SETUP_VARIANTS = {
    "MICRO_BREAKOUT": ("BREAKOUT_CROSS", "BREAKOUT_RETEST_RECLAIM"),
    "EMA9_CONTINUATION": ("EMA9_POSITIVE_5S", "EMA9_TURN_UP"),
    "MICRO_PULLBACK": ("PULLBACK_TURN", "PULLBACK_RECLAIM"),
    "VWAP_RECLAIM": ("VWAP_RECLAIM",),
    "MOMENTUM_BURST": ("MOMENTUM_ACCELERATION",),
}


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue


def percentile(values: Sequence[float], quantile: float) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(value))
    if not clean:
        return None
    index = (len(clean) - 1) * quantile
    low, high = int(index), min(int(index) + 1, len(clean) - 1)
    return clean[low] + (clean[high] - clean[low]) * (index - low)


def distribution(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None and math.isfinite(value)]
    return {
        "count": len(clean), "median": median(clean) if clean else None,
        "p75": percentile(clean, .75), "p90": percentile(clean, .90),
        "p95": percentile(clean, .95), "max": max(clean) if clean else None,
    }


def correlation(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - xbar) * (y - ybar) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - xbar) ** 2 for x in xs) * sum((y - ybar) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else None


def latest_session(logs: Path) -> tuple[str, str]:
    starts = [
        row["timestamp"] for row in rows(logs / "fast_watcher.jsonl")
        if row.get("event") == "FAST_WATCHER_STARTED"
    ]
    if not starts:
        raise ValueError("no fast watcher session found")
    start = starts[-1]
    observed = [
        row["timestamp"] for row in rows(logs / "scalp_diagnostics.jsonl")
        if row.get("record_type") == "CANDIDATE_OBSERVATION"
        and row.get("timestamp", "") >= start
    ]
    if not observed:
        raise ValueError("latest session has no SCALP observations")
    return start, max(observed)


def session_traces(logs: Path, start: str, end: str) -> list[dict[str, Any]]:
    traces = []
    for row in rows(logs / "scalp_diagnostics.jsonl"):
        if (row.get("record_type") == "CANDIDATE_OBSERVATION"
                and start <= row.get("timestamp", "") <= end):
            payload = dict(row.get("payload", {}))
            payload.setdefault("timestamp", row.get("timestamp"))
            payload.setdefault("symbol", row.get("symbol"))
            payload.setdefault("episode_id", row.get("episode_id"))
            traces.append(payload)
    return sorted(traces, key=lambda row: (row.get("timestamp", ""), row.get("symbol", "")))


def fetch_minute_bars(symbols: Sequence[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    """Read-only provider capability sample. No account or order tool is used."""
    from robinhood_mcp.client import DirectRobinhoodMcpClient

    result: dict[str, list[dict[str, Any]]] = {}
    with DirectRobinhoodMcpClient() as client:
        # A full-day minute response is large. Use one symbol per read-only
        # request to avoid oversized SSE responses while keeping calls bounded.
        for offset in range(0, len(symbols)):
            batch = [symbols[offset]]
            call = client.call_readonly("get_equity_historicals", {
                "symbols": batch, "start_time": start, "end_time": end,
                "interval": "minute", "bounds": "regular",
                "adjustment_type": "split",
            })
            value = call.value
            payload = value.get("data", value) if isinstance(value, dict) else {}
            provider_rows = payload.get("results", []) if isinstance(payload, dict) else []
            for item in provider_rows if isinstance(provider_rows, list) else []:
                symbol = str(item.get("symbol", "")).upper()
                bars = []
                for bar in item.get("bars", []) if isinstance(item, dict) else []:
                    try:
                        bars.append({
                            "begins_at": stamp(bar["begins_at"]).isoformat(),
                            "open": float(bar["open_price"]),
                            "high": float(bar["high_price"]),
                            "low": float(bar["low_price"]),
                            "close": float(bar["close_price"]),
                            "volume": float(bar["volume"]),
                            "interpolated": bool(bar.get("interpolated", False)),
                            "interval_seconds": 60,
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
                result[symbol] = bars
    return result


def minute_gate(observation: Mapping[str, Any], bars: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    symbol = observation["symbol"]
    at = stamp(observation["timestamp"])
    complete = [
        row for row in bars.get(symbol, [])
        if not row.get("interpolated")
        and stamp(row["begins_at"]) + timedelta(seconds=60) <= at
    ]
    if len(complete) < 3:
        return {"available": False, "passed": False, "reason": "INSUFFICIENT_COMPLETED_1M_BARS"}
    prior, latest = complete[-2], complete[-1]
    setup = observation.get("setup_type")
    bullish_close = latest["close"] > prior["close"] and latest["close"] >= latest["open"]
    if setup == "MICRO_PULLBACK":
        passed = latest["close"] >= latest["open"] and latest["low"] >= min(
            row["low"] for row in complete[-3:-1]
        )
    elif setup == "VWAP_RECLAIM":
        passed = latest["close"] >= prior["close"]
    else:
        passed = bullish_close
    return {
        "available": True, "passed": bool(passed),
        "latest_completed_1m": (
            stamp(latest["begins_at"]) + timedelta(seconds=60)
        ).isoformat(),
        "return_1m": latest["close"] / prior["close"] - 1,
        "bar_direction": latest["close"] / latest["open"] - 1,
        "reason": None if passed else "ONE_MINUTE_STRUCTURE_NOT_CONFIRMED",
    }


def quote_tapes(traces: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trace in traces:
        provenance = trace.get("freshness", {}).get("provenance", {})
        exchange = provenance.get("exchange_timestamp")
        bid, ask = provenance.get("bid"), provenance.get("ask")
        if exchange and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
            result[str(trace.get("symbol", "")).upper()][exchange] = {
                "exchange_timestamp": exchange, "bid": float(bid), "ask": float(ask),
                "evaluation_timestamp": trace.get("timestamp"),
            }
    return {
        symbol: sorted(values.values(), key=lambda row: row["exchange_timestamp"])
        for symbol, values in result.items()
    }


def forward_labels(observation: Mapping[str, Any], tape: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exchange = stamp(observation["exchange_timestamp"])
    entry = float(observation["ask"]) * (1 + config.SCALP_ENTRY_SLIPPAGE_BPS / 10_000)
    stamps = [stamp(row["exchange_timestamp"]) for row in tape]
    result: dict[str, Any] = {"friction_adjusted_entry": entry}
    for seconds in HORIZONS:
        index = bisect_left(stamps, exchange + timedelta(seconds=seconds))
        if index >= len(tape):
            result[f"future_{seconds}s_bid"] = None
            result[f"forward_return_{seconds}s"] = None
            continue
        bid = float(tape[index]["bid"])
        exit_price = bid * (1 - config.SCALP_EXIT_SLIPPAGE_BPS / 10_000)
        result[f"future_{seconds}s_bid"] = bid
        result[f"forward_return_{seconds}s"] = exit_price / entry - 1
        window = [
            row for row in tape
            if exchange <= stamp(row["exchange_timestamp"])
            <= exchange + timedelta(seconds=seconds)
        ]
        window_returns = [
            float(row["bid"]) * (1 - config.SCALP_EXIT_SLIPPAGE_BPS / 10_000)
            / entry - 1 for row in window
        ]
        result[f"mfe_{seconds}s"] = max(window_returns) if window_returns else None
        result[f"mae_{seconds}s"] = min(window_returns) if window_returns else None
        target = observation.get("v1_target")
        stop = observation.get("v1_stop")
        result[f"v1_target_reached_{seconds}s"] = (
            any(float(row["bid"]) >= float(target) for row in window)
            if isinstance(target, (int, float)) else None
        )
        result[f"v1_stop_breached_{seconds}s"] = (
            any(float(row["bid"]) <= float(stop) for row in window)
            if isinstance(stop, (int, float)) else None
        )
    end = exchange + timedelta(seconds=180)
    window = ([
        row for row in tape if exchange <= stamp(row["exchange_timestamp"]) <= end
    ] if result.get("forward_return_180s") is not None else [])
    returns = [
        float(row["bid"]) * (1 - config.SCALP_EXIT_SLIPPAGE_BPS / 10_000) / entry - 1
        for row in window
    ]
    if returns:
        best, worst = max(range(len(returns)), key=returns.__getitem__), min(
            range(len(returns)), key=returns.__getitem__
        )
        result.update({
            "mfe_180s": returns[best], "mae_180s": returns[worst],
            "time_to_mfe_seconds": (
                stamp(window[best]["exchange_timestamp"]) - exchange
            ).total_seconds(),
            "time_to_mae_seconds": (
                stamp(window[worst]["exchange_timestamp"]) - exchange
            ).total_seconds(),
        })
    else:
        result.update({
            "mfe_180s": None, "mae_180s": None,
            "time_to_mfe_seconds": None, "time_to_mae_seconds": None,
        })
    return result


def score_bin(score: float | None) -> str:
    if score is None:
        return "UNAVAILABLE"
    for upper, name in (
        (.30, "<.30"), (.40, ".30-.40"), (.50, ".40-.50"),
        (.60, ".50-.60"), (.65, ".60-.65"), (.70, ".65-.70"),
        (.75, ".70-.75"), (float("inf"), ">.75"),
    ):
        if score < upper:
            return name
    return ">.75"


def outcome_metrics(items: Sequence[Mapping[str, Any]], key: str = "forward_return_180s") -> dict[str, Any]:
    values = [float(row[key]) for row in items if isinstance(row.get(key), (int, float))]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value <= 0]
    gross_win, gross_loss = sum(winners), -sum(losers)
    ordered_equity, peak, max_drawdown = 1.0, 1.0, 0.0
    for value in values:
        ordered_equity *= 1 + value
        peak = max(peak, ordered_equity)
        max_drawdown = max(max_drawdown, 1 - ordered_equity / peak)
    return {
        "count": len(values),
        "net_expectancy": sum(values) / len(values) if values else None,
        "win_rate": len(winners) / len(values) if values else None,
        "average_winner": sum(winners) / len(winners) if winners else None,
        "average_loser": sum(losers) / len(losers) if losers else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (
            "INF" if gross_win > 0 else None
        ),
        "average_mfe": (
            sum(row["mfe_180s"] for row in items if isinstance(row.get("mfe_180s"), (int, float)))
            / sum(isinstance(row.get("mfe_180s"), (int, float)) for row in items)
            if any(isinstance(row.get("mfe_180s"), (int, float)) for row in items) else None
        ),
        "average_mae": (
            sum(row["mae_180s"] for row in items if isinstance(row.get("mae_180s"), (int, float)))
            / sum(isinstance(row.get("mae_180s"), (int, float)) for row in items)
            if any(isinstance(row.get("mae_180s"), (int, float)) for row in items) else None
        ),
        "median_hold_seconds": 180 if values else None,
        "max_drawdown": max_drawdown if values else None,
    }


def independent(items: Sequence[Mapping[str, Any]], *, cooldown: int = 180) -> list[Mapping[str, Any]]:
    selected, last = [], {}
    for row in sorted(items, key=lambda value: value["timestamp"]):
        at = stamp(row["exchange_timestamp"])
        previous = last.get(row["symbol"])
        if previous is not None and (at - previous).total_seconds() < cooldown:
            continue
        if row.get("forward_return_180s") is None:
            continue
        selected.append(row)
        last[row["symbol"]] = at
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--minute-cache", type=Path)
    parser.add_argument("--fetch-minute", action="store_true")
    parser.add_argument("--start", help="Inclusive UTC session start (ISO-8601)")
    parser.add_argument("--end", help="Inclusive UTC session end (ISO-8601)")
    args = parser.parse_args()

    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be supplied together")
    if args.start and args.end:
        start, end = stamp(args.start).isoformat(), stamp(args.end).isoformat()
        if stamp(end) < stamp(start):
            parser.error("--end must not be earlier than --start")
    else:
        start, end = latest_session(args.logs)
    traces = session_traces(args.logs, start, end)
    symbols = sorted({str(row.get("symbol", "")).upper() for row in traces})
    minute_bars: dict[str, list[dict[str, Any]]] = {}
    if args.minute_cache and args.minute_cache.exists() and not args.fetch_minute:
        minute_bars = json.loads(args.minute_cache.read_text(encoding="utf-8"))
    elif args.fetch_minute:
        request_start = stamp(start).replace(hour=13, minute=30, second=0, microsecond=0)
        minute_bars = fetch_minute_bars(
            symbols, request_start.isoformat().replace("+00:00", "Z"),
            end.replace("+00:00", "Z"),
        )
        if args.minute_cache:
            args.minute_cache.parent.mkdir(parents=True, exist_ok=True)
            args.minute_cache.write_text(
                json.dumps(minute_bars, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    engine = HybridScalpResearchEngine(persist=False)
    observations = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        grouped[trace["timestamp"]].append(trace)
    for timestamp in sorted(grouped):
        result = engine.observe(grouped[timestamp], now=stamp(timestamp), persist=False)
        observations.extend(result["observations"])

    tapes = quote_tapes(traces)
    labeled = []
    for observation in observations:
        row = {**observation, **forward_labels(
            observation, tapes.get(observation["symbol"], [])
        )}
        gate = minute_gate(row, minute_bars) if minute_bars else {
            "available": False, "passed": False, "reason": "ONE_MINUTE_DATA_NOT_LOADED"
        }
        row["one_minute_available"] = gate["available"]
        row["one_minute_pass"] = gate["passed"]
        row["one_minute_reason"] = gate.get("reason")
        row["one_minute_return"] = gate.get("return_1m")
        row["hybrid_1m_fast_trigger"] = bool(row["fast_trigger"] and gate["passed"])
        labeled.append(row)

    timeline = [
        row for row in labeled
        if row["symbol"] in {"AMD", "META", "AAPL"}
        and row["context_status"] == "CONTEXT_ACCEPT"
    ]
    args.timeline.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp", "exchange_timestamp", "symbol", "episode_id", "setup_type",
        "context_status", "armed", "armed_at", "trigger_level", "invalidation_price",
        "bar_timestamp", "volume_regime", "v1_score", "v1_signal_pass", "v1_decision",
        "v1_entry", "v1_stop", "v1_target", "v1_gross_rr", "v1_net_rr", "bid", "ask",
        "mid", "spread_pct", "quote_age_seconds", "distance_from_trigger_pct",
        "return_5s", "return_10s", "return_15s", "return_30s",
        "acceleration_5s", "realized_volatility_30s", "selected_trigger",
        "fast_trigger", "one_minute_pass", "v2_decision",
        "future_15s_bid", "forward_return_15s", "future_30s_bid", "forward_return_30s",
        "future_60s_bid", "forward_return_60s", "future_120s_bid", "forward_return_120s",
        "future_180s_bid", "forward_return_180s", "mfe_180s", "mae_180s",
        "time_to_mfe_seconds", "time_to_mae_seconds",
    ]
    with args.timeline.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(timeline)

    eligible = [
        row for row in labeled
        if row["context_status"] == "CONTEXT_ACCEPT" and row["armed"]
    ]
    by_symbol = {
        symbol: {
            "observations": len(group),
            "score": distribution([row["v1_score"] for row in group]),
            **{f"forward_return_{seconds}s": distribution([
                row[f"forward_return_{seconds}s"] for row in group
                if isinstance(row.get(f"forward_return_{seconds}s"), (int, float))
            ]) for seconds in HORIZONS},
            "mfe_180s": distribution([
                row["mfe_180s"] for row in group if isinstance(row.get("forward_return_180s"), (int, float))
            ]),
            "mae_180s": distribution([
                row["mae_180s"] for row in group if isinstance(row.get("forward_return_180s"), (int, float))
            ]),
        }
        for symbol in ("AMD", "META", "AAPL")
        for group in [[row for row in eligible if row["symbol"] == symbol]]
    }

    bins = {}
    for name in ("<.30", ".30-.40", ".40-.50", ".50-.60", ".60-.65",
                 ".65-.70", ".70-.75", ">.75"):
        group = [row for row in eligible if score_bin(row.get("v1_score")) == name]
        bins[name] = {
            "observations": len(group),
            **{f"forward_return_{seconds}s": outcome_metrics(group, f"forward_return_{seconds}s")
               for seconds in HORIZONS},
            "mfe_180s": distribution([
                row["mfe_180s"] for row in group if isinstance(row.get("forward_return_180s"), (int, float))
            ]),
            "mae_180s": distribution([
                row["mae_180s"] for row in group if isinstance(row.get("forward_return_180s"), (int, float))
            ]),
        }

    by_setup = {}
    for setup in sorted({row["setup_type"] for row in eligible}):
        group = [row for row in eligible if row["setup_type"] == setup]
        by_setup[setup] = {
            "observations": len(group),
            **{
                f"{name}_{seconds}s": distribution([
                    row[f"{name}_{seconds}s"] for row in group
                    if isinstance(row.get(f"{name}_{seconds}s"), (int, float))
                ])
                for seconds in HORIZONS for name in ("mfe", "mae")
            },
            "v1_target_reach_rate_180s": (
                sum(row.get("v1_target_reached_180s") is True for row in group)
                / sum(row.get("v1_target_reached_180s") is not None for row in group)
                if any(row.get("v1_target_reached_180s") is not None for row in group)
                else None
            ),
            "v1_stop_breach_rate_180s": (
                sum(row.get("v1_stop_breached_180s") is True for row in group)
                / sum(row.get("v1_stop_breached_180s") is not None for row in group)
                if any(row.get("v1_stop_breached_180s") is not None for row in group)
                else None
            ),
        }

    variants = {}
    for setup, names in SETUP_VARIANTS.items():
        variants[setup] = {}
        for name in names:
            fired = [
                row for row in eligible if row["setup_type"] == setup
                and row.get("trigger_variants", {}).get(name)
            ]
            distinct = independent(fired)
            variants[setup][name] = {
                "triggered_observations": len(fired),
                "independent_trades": outcome_metrics(distinct),
                "false_breakout_rate_60s": (
                    sum((row.get("forward_return_60s") or 0) <= 0 for row in fired
                        if row.get("forward_return_60s") is not None)
                    / sum(row.get("forward_return_60s") is not None for row in fired)
                    if any(row.get("forward_return_60s") is not None for row in fired) else None
                ),
            }

    fully_labeled = [row for row in eligible if row.get("forward_return_180s") is not None]
    times = sorted(stamp(row["timestamp"]) for row in fully_labeled)
    development_end = times[int((len(times) - 1) * .6)] if times else None
    validation_end = times[int((len(times) - 1) * .8)] if times else None

    def split(row):
        at = stamp(row["timestamp"])
        if development_end is None or at <= development_end:
            return "development"
        if at <= validation_end:
            return "validation"
        return "holdout"

    best = {}
    for setup, names in SETUP_VARIANTS.items():
        candidates = []
        for name in names:
            dev = independent([
                row for row in fully_labeled if row["setup_type"] == setup
                and split(row) == "development"
                and row.get("trigger_variants", {}).get(name)
            ])
            metric = outcome_metrics(dev)
            if metric["count"]:
                candidates.append((metric["net_expectancy"], metric["count"], name))
        if not candidates:
            best[setup] = {"selected": None, "reason": "NO_DEVELOPMENT_TRIGGERS"}
            continue
        selected = max(candidates)[2]
        best[setup] = {"selected": selected}
        for partition in ("development", "validation", "holdout"):
            sample = independent([
                row for row in fully_labeled if row["setup_type"] == setup
                and split(row) == partition
                and row.get("trigger_variants", {}).get(selected)
            ])
            best[setup][partition] = outcome_metrics(sample)

    v1_entries = [row for row in eligible if row.get("v1_decision") == "SHADOW_ENTRY"]
    v2_fast_triggers = independent([
        row for row in eligible if row.get("fast_trigger")
    ])
    v2_1m_fast_triggers = independent([
        row for row in eligible if row.get("fast_trigger") and row.get("one_minute_pass")
    ])
    v2_entries = independent([
        row for row in eligible if row.get("v2_decision") == "V2_ENTRY_READY"
    ])
    v2_1m_entries = independent([
        row for row in eligible if row.get("v2_decision") == "V2_ENTRY_READY"
        and row.get("one_minute_pass")
    ])
    positive_v2 = [row for row in v2_entries if (row.get("forward_return_180s") or 0) > 0]
    summary = {
        "strategy_control": "SCALP_V1_CONTROL",
        "strategy_experimental": config.SCALP_V2_STRATEGY_ID,
        "research_only": True,
        "session": {"start": start, "end": end, "observations": len(traces),
                    "episodes": len({row.get("episode_id") for row in traces if row.get("episode_id")})},
        "latest_symbols": by_symbol,
        "score_bins": bins,
        "setup_horizon_geometry": by_setup,
        "score_outcome_correlation": {
            f"forward_return_{seconds}s": correlation([
                (float(row["v1_score"]), float(row[f"forward_return_{seconds}s"]))
                for row in eligible
                if isinstance(row.get("v1_score"), (int, float))
                and isinstance(row.get(f"forward_return_{seconds}s"), (int, float))
            ]) for seconds in HORIZONS
        },
        "trigger_variants": variants,
        "chronological_split": {
            "method": "60/20/20 over observations with full 180-second labels",
            "development_end": development_end.isoformat() if development_end else None,
            "validation_end": validation_end.isoformat() if validation_end else None,
            "fully_labeled_observations": len(fully_labeled),
        },
        "best_trigger_by_setup": best,
        "controlled_replay": {
            "SCALP_V1_CONTROL": {"trades": len(v1_entries), **outcome_metrics(v1_entries)},
            "HYBRID_V2_FAST_TRIGGER_LABELS_BEFORE_GEOMETRY": outcome_metrics(v2_fast_triggers),
            "HYBRID_V2_1M_GATED_TRIGGER_LABELS_BEFORE_GEOMETRY": outcome_metrics(
                v2_1m_fast_triggers
            ),
            "HYBRID_V2_5M_CONTEXT_QUOTE_TRIGGER": outcome_metrics(v2_entries),
            "HYBRID_V2_5M_CONTEXT_1M_SETUP_QUOTE_TRIGGER": outcome_metrics(v2_1m_entries),
        },
        "funnel": {
            "v1_eligible_observations": len(eligible),
            "v1_signal_pass_observations": sum(
                row.get("v1_signal_pass", False) for row in eligible
            ),
            "v2_armed_episodes": len({row.get("episode_id") for row in eligible}),
            "v2_selected_fast_trigger_observations": sum(row.get("fast_trigger", False) for row in eligible),
            "v2_independent_fast_triggers": len(v2_fast_triggers),
            "v2_one_minute_gated_independent_triggers": len(v2_1m_fast_triggers),
            "v2_entry_ready_observations": sum(
                row.get("v2_decision") == "V2_ENTRY_READY" for row in eligible
            ),
            "v2_geometry_blockers": dict(Counter(
                reason for row in eligible if row.get("fast_trigger")
                for reason in (row.get("geometry") or {}).get("reasons", [])
            )),
        },
        "latency": {
            "v2_armed_to_trigger_seconds": distribution([
                row["latency"]["armed_to_trigger_seconds"] for row in eligible
                if row.get("fast_trigger") and row.get("latency", {}).get(
                    "armed_to_trigger_seconds"
                ) is not None
            ]),
            "v2_trigger_to_entry_ready_ms": distribution([
                row["latency"]["trigger_to_entry_ready_ms"] for row in eligible
                if row.get("latency", {}).get("trigger_to_entry_ready_ms") is not None
            ]),
            "v1_entry_ready_observations": len(v1_entries),
        },
        "opportunities": {
            "positive_v2_independent_trades": len(positive_v2),
            "positive_v2_missed_by_v1": sum(
                row.get("v1_decision") != "SHADOW_ENTRY" for row in positive_v2
            ),
            "percent_positive_v2_missed_by_v1": (
                100 * sum(row.get("v1_decision") != "SHADOW_ENTRY" for row in positive_v2)
                / len(positive_v2) if positive_v2 else None
            ),
        },
        "one_minute_capability": {
            "requested": bool(minute_bars),
            "symbols_returned": len(minute_bars),
            "bars_returned": sum(len(value) for value in minute_bars.values()),
            "interpolated_bars": sum(
                row.get("interpolated", False) for value in minute_bars.values() for row in value
            ),
            "observations_with_completed_1m_context": sum(
                row.get("one_minute_available", False) for row in eligible
            ),
        },
        "polling_experiment": {
            "source_observation_cadence_seconds": distribution([
                (stamp(right["timestamp"]) - stamp(left["timestamp"])).total_seconds()
                for symbol in symbols
                for group in [[row for row in observations if row["symbol"] == symbol]]
                for left, right in zip(group, group[1:])
            ]),
            "1.0_second_replay": "UNAVAILABLE_SOURCE_SAMPLING_TOO_SLOW",
            "0.5_second_replay": "UNAVAILABLE_SOURCE_SAMPLING_TOO_SLOW",
        },
        "limitations": [
            "The latest session spans only about six minutes.",
            "Observation-level labels are serially correlated; independent-trade metrics enforce a 180-second per-symbol cooldown.",
            "Chronological validation and holdout partitions are very small and cannot justify promotion.",
            "One-minute results are from an offline read-only historical query and are not yet a live fast-path feed.",
        ],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
