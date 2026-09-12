"""Single sector-aware evidence component with conservative generic fallback."""

from __future__ import annotations

from typing import Any, Mapping

from agent.models import MarketContext, NewsContext, SectorContext


def _number(value: Any) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


class SectorAgent:
    def analyze(
        self,
        symbol: str,
        *,
        sector: str | None,
        industry: str | None = None,
        benchmark: Mapping[str, Any] | None = None,
        market: MarketContext,
        news: NewsContext,
    ) -> SectorContext:
        raw = f"{sector or ''} {industry or ''}".upper()
        if "SEMICONDUCT" in raw or (sector or "").upper() == "TECHNOLOGY":
            specialization = "TECHNOLOGY_SEMICONDUCTORS"
        elif "BIOTECH" in raw or "HEALTHCARE" in raw:
            specialization = "HEALTHCARE_BIOTECH"
        elif "ENERGY" in raw:
            specialization = "ENERGY"
        elif "FINANCIAL" in raw:
            specialization = "FINANCIALS"
        else:
            specialization = "GENERAL"
        unavailable: list[str] = []
        evidence: list[str] = [f"sector_model_{specialization.lower()}"]
        positive = negative = 0
        if benchmark:
            comparisons = (
                (_number(benchmark.get("current_price")), _number(benchmark.get("vwap"))),
                (_number(benchmark.get("ema9")), _number(benchmark.get("ema20"))),
                (_number(benchmark.get("intraday_change_percent")), 0.0),
            )
            for left, right in comparisons:
                if left is None or right is None or left == right:
                    continue
                if left > right:
                    positive += 1
                else:
                    negative += 1
            if positive:
                evidence.append("sector_benchmark_bullish_signals")
            if negative:
                evidence.append("sector_benchmark_bearish_signals")
        else:
            unavailable.append("sector_benchmark")

        if positive > negative:
            bias = "BULLISH"
            score = positive / (positive + negative)
        elif negative > positive:
            bias = "BEARISH"
            score = positive / (positive + negative)
        elif positive + negative:
            bias = "MIXED"
            score = 0.5
        elif market.regime in {"BULLISH", "BEARISH", "MIXED"}:
            bias = market.regime
            score = market.score
            evidence.append("broad_market_proxy_used")
        else:
            bias = "UNKNOWN"
            score = 0.5

        driver = None
        if news.catalyst_found and news.catalyst_type in {
            "FDA", "CLINICAL_TRIAL", "REGULATORY", "MACRO", "SECTOR",
            "PRODUCT", "CUSTOMER_DEAL", "EARNINGS", "GUIDANCE",
        }:
            driver = news.catalyst_type
            evidence.append(f"sector_relevant_catalyst_{driver.lower()}")
        if not sector:
            unavailable.append("sector")
        return SectorContext(
            symbol=symbol.upper(), sector=specialization, sector_bias=bias,
            sector_driver=driver,
            confidence=round(min(1.0, (positive + negative) / 3), 3),
            score=round(max(0.0, min(1.0, score)), 3),
            evidence=tuple(evidence), unavailable_fields=tuple(unavailable),
        )
