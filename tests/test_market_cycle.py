import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import config
from agent.market_cycle import JsonSnapshotProvider, MarketCycle, SnapshotValidationError
from agent.models import LlmCandidateAnalysis, LlmReasoningResult, ReasoningTrace
from shadow.portfolio import ShadowPortfolio

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


def bullish_candidate() -> dict[str, Any]:
    candles = []
    for index in range(6):
        opened = 99.0 + index * 0.35
        candles.append(
            {
                "begins_at": f"2026-09-09T14:{30 + index * 5:02d}:00Z",
                "open": opened,
                "high": opened + 0.55,
                "low": opened - 0.20,
                "close": opened + 0.40,
                "volume": 150_000 + index * 10_000,
                "interpolated": False,
            }
        )
    return {
        "current_price": 101.00,
        "bid": 100.98,
        "ask": 101.02,
        "quote_as_of": NOW.isoformat(),
        "volume": 3_500_000,
        "relative_volume": 1.8,
        "vwap": 100.20,
        "ema9": 100.60,
        "ema20": 99.50,
        "rsi14": 61.0,
        "macd": 0.80,
        "macd_signal": 0.45,
        "macd_histogram": 0.35,
        "intraday_support_reference": 98.50,
        "intraday_resistance_reference": 105.00,
        "intraday_low": 97.90,
        "intraday_high": 104.20,
        "candles": candles,
    }


def snapshot(candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "account": {
            "is_agentic_account": True,
            "nickname": "Agentic",
            "account_type": "individual",
        },
        "portfolio": {
            "portfolio_value": "100000",
            "buying_power": "25000",
            "unleveraged_buying_power": "20000",
            "currency": "USD",
        },
        "positions": [],
        "open_orders": [],
        "daily_realized_pnl": "0",
        "market": {"is_regular_session": True, "direction": "BULLISH"},
        "scanner": {
            "criteria": list(config.SCANNER_CRITERIA),
            "candidates": [
                {
                    "symbol": "ACME",
                    "instrument_type": "EQUITY",
                    "columns": {"Volume": "3500000", "% Change": "0.04"},
                }
            ],
        },
        "candidate_data": {"ACME": dict(candidate or bullish_candidate())},
        "warnings": [],
        "errors": [],
    }


class MockRobinhoodDataProvider:
    """A mock with read methods only; deliberately no order operations."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    def get_account(self) -> Mapping[str, Any]:
        self.calls.append("account")
        return self.data["account"]

    def get_portfolio(self) -> Mapping[str, Any]:
        self.calls.append("portfolio")
        return self.data["portfolio"]

    def get_positions(self) -> Sequence[Mapping[str, Any]]:
        self.calls.append("positions")
        return self.data["positions"]

    def get_open_orders(self) -> Sequence[Mapping[str, Any]]:
        self.calls.append("open_orders")
        return self.data["open_orders"]

    def get_daily_realized_pnl(self) -> float | None:
        self.calls.append("daily_realized_pnl")
        return float(self.data["daily_realized_pnl"])

    def get_market_context(self) -> Mapping[str, Any]:
        self.calls.append("market_context")
        return self.data["market"]

    def run_equity_scanner(self) -> Mapping[str, Any]:
        self.calls.append("scanner")
        return self.data["scanner"]

    def get_candidate_data(self, symbol: str) -> Mapping[str, Any]:
        self.calls.append(f"candidate:{symbol}")
        return self.data["candidate_data"][symbol]

    def get_warnings(self) -> Sequence[str]:
        return []

    def get_errors(self) -> Sequence[str]:
        return []


class MockPreExecutionRefresher:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    def refresh_symbol(self, symbol: str, *, now: datetime) -> Mapping[str, Any]:
        self.calls.append(symbol)
        return {
            "symbol": symbol,
            **dict(self.data["candidate_data"][symbol]),
            "quote_as_of": now.isoformat(),
            "market_direction": "BULLISH",
        }


class SupportiveReasoningBridge:
    def __init__(self) -> None:
        self.calls = 0
        self.last_payload = None

    def reason(self, payload, *, expected_symbols, now):
        self.calls += 1
        self.last_payload = payload
        candidates = tuple(
            LlmCandidateAnalysis.from_mapping(
                {
                    "symbol": symbol,
                    "news_analysis": {
                        "status": "UNAVAILABLE",
                        "catalyst_found": False,
                        "catalyst_type": "NONE",
                        "sentiment": "UNKNOWN",
                        "importance": 0.0,
                        "freshness_score": 0.0,
                        "source_quality_score": 0.0,
                        "explains_price_move": None,
                        "event_summary": "NEWS_UNAVAILABLE",
                        "supporting_events": [],
                        "conflicting_events": [],
                    },
                    "sector_analysis": {
                        "sector": "GENERAL",
                        "sector_bias": "SUPPORTS",
                        "reasoning": "supportive sector context",
                        "important_sector_drivers": [],
                    },
                    "macro_analysis": {
                        "market_bias": "SUPPORTS",
                        "reasoning": "supportive market context",
                    },
                    "qualitative_analysis": {
                        "proposed_direction": "LONG",
                        "setup_quality": 0.9,
                        "catalyst_quality": 0.5,
                        "continuation_probability_score": 0.9,
                        "conflicting_evidence_severity": 0.0,
                        "reasons_for": ["strong deterministic technical setup"],
                        "reasons_against": [],
                        "key_uncertainties": ["NEWS_UNAVAILABLE"],
                        "summary": "strong technical momentum without confirmed news",
                    },
                }
            )
            for symbol in expected_symbols
        )
        return LlmReasoningResult(
            status="AVAILABLE",
            candidates=candidates,
            trace=ReasoningTrace(
                reasoning_provider="MOCK",
                model_identifier="mock-model",
                reasoning_invocation_timestamp=now.isoformat(),
                reasoning_duration_seconds=0.01,
                candidate_count=len(candidates),
                schema_version=config.LLM_REASONING_SCHEMA_VERSION,
                prompt_version=config.LLM_REASONING_PROMPT_VERSION,
                status="SUCCESS",
                failure_reason=None,
                token_usage=None,
            ),
        )


def run_cycle(
    tmp_path: Path, data: Mapping[str, Any]
) -> tuple[dict[str, Any], MockRobinhoodDataProvider]:
    provider = MockRobinhoodDataProvider(data)
    cycle = MarketCycle(
        provider,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
        reasoning_bridge=SupportiveReasoningBridge(),
    )
    return cycle.run(), provider


def test_cycle_reads_positions_before_scanning_and_logs_candidate(tmp_path: Path) -> None:
    data = snapshot()
    data["positions"] = [
        {
            "symbol": "HELD",
            "quantity": "1",
            "average_buy_price": "10",
            "current_price": "11",
        }
    ]

    result, provider = run_cycle(tmp_path, data)

    assert provider.calls.index("positions") < provider.calls.index("scanner")
    assert result["position_reviews"][0]["status"] == "ANALYSIS_ONLY_NO_ACTION"
    assert result["decision"]["type"] == "TRADE_CANDIDATE"
    assert result["decision"]["executed"] is False
    assert result["risk_manager_result"][0]["analysis_only"] is True
    assert result["analyzed_candidates"][0]["strategy_score"] == 6.5
    assert result["analyzed_candidates"][0]["max_strategy_score"] == 6.5
    assert result["analyzed_candidates"][0]["market_context"]["regime"] == "BULLISH"
    assert result["analyzed_candidates"][0]["news_context"]["summary"] == "NO_MEANINGFUL_CATALYST"
    assert result["analyzed_candidates"][0]["technical_context"]["technical_score"] == 1.0
    assert result["analyzed_candidates"][0]["sector_context"]["sector"] == "GENERAL"
    assert result["analyzed_candidates"][0]["coordinator_decision"]["decision"] == "TRADE_CANDIDATE"
    assert result["coordinator_configuration"]["weights"] == config.COORDINATOR_WEIGHTS

    log_path = tmp_path / "logs" / "2026-09-09.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["mode"] == config.MODE
    assert logged["decision"]["executed"] is False

    session = json.loads(
        (tmp_path / "state" / "session.json").read_text(encoding="utf-8")
    )
    assert session["cycle_count"] == 1
    assert session["trades_today"] == 0


def test_stale_quote_is_vetoed_as_no_trade(tmp_path: Path) -> None:
    candidate = bullish_candidate()
    candidate["quote_as_of"] = "2026-09-09T14:50:00Z"

    result, _ = run_cycle(tmp_path, snapshot(candidate))

    assert result["decision"]["type"] == "INFRASTRUCTURE_BLOCKED"
    assert result["analyzed_candidates"][0]["decision"] == "NO_TRADE"
    assert "QUOTE_STALE" in " ".join(
        result["analyzed_candidates"][0]["reasons"]
    )


def test_unverified_agentic_account_skips_scanner(tmp_path: Path) -> None:
    data = snapshot()
    data["account"] = {"is_agentic_account": False}

    result, provider = run_cycle(tmp_path, data)

    assert "scanner" not in provider.calls
    assert result["decision"]["type"] == "DATA_ERROR"
    assert "Agentic account was not positively identified" in result["errors"]


def test_existing_symbol_cannot_become_an_additional_entry(tmp_path: Path) -> None:
    data = snapshot()
    data["positions"] = [
        {
            "symbol": "ACME",
            "quantity": "5",
            "average_buy_price": "102",
            "current_price": "101",
        }
    ]

    result, provider = run_cycle(tmp_path, data)

    assert "candidate:ACME" not in provider.calls
    assert result["decision"]["type"] == "NO_TRADE"
    assert "already has an open position" in result["analyzed_candidates"][0][
        "reasons"
    ][0]


def test_closed_market_skips_scanner(tmp_path: Path) -> None:
    data = snapshot()
    data["market"] = {"is_regular_session": False, "direction": "UNKNOWN"}

    result, provider = run_cycle(tmp_path, data)

    assert "scanner" not in provider.calls
    assert result["market_context"]["effective_regular_session"] is False
    assert result["decision"]["type"] == "ANALYSIS_SKIPPED"


def test_closed_market_does_not_invoke_llm_or_fabricate_reasoning(
    tmp_path: Path,
) -> None:
    data = snapshot()
    data["market"] = {"is_regular_session": False, "direction": "UNKNOWN"}
    reasoning = SupportiveReasoningBridge()
    result = MarketCycle(
        MockRobinhoodDataProvider(data),
        reasoning_bridge=reasoning,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
    ).run()

    assert reasoning.calls == 0
    assert result["llm_reasoning"]["trace"]["status"] == "NOT_INVOKED"
    assert result["decision"]["type"] == "ANALYSIS_SKIPPED"


def test_snapshot_rejects_sensitive_fields(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    data = snapshot()
    data["account"] = {
        "is_agentic_account": True,
        "account_number": "sensitive-value",
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="sensitive field"):
        JsonSnapshotProvider.from_path(path)


def test_market_cycle_provider_exposes_no_order_methods() -> None:
    method_names = set(dir(MockRobinhoodDataProvider(snapshot())))
    forbidden_verbs = ("place", "preview", "review", "modify", "cancel", "submit")

    assert not any(any(verb in name for verb in forbidden_verbs) for name in method_names)


def test_shadow_risk_rejection_is_logged_with_reason(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(
        tmp_path / "shadow.json", tmp_path / "shadow_trades.jsonl"
    )
    portfolio.state.trading_date = "2026-09-09"
    portfolio.state.daily_pnl = -config.SHADOW_STARTING_CAPITAL * config.MAX_DAILY_LOSS_PERCENT
    provider = MockRobinhoodDataProvider(snapshot())
    result = MarketCycle(
        provider,
        shadow_portfolio=portfolio,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
        reasoning_bridge=SupportiveReasoningBridge(),
    ).run()

    assert result["decision"]["type"] == "NO_TRADE"
    assert result["rejected_shadow_candidates"][0]["reason"] == "DAILY_LOSS_LIMIT"


def test_production_slow_cycle_never_opens_shadow_entry_directly(tmp_path: Path) -> None:
    portfolio = ShadowPortfolio(
        tmp_path / "shadow.json", tmp_path / "shadow_trades.jsonl"
    )
    result = MarketCycle(
        MockRobinhoodDataProvider(snapshot()),
        shadow_portfolio=portfolio,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
        reasoning_bridge=SupportiveReasoningBridge(),
    ).run()
    assert result["decision"]["type"] == "TRADE_CANDIDATE"
    assert result["new_shadow_positions_opened"] == []
    assert portfolio.snapshot().open_positions == []


def test_open_shadow_scanner_rediscovery_is_context_only(tmp_path: Path, monkeypatch) -> None:
    # Compatibility coverage for the old direct-slow-entry route. Production
    # SHADOW_TRADING now routes new entries through the fast watchlist.
    monkeypatch.setattr(config, "SHADOW_ENTRY_VIA_FAST_WATCHLIST", False)
    portfolio = ShadowPortfolio(
        tmp_path / "shadow.json", tmp_path / "shadow_trades.jsonl"
    )
    data = snapshot()
    refresher = MockPreExecutionRefresher(data)
    first = MarketCycle(
        MockRobinhoodDataProvider(data),
        shadow_portfolio=portfolio,
        pre_execution_refresher=refresher,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
        reasoning_bridge=SupportiveReasoningBridge(),
    ).run()
    assert first["new_shadow_positions_opened"]

    second = MarketCycle(
        MockRobinhoodDataProvider(data),
        shadow_portfolio=portfolio,
        pre_execution_refresher=refresher,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
        reasoning_bridge=SupportiveReasoningBridge(),
    ).run()
    assert second["decision"]["type"] == "NO_TRADE"
    assert second["rejected_shadow_candidates"] == []
    assert second["analyzed_candidates"][0]["decision"] == "POSITION_CONTEXT_UPDATED"
    assert second["analyzed_candidates"][0]["coordinator_decision"] is not None
    assert len(portfolio.snapshot().open_positions) == 1


def test_one_reasoning_invocation_handles_multiple_candidates(tmp_path: Path) -> None:
    data = snapshot()
    data["scanner"]["candidates"].append(
        {
            "symbol": "BETA",
            "instrument_type": "EQUITY",
            "columns": {"Volume": "5000000", "% Change": "0.09"},
        }
    )
    data["candidate_data"]["BETA"] = bullish_candidate()
    reasoning = SupportiveReasoningBridge()
    result = MarketCycle(
        MockRobinhoodDataProvider(data),
        reasoning_bridge=reasoning,
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
    ).run()

    assert reasoning.calls == 1
    assert [
        item["symbol"] for item in reasoning.last_payload["candidates"]
    ] == ["BETA", "ACME"]
    assert result["llm_reasoning"]["trace"]["candidate_count"] == 2


def test_unavailable_reasoning_fails_all_candidates_to_no_trade(
    tmp_path: Path,
) -> None:
    result = MarketCycle(
        MockRobinhoodDataProvider(snapshot()),
        state_path=tmp_path / "state" / "session.json",
        logs_dir=tmp_path / "logs",
        clock=lambda: NOW,
    ).run()

    assert result["decision"]["type"] == "INFRASTRUCTURE_BLOCKED"
    assert result["risk_manager_result"] == []
    assert "LLM_REASONING_UNAVAILABLE" in result["analyzed_candidates"][0][
        "coordinator_vetoes"
    ]
