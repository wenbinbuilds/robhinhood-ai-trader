"""Boundary-aware direct historical refresh for the shadow scalp runtime."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from threading import RLock, Thread
from time import monotonic
from typing import Any, Callable, Mapping, Sequence

import config
from agent.technical_indicators import calculate_indicators
from robinhood_mcp.normalization import normalized_historicals
from strategies.scalp.freshness import completed_micro_bars, micro_bar_freshness


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _return_3bar(row: Mapping[str, Any]) -> float | None:
    bars = completed_micro_bars(row.get('candles', []) or [], datetime.max.replace(tzinfo=timezone.utc))
    closes = [bar['close'] for bar in bars]
    return closes[-1] / closes[-4] - 1 if len(closes) >= 4 and closes[-4] else None


def _relative_volume(bars: Sequence[Mapping[str, Any]]) -> float | None:
    volumes = [float(row['volume']) for row in bars[-6:]]
    baseline = mean(volumes[:-1]) if len(volumes) >= 2 else None
    return volumes[-1] / baseline if baseline and baseline > 0 else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


class ScalpHistoryRefresher:
    """Refreshes only due scalp/benchmark histories, independently of the LLM.

    The supplied client exposes only the direct read-only historical batch used
    by the snapshot collector. No quote, account, scanner, or order operation is
    reachable from this component.
    """

    BENCHMARKS = ('SPY', 'QQQ')

    def __init__(self, client, base_lookup: Callable[[str], Mapping[str, Any]], *, clock=None):
        self.client = client
        self.base_lookup = base_lookup
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._last_attempt: dict[str, datetime] = {}
        self._latencies: list[float] = []
        self._history_ages: list[float] = []
        self._request_batches = self._symbols_requested = 0
        self._successes = self._unchanged = self._failures = 0
        self._worker: Thread | None = None
        self._pending_result: dict[str, Any] | None = None

    def _seed(self, symbol: str) -> dict[str, Any]:
        key = symbol.upper()
        if key not in self._rows:
            self._rows[key] = deepcopy(dict(self.base_lookup(key) or {}))
        return self._rows[key]

    def get(self, symbol: str) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self._seed(symbol))

    def get_many(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Capture one atomic cache generation for a full signal cycle."""
        with self.lock:
            return {symbol.upper(): deepcopy(self._seed(symbol)) for symbol in symbols}

    def _due(self, symbol: str, now: datetime) -> bool:
        row = self._seed(symbol)
        freshness = micro_bar_freshness(
            row.get('candles', []) or [], now=now,
            provider_status=row.get('micro_bar_provider_status', 'OK' if row else 'UNAVAILABLE'),
        )
        if freshness.refresh_due_at is None:
            due = True
        else:
            due_at = datetime.fromisoformat(freshness.refresh_due_at)
            due = now >= due_at
        attempted = self._last_attempt.get(symbol)
        if attempted is not None and now - attempted < timedelta(
                seconds=config.SCALP_MICRO_BAR_REFRESH_RETRY_SECONDS):
            return False
        return due

    def refresh(self, symbols: Sequence[str], *, now: datetime) -> dict[str, Any]:
        """Refresh due symbols in one batch and return cycle-level evidence."""

        current = _utc(now)
        due = self._claim_due(symbols, current)
        if not due:
            return self._result(current, (), (), (), (), 0.0)
        return self._refresh_due(due, current)

    def _claim_due(self, symbols: Sequence[str], current: datetime) -> list[str]:
        with self.lock:
            requested = list(dict.fromkeys(
                [str(symbol).upper() for symbol in symbols if symbol]
                + list(self.BENCHMARKS)
            ))
            due = [symbol for symbol in requested if self._due(symbol, current)]
            if not due:
                return []
            for symbol in due:
                self._last_attempt[symbol] = current
            self._request_batches += 1
            self._symbols_requested += len(due)
            return due

    def poll(self, symbols: Sequence[str], *, now: datetime,
             quote_timestamps: Mapping[str, datetime] | None = None) -> dict[str, Any]:
        """Schedule a due batch without blocking the fast quote watcher."""

        current = _utc(now)
        schedule_started = monotonic()
        with self.lock:
            if self._pending_result is not None:
                result, self._pending_result = self._pending_result, None
                return {**result, 'refresh_completed': True,
                        'refresh_in_progress': False,
                        'event_loop_blocking_duration_seconds': monotonic()-schedule_started}
            if self._worker is not None and self._worker.is_alive():
                return {
                    **self._result(current, (), (), (), (), 0.0),
                    'refresh_in_progress': True, 'refresh_completed': False,
                    'event_loop_blocking_duration_seconds': monotonic()-schedule_started,
                }
            due = self._claim_due(symbols, current)
            if not due:
                return {
                    **self._result(current, (), (), (), (), 0.0),
                    'refresh_in_progress': False, 'refresh_completed': False,
                    'event_loop_blocking_duration_seconds': monotonic()-schedule_started,
                }
            stamps = dict(quote_timestamps or {})
            self._worker = Thread(
                target=self._background_refresh,
                args=(due, current, stamps),
                name='scalp-history-refresh', daemon=True,
            )
            self._worker.start()
            return {
                **self._result(current, due, (), (), (), 0.0),
                'refresh_started': True, 'refresh_in_progress': True,
                'refresh_completed': False,
                'event_loop_blocking_duration_seconds': monotonic()-schedule_started,
            }

    def _background_refresh(self, due, current, quote_timestamps):
        result = self._refresh_due(due, current, quote_timestamps=quote_timestamps)
        with self.lock:
            self._pending_result = result

    def close(self):
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=config.ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS + 5)

    def _refresh_due(self, due: Sequence[str], current: datetime, *,
                     quote_timestamps: Mapping[str, datetime] | None = None) -> dict[str, Any]:

        started = monotonic()
        try:
            calls = self.client.get_historicals_many(due)
            if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
                raise ValueError('invalid historical batch')
        except Exception:
            calls = [RuntimeError('historical refresh failed')] * len(due)
        latency = monotonic() - started
        completed_at = _utc(self.clock())
        successes, unchanged, failures = [], [], []
        with self.lock:
            self._latencies.append(latency)
            for index, symbol in enumerate(due):
                call = calls[index] if index < len(calls) else RuntimeError('missing history result')
                previous = self._seed(symbol)
                if isinstance(call, BaseException):
                    previous['micro_bar_provider_status'] = 'ERROR'
                    previous['micro_bar_refresh_error'] = type(call).__name__
                    failures.append(symbol)
                    continue
                try:
                    prior_freshness = micro_bar_freshness(
                        previous.get('candles', []) or [], now=completed_at,
                        provider_status=previous.get('micro_bar_provider_status', 'OK'),
                    )
                    raw = normalized_historicals(call.value, symbol)
                    indicators = calculate_indicators(
                        raw.get('candles', []), now=completed_at, completed_only=True,
                    )
                    bars = indicators['candles']
                    if not bars:
                        raise ValueError('no completed historical bars')
                    previous.update(indicators)
                    previous.update({
                        'symbol': symbol,
                        'micro_bar_provider_status': 'OK',
                        'micro_bar_refreshed_at': completed_at.isoformat(),
                        'micro_bar_refresh_error': None,
                    })
                    provider_relative_volume = raw.get('relative_volume')
                    bar_relative_volume = _relative_volume(bars)
                    if provider_relative_volume is not None:
                        previous['relative_volume'] = provider_relative_volume
                        previous['relative_volume_source'] = 'PROVIDER_HISTORY'
                    else:
                        previous['relative_volume'] = bar_relative_volume
                        previous['relative_volume_source'] = (
                            'COMPLETED_MICRO_BAR_RATIO' if bar_relative_volume is not None
                            else 'UNAVAILABLE'
                        )
                    previous['relative_volume_timestamp'] = (
                        micro_bar_freshness(bars, now=completed_at).latest_completed_bar_timestamp
                    )
                    previous['relative_volume_status'] = (
                        'OK' if previous['relative_volume'] is not None else 'UNAVAILABLE'
                    )
                    fresh = micro_bar_freshness(bars, now=completed_at)
                    if fresh.bar_age_seconds is not None:
                        self._history_ages.append(fresh.bar_age_seconds)
                    if (prior_freshness.latest_completed_bar_timestamp is not None
                            and fresh.latest_completed_bar_timestamp
                            <= prior_freshness.latest_completed_bar_timestamp):
                        unchanged.append(symbol)
                    else:
                        successes.append(symbol)
                except Exception as exc:
                    previous['micro_bar_provider_status'] = 'ERROR'
                    previous['micro_bar_refresh_error'] = type(exc).__name__
                    failures.append(symbol)
            self._successes += len(successes)
            self._unchanged += len(unchanged)
            self._failures += len(failures)
            benchmark_returns = {
                symbol: _return_3bar(self._seed(symbol)) for symbol in self.BENCHMARKS
            }
            benchmark_freshness = {
                symbol: micro_bar_freshness(
                    self._seed(symbol).get('candles', []) or [], now=completed_at,
                    provider_status=self._seed(symbol).get('micro_bar_provider_status', 'OK'),
                ) for symbol in self.BENCHMARKS
            }
            for symbol, row in self._rows.items():
                if symbol not in self.BENCHMARKS:
                    row['spy_return_3bar'] = benchmark_returns['SPY']
                    row['qqq_return_3bar'] = benchmark_returns['QQQ']
                    row['spy_feature_timestamp'] = benchmark_freshness['SPY'].latest_completed_bar_timestamp
                    row['qqq_feature_timestamp'] = benchmark_freshness['QQQ'].latest_completed_bar_timestamp
                    row['spy_feature_status'] = benchmark_freshness['SPY'].freshness_status
                    row['qqq_feature_status'] = benchmark_freshness['QQQ'].freshness_status
                    row['benchmark_provider_status'] = (
                        'OK' if all(value.provider_status == 'OK'
                                    for value in benchmark_freshness.values()) else 'ERROR'
                    )
            timing = self._timing(
                current, completed_at, due, latency, quote_timestamps or {},
            )
            return self._result(
                completed_at, due, successes, unchanged, failures, latency,
                timing=timing,
            )

    @staticmethod
    def _timing(started_at, completed_at, due, provider_latency, quote_timestamps):
        before = {
            symbol: (started_at - stamp).total_seconds()
            for symbol, stamp in quote_timestamps.items()
            if isinstance(stamp, datetime) and stamp.tzinfo is not None
        }
        after = {
            symbol: (completed_at - stamp).total_seconds()
            for symbol, stamp in quote_timestamps.items()
            if isinstance(stamp, datetime) and stamp.tzinfo is not None
        }
        crossed = sum(
            before.get(symbol, float('inf')) <= config.SCALP_MAX_QUOTE_AGE_SECONDS
            < age for symbol, age in after.items()
        )
        return {
            'refresh_start': started_at.isoformat(),
            'refresh_end': completed_at.isoformat(),
            'duration_seconds': (completed_at-started_at).total_seconds(),
            'provider_latency_seconds': provider_latency,
            'symbols_requested': list(due),
            'quote_age_immediately_before': before,
            'quote_age_immediately_after': after,
            'quotes_crossing_max_age_during_refresh': crossed,
        }

    def _result(self, completed_at, attempted, successes, unchanged, failures, latency,
                *, timing=None):
        total = self._successes + self._unchanged + self._failures
        return {
            'attempted_symbols': list(attempted),
            'successful_symbols': list(successes),
            'unchanged_symbols': list(unchanged),
            'failed_symbols': list(failures),
            'attempt_count': len(attempted),
            'success_count': len(successes),
            'unchanged_count': len(unchanged),
            'failure_count': len(failures),
            'latency_seconds': latency,
            'completed_at': completed_at.isoformat(),
            'provider_metrics': {
                'request_batches': self._request_batches,
                'symbols_requested': self._symbols_requested,
                'successes': self._successes,
                'successful_responses_without_new_bar': self._unchanged,
                'failures': self._failures,
                'response_success_rate': (
                    (self._successes + self._unchanged) / total if total else None
                ),
                'success_rate': (
                    (self._successes + self._unchanged) / total if total else None
                ),
                'new_bar_success_rate': self._successes / total if total else None,
                'latency_median_seconds': median(self._latencies) if self._latencies else None,
                'latency_p95_seconds': _percentile(self._latencies, .95),
                'latency_max_seconds': max(self._latencies) if self._latencies else None,
                'returned_history_age_median_seconds': median(self._history_ages) if self._history_ages else None,
                'returned_history_age_p95_seconds': _percentile(self._history_ages, .95),
                'returned_history_age_max_seconds': max(self._history_ages) if self._history_ages else None,
            },
            'timing': dict(timing or {}),
        }
