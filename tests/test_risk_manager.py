from decimal import Decimal

from risk.risk_manager import RiskLimits, RiskManager, RiskRequest


def request(**overrides: object) -> RiskRequest:
    values: dict[str, object] = {
        "account_equity": "100000",
        "entry_price": "100",
        "stop_price": "98",
        "daily_realized_pnl": "0",
        "open_positions": 0,
        "trades_today": 0,
        "available_buying_power": "20000",
    }
    values.update(overrides)
    return RiskRequest(**values)  # type: ignore[arg-type]


def test_valid_position_sizing() -> None:
    result = RiskManager().evaluate(request())

    assert result.approved is True
    assert result.max_position_dollars == Decimal("5000.00")
    assert result.max_risk_dollars == Decimal("500.000")
    assert result.risk_per_share == Decimal("2")
    assert result.max_shares == 50
    assert result.theoretical_position_value == Decimal("5000")
    assert result.theoretical_dollar_risk == Decimal("100")
    assert result.binding_limit == "position_percent"


def test_invalid_stop_is_rejected() -> None:
    result = RiskManager().evaluate(request(stop_price="100"))

    assert result.approved is False
    assert "stop price must be below entry price for a long trade" in result.reasons
    assert result.max_shares == 0


def test_requested_position_over_position_size_limit_is_rejected() -> None:
    result = RiskManager().evaluate(
        request(stop_price="99.50", requested_shares=51)
    )

    assert result.approved is False
    assert "requested position exceeds maximum position size" in result.reasons
    assert "requested risk exceeds maximum risk per trade" not in result.reasons


def test_requested_position_over_risk_per_trade_limit_is_rejected() -> None:
    limits = RiskLimits(
        max_position_percent=Decimal("0.50"),
        max_risk_per_trade_percent=Decimal("0.005"),
        max_daily_loss_percent=Decimal("0.02"),
        max_simultaneous_positions=2,
        max_trades_per_day=5,
    )
    result = RiskManager(limits).evaluate(request(requested_shares=300))

    assert result.approved is False
    assert "requested risk exceeds maximum risk per trade" in result.reasons
    assert "requested position exceeds maximum position size" not in result.reasons


def test_daily_loss_shutdown() -> None:
    result = RiskManager().evaluate(request(daily_realized_pnl="-2000"))

    assert result.approved is False
    assert "daily loss limit reached" in result.reasons


def test_maximum_open_positions() -> None:
    result = RiskManager().evaluate(request(open_positions=2))

    assert result.approved is False
    assert "maximum simultaneous positions reached" in result.reasons


def test_maximum_trades_per_day() -> None:
    result = RiskManager().evaluate(request(trades_today=5))

    assert result.approved is False
    assert "maximum trades per day reached" in result.reasons


def test_unavailable_daily_pnl_fails_closed() -> None:
    result = RiskManager().evaluate(request(daily_realized_pnl=None))

    assert result.approved is False
    assert "today's realized P&L is unavailable" in result.reasons
