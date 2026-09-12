from datetime import datetime, timezone

from agent.market_calendar import regular_session


def test_regular_weekday_session_is_open():
    assert regular_session(datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc))[:2] == ("OPEN", True)


def test_exchange_holiday_is_closed():
    assert regular_session(datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc))[:2] == ("CLOSED", False)


def test_early_close_is_respected():
    # Day after Thanksgiving, 14:00 New York.
    assert regular_session(datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc))[:2] == ("CLOSED", False)
