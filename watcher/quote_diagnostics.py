"""Offline aggregation for persisted quote-request provenance."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Any


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def quote_age_distribution(values: Iterable[float]) -> dict[str, Any]:
    ages = [float(value) for value in values if value is not None and value >= 0]
    count = len(ages)
    percent = lambda predicate: (
        100 * sum(predicate(value) for value in ages) / count if count else None
    )
    return {
        'count': count,
        'min': min(ages) if ages else None,
        'median': median(ages) if ages else None,
        'p75': _percentile(ages, .75),
        'p90': _percentile(ages, .90),
        'p95': _percentile(ages, .95),
        'p99': _percentile(ages, .99),
        'max': max(ages) if ages else None,
        'percent_le_1s': percent(lambda value: value <= 1),
        'percent_le_2s': percent(lambda value: value <= 2),
        'percent_le_3s': percent(lambda value: value <= 3),
        'percent_le_5s': percent(lambda value: value <= 5),
        'percent_le_30s': percent(lambda value: value <= 30),
        'percent_gt_60s': percent(lambda value: value > 60),
        'percent_gt_120s': percent(lambda value: value > 120),
        'percent_gt_300s': percent(lambda value: value > 300),
    }


def _rows(path: str | Path, *, session_date=None) -> list[dict[str, Any]]:
    result = []
    try:
        stream = Path(path).open(encoding='utf-8')
    except FileNotFoundError:
        return result
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get('event') != 'QUOTE_REQUEST_TRACE':
                    continue
                stamp = datetime.fromisoformat(
                    str(row.get('timestamp', '')).replace('Z', '+00:00')
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if session_date is not None and stamp.astimezone(timezone.utc).date() != session_date:
                continue
            result.append(row)
    return result


def quote_provenance_summary(
    path: str | Path, *, strategy: str, session_date=None,
) -> dict[str, Any]:
    """Report request cadence and exchange age for one strategy consumer."""

    strategy = strategy.upper()
    rows = [
        row for row in _rows(path, session_date=session_date)
        if strategy in row.get('strategy_scopes', [])
    ]
    by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get('symbol'):
            by_symbol[str(row['symbol']).upper()].append(row)
    ages = [row.get('exchange_quote_age_seconds') for row in rows]
    latency = [float(row['provider_latency_ms']) for row in rows
               if row.get('provider_latency_ms') is not None]
    cycles = {}
    for row in rows:
        cycle_id = row.get('poll_cycle_id')
        if cycle_id:
            cycles[cycle_id] = row.get('full_universe_cycle_duration_seconds')
    stale_reasons = Counter(
        str(row['stale_reason']) for row in rows if row.get('stale_reason')
    )
    per_symbol = {}
    for symbol, group in sorted(by_symbol.items()):
        symbol_ages = [float(row['exchange_quote_age_seconds']) for row in group
                       if row.get('exchange_quote_age_seconds') is not None]
        symbol_latency = [float(row['provider_latency_ms']) for row in group
                          if row.get('provider_latency_ms') is not None]
        successes = [row for row in group if row.get('provider_status') == 'OK']
        intervals = [float(row['poll_interval_since_previous_seconds']) for row in group
                     if row.get('poll_interval_since_previous_seconds') is not None]
        per_symbol[symbol] = {
            'observations': len(group),
            'poll_attempts': sum(bool(row.get('requested_this_cycle')) for row in group),
            'provider_successes': len(successes),
            'median_quote_age_seconds': median(symbol_ages) if symbol_ages else None,
            'p90_quote_age_seconds': _percentile(symbol_ages, .90),
            'max_quote_age_seconds': max(symbol_ages) if symbol_ages else None,
            'freshness_pass_rate_percent': (
                100 * sum(bool(row.get('freshness_by_strategy', {}).get(strategy))
                          for row in group) / len(group) if group else None
            ),
            'median_provider_latency_ms': median(symbol_latency) if symbol_latency else None,
            'median_poll_interval_seconds': median(intervals) if intervals else None,
            'cache_hit_rate_percent': (
                100 * sum(bool(row.get('cache_hit')) for row in group) / len(group)
                if group else None
            ),
            'last_exchange_timestamp': next(
                (row.get('exchange_timestamp') for row in reversed(group)
                 if row.get('exchange_timestamp')), None,
            ),
            'stale_reasons': dict(Counter(
                str(row['stale_reason']) for row in group if row.get('stale_reason')
            )),
        }
    durations = [float(value) for value in cycles.values() if value is not None]
    return {
        'strategy': strategy,
        'observations': len(rows),
        'poll_cycles': len(cycles),
        'quote_age': quote_age_distribution(ages),
        'median_provider_latency_ms': median(latency) if latency else None,
        'p95_provider_latency_ms': _percentile(latency, .95),
        'median_full_universe_cycle_duration_seconds': (
            median(durations) if durations else None
        ),
        'p95_full_universe_cycle_duration_seconds': _percentile(durations, .95),
        'stale_reasons': dict(stale_reasons),
        'cache_hit_rate_percent': (
            100 * sum(bool(row.get('cache_hit')) for row in rows) / len(rows)
            if rows else None
        ),
        'per_symbol': per_symbol,
    }
