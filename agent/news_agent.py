"""Deterministic catalyst classification, freshness decay, and deduplication."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import config
from agent.models import NewsContext, NewsEventCluster

CATALYST_KEYWORDS = {
    "EARNINGS": ("earnings", "quarterly results", "eps", "revenue"),
    "GUIDANCE": ("guidance", "forecast", "outlook"),
    "FDA": ("fda", "food and drug administration", "approval", "rejection"),
    "CLINICAL_TRIAL": ("clinical trial", "phase 1", "phase 2", "phase 3", "endpoint"),
    "M_AND_A": ("acquire", "acquisition", "merger", "takeover", "buyout"),
    "SEC_FILING": ("8-k", "10-k", "10-q", "sec filing", "securities and exchange"),
    "PRODUCT": ("launch", "product", "unveil"),
    "CUSTOMER_DEAL": ("contract", "customer deal", "partnership", "agreement"),
    "ANALYST_ACTION": ("upgrade", "downgrade", "price target", "analyst"),
    "LEGAL": ("lawsuit", "litigation", "court", "settlement"),
    "REGULATORY": ("regulator", "regulatory", "probe", "investigation"),
    "MACRO": ("federal reserve", "inflation", "jobs report", "interest rate"),
    "SECTOR": ("sector", "industry", "peers"),
    "MANAGEMENT": ("ceo", "cfo", "resign", "appoint"),
}

POSITIVE_WORDS = {
    "beat", "beats", "growth", "approval", "approved", "upgrade", "raises",
    "record", "strong", "surge", "wins", "positive", "successful",
}
NEGATIVE_WORDS = {
    "miss", "misses", "cut", "cuts", "rejection", "rejected", "downgrade",
    "lawsuit", "probe", "decline", "weak", "loss", "recall", "failed",
}
MAJOR_CATALYSTS = {"EARNINGS", "GUIDANCE", "FDA", "CLINICAL_TRIAL", "M_AND_A"}
QUALITY_WEIGHT = {
    "PRIMARY_COMPANY_SOURCE": 1.0,
    "REGULATORY_FILING": 1.0,
    "MAJOR_WIRE": 0.9,
    "MAJOR_FINANCIAL_NEWS": 0.8,
    "OTHER_REPUTABLE": 0.6,
    "LOW_CONFIDENCE": 0.25,
}
SENTIMENT_VALUE = {
    "VERY_POSITIVE": 1.0,
    "POSITIVE": 0.6,
    "NEUTRAL": 0.0,
    "NEGATIVE": -0.6,
    "VERY_NEGATIVE": -1.0,
    "UNKNOWN": 0.0,
}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(timezone.utc)


def _tokens(text: str) -> set[str]:
    ignored = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "at", "says"}
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 2 and word not in ignored}


class NewsAgent:
    """Turn raw public-news rows into deduplicated event evidence."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], NewsContext] = {}

    @staticmethod
    def _catalyst(article: Mapping[str, Any]) -> str:
        supplied = str(article.get("catalyst_type", "")).upper()
        if supplied in {*CATALYST_KEYWORDS, "OTHER", "NONE"}:
            return supplied
        text = f"{article.get('headline', '')} {article.get('summary', '')}".lower()
        for catalyst, words in CATALYST_KEYWORDS.items():
            if any(word in text for word in words):
                return catalyst
        return "OTHER" if text.strip() else "NONE"

    @staticmethod
    def _sentiment(article: Mapping[str, Any]) -> str:
        supplied = str(article.get("sentiment", "")).upper()
        if supplied in SENTIMENT_VALUE:
            return supplied
        words = _tokens(f"{article.get('headline', '')} {article.get('summary', '')}")
        positive = len(words & POSITIVE_WORDS)
        negative = len(words & NEGATIVE_WORDS)
        delta = positive - negative
        if delta >= 2:
            return "VERY_POSITIVE"
        if delta == 1:
            return "POSITIVE"
        if delta <= -2:
            return "VERY_NEGATIVE"
        if delta == -1:
            return "NEGATIVE"
        return "NEUTRAL"

    @staticmethod
    def _quality(article: Mapping[str, Any]) -> str:
        value = str(article.get("source_quality", "OTHER_REPUTABLE")).upper()
        return value if value in QUALITY_WEIGHT else "LOW_CONFIDENCE"

    @staticmethod
    def _similar(left: set[str], right: set[str]) -> bool:
        union = left | right
        return bool(union) and len(left & right) / len(union) >= 0.45

    def analyze(
        self,
        symbol: str,
        articles: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> NewsContext:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cache_material = repr(sorted((str(item.get("headline")), str(item.get("published_at"))) for item in articles))
        # Cache identical evidence only for this analysis timestamp. Reusing a
        # result in a later cycle would freeze deterministic freshness decay.
        cache_key = (symbol.upper(), cache_material, current.isoformat())
        if cache_key in self._cache:
            return self._cache[cache_key]
        if not articles:
            result = NewsContext(
                symbol=symbol.upper(), catalyst_found=False, catalyst_type="NONE",
                sentiment="UNKNOWN", importance=0.0, freshness_minutes=None,
                source_count=0, event_id=None, event_cluster=None,
                summary="NO_MEANINGFUL_CATALYST", confidence=0.0, score=0.5,
                unavailable_fields=("news",),
            )
            self._cache[cache_key] = result
            return result

        grouped: list[dict[str, Any]] = []
        missing_timestamp = False
        for article in articles:
            headline = str(article.get("headline", "")).strip()
            if not headline:
                continue
            catalyst = self._catalyst(article)
            token_set = _tokens(f"{symbol} {headline} {article.get('summary', '')}")
            group = next(
                (
                    item for item in grouped
                    if item["catalyst"] == catalyst and self._similar(item["tokens"], token_set)
                ),
                None,
            )
            if group is None:
                group = {"catalyst": catalyst, "tokens": token_set, "articles": []}
                grouped.append(group)
            group["articles"].append(article)

        clusters: list[NewsEventCluster] = []
        weighted_values: list[tuple[float, float]] = []
        for group in grouped:
            rows = group["articles"]
            times = [_parse_time(row.get("published_at")) for row in rows]
            if any(item is None for item in times):
                missing_timestamp = True
            valid_times = [item for item in times if item is not None]
            freshness = min(max(0.0, (current - item).total_seconds() / 60) for item in valid_times) if valid_times else None
            half_life = (
                config.NEWS_MAJOR_EVENT_HALF_LIFE_MINUTES
                if group["catalyst"] in MAJOR_CATALYSTS
                else config.NEWS_DEFAULT_HALF_LIFE_MINUTES
            )
            decay = 0.25 if freshness is None else math.exp(-math.log(2) * freshness / half_life)
            maximum_age = config.NEWS_MAX_AGE_MINUTES * (
                2 if group["catalyst"] in MAJOR_CATALYSTS else 1
            )
            if freshness is not None and freshness > maximum_age:
                decay = 0.0
            qualities = [self._quality(row) for row in rows]
            best_quality = max(qualities, key=lambda value: QUALITY_WEIGHT[value])
            sentiments = [self._sentiment(row) for row in rows]
            sentiment_value = sum(SENTIMENT_VALUE[item] for item in sentiments) / len(sentiments)
            if sentiment_value >= 0.8:
                sentiment = "VERY_POSITIVE"
            elif sentiment_value > 0.15:
                sentiment = "POSITIVE"
            elif sentiment_value <= -0.8:
                sentiment = "VERY_NEGATIVE"
            elif sentiment_value < -0.15:
                sentiment = "NEGATIVE"
            else:
                sentiment = "NEUTRAL"
            supplied_importance = [
                float(row["importance"])
                for row in rows
                if isinstance(row.get("importance"), (int, float))
            ]
            base_importance = max(supplied_importance, default=0.85 if group["catalyst"] in MAJOR_CATALYSTS else 0.6)
            importance = max(0.0, min(1.0, base_importance * QUALITY_WEIGHT[best_quality] * decay))
            canonical = " ".join(sorted(group["tokens"])) + group["catalyst"] + symbol.upper()
            event_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            safe_sources = tuple(
                {
                    "headline": str(row.get("headline", "")),
                    "source": str(row.get("source", row.get("publisher", "UNKNOWN"))),
                    "published_at": row.get("published_at"),
                    "url": row.get("url"),
                    "source_quality": self._quality(row),
                }
                for row in rows
            )
            cluster = NewsEventCluster(
                event_id=event_id, catalyst_type=group["catalyst"], sentiment=sentiment,
                importance=round(importance, 3), freshness_minutes=None if freshness is None else round(freshness, 1),
                source_count=len(rows), source_quality=best_quality,
                summary=str(rows[0].get("summary") or rows[0].get("headline")),
                sources=safe_sources, deduplicated_article_count=max(0, len(rows) - 1),
            )
            clusters.append(cluster)
            weighted_values.append((sentiment_value, max(0.05, importance)))

        if not clusters:
            return self.analyze(symbol, [], now=current)
        total_weight = sum(weight for _, weight in weighted_values)
        net = sum(value * weight for value, weight in weighted_values) / total_weight
        sentiment = (
            "VERY_POSITIVE" if net >= 0.8 else "POSITIVE" if net > 0.15
            else "VERY_NEGATIVE" if net <= -0.8 else "NEGATIVE" if net < -0.15
            else "NEUTRAL"
        )
        best = max(clusters, key=lambda item: item.importance)
        meaningful = best.catalyst_type not in {"NONE", "OTHER"} and best.importance >= 0.1
        result = NewsContext(
            symbol=symbol.upper(), catalyst_found=meaningful,
            catalyst_type=best.catalyst_type, sentiment=sentiment,
            importance=best.importance, freshness_minutes=best.freshness_minutes,
            source_count=sum(item.source_count for item in clusters), event_id=best.event_id,
            event_cluster=best.summary,
            summary=best.summary if meaningful else "NO_MEANINGFUL_CATALYST",
            confidence=round(min(1.0, total_weight / max(1, len(clusters))), 3),
            score=round(max(0.0, min(1.0, (net + 1) / 2)), 3),
            sources=tuple(source for cluster in clusters for source in cluster.sources),
            event_clusters=tuple(clusters),
            unavailable_fields=("publication_timestamp",) if missing_timestamp else (),
        )
        self._cache[cache_key] = result
        return result
