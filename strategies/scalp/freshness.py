"""Timeframe-aware completed-bar validation for the scalp data path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping, Sequence

import config
from watcher.models import timestamp


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def completed_micro_bars(
    rows: Sequence[Mapping[str, Any]], now: datetime,
) -> list[dict[str, Any]]:
    """Return causal, completed OHLCV bars with explicit interval/provenance."""

    current = _utc(now)
    result = []
    for row in rows:
        began = timestamp(row.get('begins_at'))
        interval = _number(row.get('interval_seconds')) or 300.0
        if began is None or interval <= 0 or row.get('is_forming') or row.get('interpolated'):
            continue
        began = began.astimezone(timezone.utc)
        if began + timedelta(seconds=interval) > current:
            continue
        values = {
            name: _number(row.get(name))
            for name in ('open', 'high', 'low', 'close', 'volume')
        }
        if (any(value is None for value in values.values())
                or values['high'] < values['low'] or values['volume'] < 0):
            continue
        result.append({
            **values,
            'begins_at': began.isoformat(),
            'interval_seconds': interval,
            'bar_source': str(row.get('bar_source') or 'PROVIDER_HISTORY'),
        })
    result.sort(key=lambda row: row['begins_at'])
    if not result:
        return result
    # A feature vector must never combine one-minute and five-minute volumes,
    # returns, or EMA inputs. The newest completed bar declares the active
    # series timeframe; mismatched rows are excluded rather than resampled.
    active_interval = result[-1]['interval_seconds']
    return [row for row in result if row['interval_seconds'] == active_interval]


@dataclass(frozen=True)
class MicroBarFreshness:
    bar_timeframe_seconds: float | None
    latest_completed_bar_begins_at: str | None
    latest_completed_bar_timestamp: str | None
    current_timestamp: str
    bar_age_seconds: float | None
    expected_next_bar_close: str | None
    refresh_due_at: str | None
    allowed_lag_seconds: float
    provider_status: str
    freshness_status: str
    freshness_reason: str
    bar_source: str

    @property
    def usable(self) -> bool:
        return self.freshness_status in {'FRESH', 'AGING'}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def micro_bar_freshness(
    rows: Sequence[Mapping[str, Any]], *, now: datetime,
    provider_status: str = 'OK',
    allowed_lag_seconds: float = config.SCALP_MAX_MICRO_BAR_AGE_SECONDS,
    refresh_grace_seconds: float = config.SCALP_MICRO_BAR_REFRESH_GRACE_SECONDS,
) -> MicroBarFreshness:
    """Classify the latest completed bar relative to its next close boundary.

    ``allowed_lag_seconds`` begins when the *next* bar should have completed;
    it is not a quote-style age limit measured from the current bar's close.
    """

    current = _utc(now)
    bars = completed_micro_bars(rows, current)
    status = str(provider_status or 'UNAVAILABLE').upper()
    if not bars:
        return MicroBarFreshness(
            None, None, None, current.isoformat(), None, None, None,
            float(allowed_lag_seconds), status, 'UNAVAILABLE',
            'NO_COMPLETED_MICRO_BAR', 'UNAVAILABLE',
        )
    latest = bars[-1]
    began = timestamp(latest['begins_at']).astimezone(timezone.utc)
    interval = float(latest['interval_seconds'])
    closed = began + timedelta(seconds=interval)
    expected_next = closed + timedelta(seconds=interval)
    refresh_due = expected_next + timedelta(seconds=refresh_grace_seconds)
    stale_at = expected_next + timedelta(seconds=allowed_lag_seconds)
    if current < refresh_due:
        freshness_status = 'FRESH'
        reason = 'CURRENT_COMPLETED_BAR_VALID_UNTIL_NEXT_BOUNDARY'
    elif current <= stale_at:
        freshness_status = 'AGING'
        reason = 'NEW_COMPLETED_BAR_DUE_WITHIN_PROVIDER_LAG'
    else:
        freshness_status = 'STALE'
        reason = 'NEW_COMPLETED_BAR_MISSING_BEYOND_ALLOWED_LAG'
    return MicroBarFreshness(
        interval,
        began.isoformat(),
        closed.isoformat(),
        current.isoformat(),
        (current - closed).total_seconds(),
        expected_next.isoformat(),
        refresh_due.isoformat(),
        float(allowed_lag_seconds),
        status,
        freshness_status,
        reason,
        str(latest.get('bar_source') or 'PROVIDER_HISTORY'),
    )
