from agent.macro_agent import MacroAgent


def benchmark(symbol: str, bullish: bool):
    return {
        "symbol": symbol,
        "current_price": 101 if bullish else 99,
        "vwap": 100,
        "ema9": 101 if bullish else 99,
        "ema20": 100,
        "intraday_change_percent": 0.02 if bullish else -0.02,
        "candles": [],
    }


def test_bullish_spy_qqq() -> None:
    result = MacroAgent().analyze({"benchmarks": [benchmark("SPY", True), benchmark("QQQ", True)]})
    assert result.regime == "BULLISH"


def test_bearish_spy_qqq() -> None:
    result = MacroAgent().analyze({"benchmarks": [benchmark("SPY", False), benchmark("QQQ", False)]})
    assert result.regime == "BEARISH"


def test_mixed_market() -> None:
    result = MacroAgent().analyze({"benchmarks": [benchmark("SPY", True), benchmark("QQQ", False)]})
    assert result.regime == "MIXED"


def test_missing_market_data() -> None:
    result = MacroAgent().analyze({"benchmarks": [], "direction": "UNKNOWN"})
    assert result.regime == "UNKNOWN"
    assert result.confidence == 0
