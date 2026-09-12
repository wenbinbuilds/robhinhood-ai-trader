import pytest

from agent.models import MarketContext, NewsContext
from agent.sector_agent import SectorAgent


MARKET = MarketContext("BULLISH", "BULLISH", "BULLISH", None, 0.8, 0.8)
NO_NEWS = NewsContext(
    "ACME", False, "NONE", "UNKNOWN", 0, None, 0, None, None,
    "NO_MEANINGFUL_CATALYST", 0, 0.5,
)


@pytest.mark.parametrize(
    ("sector", "industry", "expected"),
    [
        ("Technology", "Semiconductors", "TECHNOLOGY_SEMICONDUCTORS"),
        ("Healthcare", "Biotechnology", "HEALTHCARE_BIOTECH"),
        ("Energy", "Oil & Gas", "ENERGY"),
        ("Financials", "Banks", "FINANCIALS"),
        ("Industrials", "Machinery", "GENERAL"),
    ],
)
def test_sector_specializations(sector: str, industry: str, expected: str) -> None:
    result = SectorAgent().analyze(
        "ACME", sector=sector, industry=industry, market=MARKET, news=NO_NEWS
    )
    assert result.sector == expected
