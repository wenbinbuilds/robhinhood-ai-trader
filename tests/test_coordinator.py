from dataclasses import replace

from agent.coordinator import CoordinatorAgent
from agent.models import (
    LlmCandidateAnalysis,
    MarketContext,
    NewsContext,
    SectorContext,
    TechnicalAssessment,
    TechnicalContext,
)


def llm_analysis(
    *,
    sentiment="POSITIVE",
    importance=0.8,
    sector_bias="SUPPORTS",
    market_bias="SUPPORTS",
    direction="LONG",
    setup_quality=0.85,
    catalyst_quality=0.8,
    continuation=0.8,
    conflict=0.1,
):
    return LlmCandidateAnalysis.from_mapping(
        {
            "symbol": "ACME",
            "news_analysis": {
                "status": "AVAILABLE",
                "catalyst_found": True,
                "catalyst_type": "EARNINGS",
                "sentiment": sentiment,
                "importance": importance,
                "freshness_score": 0.9,
                "source_quality_score": 0.9,
                "explains_price_move": True,
                "event_summary": "earnings beat",
                "supporting_events": [],
                "conflicting_events": [],
            },
            "sector_analysis": {
                "sector": "TECHNOLOGY_SEMICONDUCTORS",
                "sector_bias": sector_bias,
                "reasoning": "sector evidence",
                "important_sector_drivers": [],
            },
            "macro_analysis": {
                "market_bias": market_bias,
                "reasoning": "market evidence",
            },
            "qualitative_analysis": {
                "proposed_direction": direction,
                "setup_quality": setup_quality,
                "catalyst_quality": catalyst_quality,
                "continuation_probability_score": continuation,
                "conflicting_evidence_severity": conflict,
                "reasons_for": ["constructive evidence"],
                "reasons_against": [],
                "key_uncertainties": [],
                "summary": "constructive long setup",
            },
        }
    )


def inputs():
    market = MarketContext("BULLISH", "BULLISH", "BULLISH", None, 0.8, 0.85, ("market_up",))
    news = NewsContext("ACME", True, "EARNINGS", "POSITIVE", 0.8, 10, 1, "e1", "earnings", "beat", 0.8, 0.85)
    technical_context = TechnicalContext(
        "ACME", "UPTREND", "ABOVE", "BULLISH", "BULLISH", "BULLISH",
        "CONFIRMED", "CONSTRUCTIVE", 0.9, 0.85, ("price_above_vwap",), (), (),
    )
    plan = {
        "technical_disposition": "QUALIFIED", "entry": 101.0, "stop": 99.0,
        "target": 105.0, "risk_per_share": 2.0, "risk_reward_ratio": 2.0,
        "thesis": "multiple confirmations", "invalidation_condition": "below 99",
    }
    technical = TechnicalAssessment(technical_context, plan)
    sector = SectorContext("ACME", "TECHNOLOGY_SEMICONDUCTORS", "BULLISH", "EARNINGS", 0.8, 0.8, ("sector_up",))
    return market, news, technical, sector


def decide(market, news, technical, sector, risk_result=None, llm=None):
    return CoordinatorAgent().decide(
        "ACME", market=market, news=news, technical=technical,
        sector=sector, risk_result=risk_result,
        llm_analysis=llm or llm_analysis(),
    )


def test_strong_agreement_among_agents() -> None:
    result = decide(*inputs())
    assert result.decision == "TRADE_CANDIDATE"
    assert result.entry == 101.0


def test_technical_bullish_but_news_strongly_negative() -> None:
    market, news, technical, sector = inputs()
    news = replace(news, sentiment="VERY_NEGATIVE", importance=0.9, score=0.0)
    result = decide(
        market, news, technical, sector,
        llm=llm_analysis(sentiment="VERY_NEGATIVE", importance=0.9),
    )
    assert result.decision == "NO_TRADE"
    assert "major negative catalyst" in " ".join(result.reasons_against)


def test_strong_news_but_weak_technical_setup() -> None:
    market, news, technical, sector = inputs()
    technical = replace(technical, context=replace(technical.context, technical_score=0.3, confidence=0.3))
    # Context confirms a setup; it does not manufacture one when the
    # deterministic technical core is weak.
    assert decide(market, news, technical, sector).decision == "NO_TRADE"


def test_mixed_signals_return_watch() -> None:
    market, news, technical, sector = inputs()
    market = replace(market, regime="MIXED", score=0.5)
    news = replace(news, sentiment="NEUTRAL", score=0.5)
    sector = replace(sector, sector_bias="MIXED", score=0.5)
    technical = replace(technical, context=replace(technical.context, technical_score=0.8))
    assert decide(
        market,
        news,
        technical,
        sector,
        llm=llm_analysis(
            sentiment="NEUTRAL",
            sector_bias="NEUTRAL",
            market_bias="NEUTRAL",
            setup_quality=0.6,
            catalyst_quality=0.4,
            continuation=0.5,
            conflict=0.5,
        ),
    ).decision == "WATCH"


def test_insufficient_data_is_no_trade() -> None:
    market, news, technical, sector = inputs()
    technical = replace(
        technical,
        context=replace(technical.context, unavailable_fields=("quote_freshness",)),
    )
    assert decide(market, news, technical, sector).decision == "NO_TRADE"


def test_risk_manager_rejection_vetoes_candidate() -> None:
    result = decide(*inputs(), risk_result={"approved": False, "reasons": ["daily loss limit reached"]})
    assert result.decision == "NO_TRADE"
    assert result.risk_manager_approved is False


def test_short_setup_while_shorting_disabled() -> None:
    market, news, technical, sector = inputs()
    technical = replace(
        technical,
        context=replace(technical.context, trend="DOWNTREND"),
        candidate_plan={**technical.candidate_plan, "direction": "SHORT"},
    )
    assert decide(market, news, technical, sector).decision == "NO_TRADE"


def test_llm_short_proposal_is_deterministically_vetoed() -> None:
    result = decide(*inputs(), llm=llm_analysis(direction="SHORT"))
    assert result.decision == "NO_TRADE"
    assert "short-only LLM proposal" in " ".join(result.vetoes)


def test_missing_llm_reasoning_fails_closed() -> None:
    market, news, technical, sector = inputs()
    result = CoordinatorAgent().decide(
        "ACME", market=market, news=news, technical=technical, sector=sector,
        llm_failure_reason="LLM_REASONING_TIMEOUT",
    )
    assert result.decision == "NO_TRADE"
    assert "LLM_REASONING_UNAVAILABLE" in result.vetoes
    assert result.qualitative_score == 0.5


def test_position_score_semantics_make_watch_and_trade_reachable() -> None:
    market, news, technical, sector = inputs()
    neutral = llm_analysis(
        sentiment="NEUTRAL", importance=0.5,
        sector_bias="NEUTRAL", market_bias="NEUTRAL",
        setup_quality=0.5, catalyst_quality=0.5,
        continuation=0.5, conflict=0.5,
    )

    poor = replace(
        technical, context=replace(technical.context, technical_score=0.3)
    )
    moderate = replace(
        technical, context=replace(technical.context, technical_score=0.65)
    )
    strong = replace(
        technical, context=replace(technical.context, technical_score=0.80)
    )
    complete = replace(
        technical, context=replace(technical.context, technical_score=0.90)
    )

    assert decide(market, news, poor, sector, llm=neutral).decision == "NO_TRADE"
    assert decide(market, news, moderate, sector, llm=neutral).decision == "WATCH"
    assert decide(market, news, strong, sector, llm=neutral).decision == "WATCH"
    assert decide(market, news, complete, sector).decision == "TRADE_CANDIDATE"


def test_position_score_breakdown_sums_exactly_and_rejects_invalid_scales() -> None:
    result = decide(*inputs())
    assert round(sum(
        component["contribution"]
        for component in result.score_breakdown.values()
    ), 3) == result.combined_score
    market, news, technical, sector = inputs()
    bad = replace(technical, context=replace(technical.context, technical_score=1.01))
    import pytest
    with pytest.raises(ValueError, match="technical score"):
        decide(market, news, bad, sector)


def test_extreme_llm_bullishness_cannot_override_technical_veto() -> None:
    market, news, technical, sector = inputs()
    technical = replace(
        technical,
        context=replace(technical.context, unavailable_fields=("quote_freshness",)),
    )
    result = decide(
        market, news, technical, sector,
        llm=llm_analysis(
            setup_quality=1.0, catalyst_quality=1.0,
            continuation=1.0, conflict=0.0,
        ),
    )
    assert result.decision == "NO_TRADE"
    assert "stale or insufficient technical market data" in result.vetoes
