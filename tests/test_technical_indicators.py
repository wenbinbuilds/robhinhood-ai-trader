from datetime import datetime, timedelta, timezone

import pytest

from agent.technical_indicators import calculate_indicators, normalize_candles


def candles(count=50):
    start = datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc)
    return [
        {
            "begins_at": (start + timedelta(minutes=5 * index)).isoformat(),
            "open": float(index + 1), "high": float(index + 1.5),
            "low": float(index + 0.5), "close": float(index + 1),
            "volume": 100.0, "interpolated": False,
        }
        for index in range(count)
    ]


def test_deterministic_indicator_fixture():
    result = calculate_indicators(candles())
    assert result["ema9"] == pytest.approx(46.0)
    assert result["ema20"] == pytest.approx(40.5)
    assert result["rsi14"] == pytest.approx(100.0)
    assert result["macd"] == pytest.approx(7.0)
    assert result["macd_signal"] == pytest.approx(7.0)
    assert result["macd_histogram"] == pytest.approx(0.0)
    assert result["vwap"] == pytest.approx(25.5)
    assert result["intraday_low"] == pytest.approx(0.5)
    assert result["intraday_high"] == pytest.approx(50.5)


def test_normalizer_removes_interpolated_malformed_and_duplicate_bars():
    values = candles(3)
    values.append(dict(values[0]))
    values.append({**values[1], "interpolated": True})
    values.append({"begins_at": "invalid"})
    result = normalize_candles(values)
    assert [row["close"] for row in result] == [1.0, 2.0, 3.0]


def test_short_history_marks_long_period_indicators_unavailable():
    result = calculate_indicators(candles(10))
    assert result["ema9"] is not None
    assert result["ema20"] is None
    assert result["rsi14"] is None
    assert result["macd"] is None
