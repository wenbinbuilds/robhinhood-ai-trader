#!/usr/bin/env python3
"""Offline, read-only SCALP latency audit from persisted local journals."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue


def percentile(values, quantile):
    ordered = sorted(
        float(value) for value in values
        if value is not None and math.isfinite(float(value))
    )
    if not ordered:
        return None
    index = (len(ordered) - 1) * quantile
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def distribution(values):
    clean = [
        float(value) for value in values
        if value is not None and math.isfinite(float(value))
    ]
    return {
        "count": len(clean),
        "median": median(clean) if clean else None,
        "p75": percentile(clean, .75),
        "p90": percentile(clean, .90),
        "p95": percentile(clean, .95),
        "max": max(clean) if clean else None,
    }


def first(items, predicate):
    return next((item for item in items if predicate(item["payload"])), None)


def trace_row(item):
    payload = item["payload"]
    provenance = payload["freshness"].get("provenance", {})
    geometry = payload.get("geometry", {})
    lifecycle = payload.get("episode_lifecycle", {})
    components = payload.get("signal", {}).get("components", {})
    mid = provenance.get("mid")
    anchor = lifecycle.get("original_anchor_price")
    return {
        "evaluation_timestamp": item.get("timestamp"),
        "exchange_timestamp": provenance.get("exchange_timestamp"),
        "provider_response_received": provenance.get("received_timestamp"),
        "bid": provenance.get("bid"),
        "ask": provenance.get("ask"),
        "mid": mid,
        "quote_age_seconds": provenance.get("exchange_quote_age_seconds"),
        "provider_latency_ms": provenance.get("provider_latency_ms"),
        "poll_interval_seconds": provenance.get("poll_interval_since_previous_seconds"),
        "spread_pct": provenance.get("spread_pct"),
        "breakout_reference": anchor,
        "distance_from_breakout": (
            float(mid) - float(anchor) if mid is not None and anchor is not None else None
        ),
        "distance_from_breakout_bps": (
            (float(mid) / float(anchor) - 1) * 10_000
            if mid is not None and anchor else None
        ),
        "micro_momentum": components.get("micro_momentum", {}).get("raw"),
        "relative_strength": components.get("relative_strength", {}).get("raw"),
        "ema9": geometry.get("structural_levels", {}).get("ema9"),
        "ema20": lifecycle.get("fingerprint_fields", {}).get("ema20"),
        "vwap": geometry.get("structural_levels", {}).get("vwap"),
        "volume_expansion": payload.get("volume_expansion", {}).get("observed"),
        "score": payload.get("signal", {}).get("score"),
        "extension_pct": geometry.get("entry_extension_pct"),
        "stop": geometry.get("stop"),
        "target": geometry.get("target"),
        "gross_rr": geometry.get("gross_rr"),
        "net_rr": geometry.get("net_rr"),
        "decision": payload.get("final"),
        "reasons": ",".join(payload.get("rejection_reasons", [])),
    }


def forward_outcome(entry, quotes, seconds, *, slippage=.00025):
    at = stamp(entry["evaluation_timestamp"])
    future = next(
        (quote for quote in quotes
         if stamp(quote["evaluation_timestamp"]) >= at + timedelta(seconds=seconds)),
        None,
    )
    if future is None:
        return None
    fill = float(entry["ask"]) * (1 + slippage)
    exit_fill = float(future["bid"]) * (1 - slippage)
    return exit_fill / fill - 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", required=True)
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    observations = sorted(
        (item for item in rows(args.logs / "scalp_diagnostics.jsonl")
         if item.get("record_type") == "CANDIDATE_OBSERVATION"
         and item.get("episode_id") == args.episode),
        key=lambda item: item["timestamp"],
    )
    if not observations:
        raise SystemExit(f"episode not found: {args.episode}")
    episode_start = observations[0]["timestamp"]

    starts = [
        item["timestamp"] for item in rows(args.logs / "fast_watcher.jsonl")
        if item.get("event") == "FAST_WATCHER_STARTED"
        and item.get("timestamp", "") <= episode_start
    ]
    session_start = starts[-1] if starts else episode_start
    session_end = observations[-1]["timestamp"]
    quote_rows = [
        item for item in rows(args.logs / "quote_provenance.jsonl")
        if session_start <= item.get("timestamp", "") <= session_end
        and "SCALP" in item.get("strategy_scopes", [])
    ]
    amd_quotes = sorted(
        (item for item in quote_rows if item.get("symbol") == "AMD"
         and item.get("bid") is not None and item.get("ask") is not None
         and item.get("exchange_quote_age_seconds") is not None
         and 0 <= float(item["exchange_quote_age_seconds"]) <= 2.0),
        key=lambda item: item["evaluation_timestamp"],
    )

    output_rows = [trace_row(item) for item in observations]
    args.observations.parent.mkdir(parents=True, exist_ok=True)
    with args.observations.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)

    milestone_predicates = {
        "T1_setup_classified": lambda p: p.get("setup_detected_anywhere"),
        "T2_episode_created": lambda _p: True,
        "T3_first_eligible": lambda p: p.get("stage_flags", {}).get(
            "eligible_micro_signals"
        ),
        "T4_first_score_060": lambda p: (p.get("signal", {}).get("score") or -1) >= .60,
        "T5_first_score_065": lambda p: (p.get("signal", {}).get("score") or -1) >= .65,
        "T6_first_score_068": lambda p: (p.get("signal", {}).get("score") or -1) >= .68,
        "T7_first_score_070": lambda p: (p.get("signal", {}).get("score") or -1) >= .70,
        "T8_first_overextended": lambda p: "ENTRY_OVEREXTENDED" in p.get(
            "rejection_reasons", []
        ),
        "T9_first_rr_failure": lambda p: "SCALP_RR_BELOW_MINIMUM" in p.get(
            "rejection_reasons", []
        ),
        "T10_shadow_entry": lambda p: p.get("final") == "SHADOW_ENTRY",
    }
    milestones = {}
    for name, predicate in milestone_predicates.items():
        item = first(observations, predicate)
        milestones[name] = trace_row(item) if item else None

    unique_cycles = {}
    for item in quote_rows:
        unique_cycles[item.get("poll_cycle_id")] = item
    cycles = sorted(unique_cycles.values(), key=lambda item: item["evaluation_timestamp"])
    cadence = [
        (stamp(right["evaluation_timestamp"]) - stamp(left["evaluation_timestamp"])).total_seconds()
        for left, right in zip(cycles, cycles[1:])
    ]

    per_symbol = {}
    for symbol in sorted({item.get("symbol") for item in quote_rows}):
        group = [item for item in quote_rows if item.get("symbol") == symbol]
        ages = [item.get("exchange_quote_age_at_receive_seconds") for item in group]
        clean = [float(value) for value in ages if value is not None]
        per_symbol[symbol] = {
            "quote_age_at_receive_seconds": distribution(clean),
            "provider_latency_ms": distribution(
                item.get("provider_latency_ms") for item in group
            ),
            "percent_le_500ms": 100 * sum(value <= .5 for value in clean) / len(clean),
            "percent_le_1s": 100 * sum(value <= 1 for value in clean) / len(clean),
            "percent_le_2s": 100 * sum(value <= 2 for value in clean) / len(clean),
            "percent_le_3s": 100 * sum(value <= 3 for value in clean) / len(clean),
            "percent_gt_3s": 100 * sum(value > 3 for value in clean) / len(clean),
        }

    eligible = milestones["T3_first_eligible"]
    current_entry = next(
        item for item in amd_quotes
        if item["evaluation_timestamp"] >= eligible["evaluation_timestamp"]
    ) if eligible else None
    bar_close = stamp("2026-09-24T18:40:00+00:00")
    anchor = output_rows[0]["breakout_reference"]
    causal_quote = next(
        (item for item in amd_quotes
         if stamp(item["exchange_timestamp"]) >= bar_close
         and item.get("mid") is not None and float(item["mid"]) > float(anchor)),
        None,
    )
    counterfactuals = {}
    for name, entry in (
        ("first_causal_context_plus_quote", causal_quote),
        ("first_valid_setup_and_score_060_065_068_070", current_entry),
    ):
        if entry is None:
            counterfactuals[name] = None
            continue
        at = stamp(entry["evaluation_timestamp"])
        fill = float(entry["ask"]) * 1.00025
        window = [
            item for item in amd_quotes
            if at <= stamp(item["evaluation_timestamp"]) <= at + timedelta(seconds=180)
        ]
        returns = [float(item["bid"]) * .99975 / fill - 1 for item in window]
        counterfactuals[name] = {
            "evaluation_timestamp": entry["evaluation_timestamp"],
            "exchange_timestamp": entry["exchange_timestamp"],
            "ask": entry["ask"],
            "spread_pct": entry.get("spread_pct"),
            "slippage_adjusted_entry": fill,
            "return_15s": forward_outcome(entry, amd_quotes, 15),
            "return_30s": forward_outcome(entry, amd_quotes, 30),
            "return_60s": forward_outcome(entry, amd_quotes, 60),
            "return_120s": forward_outcome(entry, amd_quotes, 120),
            "return_180s": forward_outcome(entry, amd_quotes, 180),
            "mfe_180s": max(returns) if returns else None,
            "mae_180s": min(returns) if returns else None,
        }

    signal_pass = [
        item for item in observations
        if item["payload"].get("stage_flags", {}).get("signal_score_pass")
    ]
    summary = {
        "episode_id": args.episode,
        "session_start": session_start,
        "session_end": session_end,
        "episode_observations": len(observations),
        "session_scalp_quote_observations": len(quote_rows),
        "provider_latency_ms": distribution(
            item.get("provider_latency_ms") for item in quote_rows
        ),
        "quote_age_at_receive_seconds": distribution(
            item.get("exchange_quote_age_at_receive_seconds") for item in quote_rows
        ),
        "local_receive_to_evaluation_ms": distribution(
            (stamp(item["evaluation_timestamp"]) - stamp(item["received_timestamp"])).total_seconds() * 1000
            for item in quote_rows
            if item.get("evaluation_timestamp") and item.get("received_timestamp")
        ),
        "poll_cadence_seconds": distribution(cadence),
        "full_universe_refresh_seconds": distribution(
            item.get("full_universe_cycle_duration_seconds") for item in quote_rows
        ),
        "per_symbol": per_symbol,
        "milestones": milestones,
        "causal_quote": causal_quote,
        "counterfactuals": counterfactuals,
        "signal_pass_observations": len(signal_pass),
        "signal_pass_overextended": sum(
            "ENTRY_OVEREXTENDED" in item["payload"].get("rejection_reasons", [])
            for item in signal_pass
        ),
        "signal_pass_rr_failure": sum(
            "SCALP_RR_BELOW_MINIMUM" in item["payload"].get("rejection_reasons", [])
            for item in signal_pass
        ),
        "rejection_counts": Counter(
            reason for item in observations
            for reason in item["payload"].get("rejection_reasons", [])
        ),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
