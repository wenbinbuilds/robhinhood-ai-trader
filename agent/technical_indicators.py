"""Deterministic indicators calculated from one normalized candle series.

EMA uses an SMA seed and alpha ``2 / (period + 1)``. RSI uses Wilder's
smoothed average gains/losses. MACD is EMA(12)-EMA(26) with an EMA(9) signal.
VWAP is the session sum of typical-price times volume divided by volume.
Interpolated, malformed, duplicate-timestamp, and non-finite bars are ignored.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import config


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def normalize_candles(
    values: Sequence[Mapping[str, Any]], *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return unique real OHLCV bars in chronological order."""

    rows: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for value in values:
        timestamp = _timestamp(value.get("begins_at"))
        numbers = {name: _finite(value.get(name)) for name in ("open", "high", "low", "close", "volume")}
        if timestamp is None or any(item is None for item in numbers.values()):
            continue
        if bool(value.get("interpolated", False)):
            continue
        if numbers["volume"] < 0 or numbers["low"] > numbers["high"]:
            continue
        key = timestamp.isoformat()
        rows[key] = (timestamp, {
            "begins_at": str(value["begins_at"]),
            **{name: float(number) for name, number in numbers.items()},
            "interpolated": False,
            "interval_seconds": _finite(value.get("interval_seconds")) or 300.0,
            "bar_source": str(value.get("bar_source") or "PROVIDER_HISTORY"),
        })
    ordered = [item[1] for item in sorted(rows.values(), key=lambda item: item[0])]
    return ordered[-limit:] if limit is not None else ordered


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = (values[index] - current) * alpha + current
        result[index] = current
    return result


def rsi14(values: Sequence[float]) -> float | None:
    period = 14
    if len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for index in range(period, len(changes)):
        average_gain = ((period - 1) * average_gain + gains[index]) / period
        average_loss = ((period - 1) * average_loss + losses[index]) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def macd(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    pairs = [
        (index, fast[index] - slow[index])
        for index in range(len(values))
        if fast[index] is not None and slow[index] is not None
    ]
    if not pairs:
        return None, None, None
    macd_values = [value for _, value in pairs]
    signal_values = ema_series(macd_values, 9)
    current = macd_values[-1]
    signal = signal_values[-1]
    return current, signal, current - signal if signal is not None else None


def calculate_indicators(
    values: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    completed_only: bool = False,
) -> dict[str, Any]:
    candles = normalize_candles(values, limit=config.ANALYSIS_CANDLES_TO_RETAIN)
    if completed_only:
        if now is None:
            raise ValueError("now is required when completed_only is true")
        now_utc = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        candles = [
            item for item in candles
            if (_timestamp(item["begins_at"]).astimezone(timezone.utc)
                + timedelta(seconds=item.get("interval_seconds", 300.0))) <= now_utc
        ]
    closes = [item["close"] for item in candles]
    ema9_values = ema_series(closes, 9)
    ema20_values = ema_series(closes, 20)
    macd_value, signal, histogram = macd(closes)

    session_rows: list[dict[str, Any]] = []
    if candles:
        ny = ZoneInfo(config.MARKET_TIMEZONE)
        latest_date = _timestamp(candles[-1]["begins_at"]).astimezone(ny).date()
        session_rows = [
            item for item in candles
            if _timestamp(item["begins_at"]).astimezone(ny).date() == latest_date
        ]
    volume = sum(item["volume"] for item in session_rows)
    vwap = (
        sum(((item["high"] + item["low"] + item["close"]) / 3.0) * item["volume"] for item in session_rows) / volume
        if volume > 0 else None
    )
    return {
        "candles": candles,
        "ema9": ema9_values[-1] if ema9_values else None,
        "ema20": ema20_values[-1] if ema20_values else None,
        "rsi14": rsi14(closes),
        "macd": macd_value,
        "macd_signal": signal,
        "macd_histogram": histogram,
        "vwap": vwap,
        "intraday_high": max((item["high"] for item in session_rows), default=None),
        "intraday_low": min((item["low"] for item in session_rows), default=None),
        "volume": volume if session_rows else None,
    }
