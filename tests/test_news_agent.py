from datetime import datetime, timedelta, timezone

from agent.news_agent import NewsAgent

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


def article(headline: str, **overrides):
    value = {
        "headline": headline,
        "source": "Example Wire",
        "published_at": (NOW - timedelta(minutes=20)).isoformat(),
        "url": "https://example.test/story",
        "summary": headline,
        "source_quality": "MAJOR_WIRE",
        "importance": 0.8,
    }
    value.update(overrides)
    return value


def test_no_news_found() -> None:
    result = NewsAgent().analyze("ACME", [], now=NOW)
    assert result.catalyst_found is False
    assert result.summary == "NO_MEANINGFUL_CATALYST"


def test_one_meaningful_catalyst() -> None:
    result = NewsAgent().analyze("ACME", [article("ACME wins major customer contract")], now=NOW)
    assert result.catalyst_found is True
    assert result.catalyst_type == "CUSTOMER_DEAL"
    assert result.sentiment in {"POSITIVE", "VERY_POSITIVE"}


def test_stale_news_decays() -> None:
    old = article("ACME launches product", published_at=(NOW - timedelta(days=3)).isoformat())
    fresh = article("ACME launches product", published_at=(NOW - timedelta(minutes=5)).isoformat())
    assert NewsAgent().analyze("ACME", [old], now=NOW).importance < NewsAgent().analyze("ACME", [fresh], now=NOW).importance


def test_news_cache_does_not_freeze_freshness_across_cycles() -> None:
    agent = NewsAgent()
    rows = [article("ACME launches product")]
    first = agent.analyze("ACME", rows, now=NOW)
    later = agent.analyze("ACME", rows, now=NOW + timedelta(hours=6))
    assert later.importance < first.importance


def test_duplicate_articles_form_one_event_cluster() -> None:
    rows = [
        article("ACME earnings beat estimates as revenue grows", source="Wire A"),
        article("ACME earnings beat estimates and revenue grows", source="News B", url="https://example.test/two"),
    ]
    result = NewsAgent().analyze("ACME", rows, now=NOW)
    assert len(result.event_clusters) == 1
    assert result.event_clusters[0].deduplicated_article_count == 1


def test_conflicting_positive_and_negative_events() -> None:
    rows = [
        article("ACME earnings beat estimates", catalyst_type="EARNINGS", sentiment="POSITIVE"),
        article("ACME faces regulatory probe", catalyst_type="REGULATORY", sentiment="NEGATIVE", url="https://example.test/probe"),
    ]
    result = NewsAgent().analyze("ACME", rows, now=NOW)
    assert result.sentiment == "NEUTRAL"
    assert len(result.event_clusters) == 2


def test_low_quality_source_has_lower_importance() -> None:
    low = article("ACME earnings beat", source_quality="LOW_CONFIDENCE")
    high = article("ACME earnings beat", source_quality="REGULATORY_FILING")
    assert NewsAgent().analyze("ACME", [low], now=NOW).importance < NewsAgent().analyze("ACME", [high], now=NOW).importance


def test_high_impact_earnings_event() -> None:
    result = NewsAgent().analyze("ACME", [article("ACME earnings beat and raises guidance")], now=NOW)
    assert result.catalyst_type == "EARNINGS"
    assert result.importance > 0.7


def test_fda_event() -> None:
    result = NewsAgent().analyze("BIO", [article("FDA approves BIO treatment")], now=NOW)
    assert result.catalyst_type == "FDA"


def test_missing_publication_timestamp_is_not_fabricated() -> None:
    result = NewsAgent().analyze("ACME", [article("ACME product launch", published_at=None)], now=NOW)
    assert result.freshness_minutes is None
    assert "publication_timestamp" in result.unavailable_fields
