"""Immutable messages across research, setup, geometry and execution boundaries."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Quality(str, Enum):
    FRESH = 'FRESH'
    STALE = 'STALE'
    UNAVAILABLE = 'UNAVAILABLE'
    INVALID = 'INVALID'


@dataclass(frozen=True)
class ComponentProvenance:
    component: str
    status: str = 'UNKNOWN'
    source: str | None = None
    source_timestamp: str | None = None
    fallback_used: bool | None = None
    cache_hit: bool | None = None
    context: str | None = None


@dataclass(frozen=True)
class AlphaSnapshot:
    episode_id: str
    symbol: str
    timestamp: str
    technical_score: float | None
    news_score: float | None
    sector_score: float | None
    market_score: float | None
    qualitative_score: float | None
    slow_score: float | None
    live_score: float | None = None
    dynamic_score: float | None = None
    slow_weight: float | None = None
    live_weight: float | None = None
    provenance: tuple[ComponentProvenance, ...] = ()

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SetupEpisode:
    episode_id: str
    symbol: str
    created_at: str
    research_cycle_id: str
    research_entry: float
    slow_score: float
    state: str
    structural_context: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class GeometryDecision:
    episode_id: str
    symbol: str
    timestamp: str
    entry: float | None
    support: float | None
    resistance: float | None
    stop: float | None
    target: float | None
    risk: float | None
    reward: float | None
    risk_reward_ratio: float | None
    entry_drift: float | None
    research_rr: float | None
    live_rr: float | None
    valid: bool
    rejection_reasons: tuple[str, ...] = ()
    structural_evidence: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    episode_id: str
    symbol: str
    approved: bool
    requested_size: int | None
    approved_size: int
    risk_dollars: float
    risk_percent: float | None
    daily_loss_state: str
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioDecision:
    episode_id: str
    symbol: str
    approved: bool
    final_size: int
    reasons: tuple[str, ...] = ()
    duplicate_position_check: str = 'NOT_EVALUATED'
    exposure_check: str = 'NOT_EVALUATED'


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    quote_timestamp: str | None
    quote_age: float | None
    spread: float | None
    provider: str | None
    quote_status: Quality
    candle_status: Quality = Quality.UNAVAILABLE
    candle_timestamp: str | None = None
    completed_candles: tuple = ()

    @classmethod
    def from_quote(cls, quote, *, symbol, now, max_age):
        if quote is None:
            return cls(symbol, None, None, None, None, None, None, None, Quality.UNAVAILABLE)
        try:
            age = quote.age_at(now)
            valid = quote.symbol == symbol and quote.timestamp.tzinfo is not None and quote.mark_price is not None
            status = Quality.INVALID if not valid or age < 0 else Quality.STALE if age > max_age else Quality.FRESH
            bid, ask = quote.bid, quote.ask
            spread = (ask-bid)/((ask+bid)/2) if bid is not None and ask is not None and ask+bid > 0 else None
            return cls(symbol, bid, ask, quote.mark_price, quote.timestamp.isoformat(), age, spread, quote.source, status)
        except (TypeError, ValueError, AttributeError):
            return cls(symbol, None, None, None, None, None, None, None, Quality.INVALID)

    @classmethod
    def from_mapping(cls, data, *, now, max_age):
        from watcher.models import FastQuote, timestamp, price
        at = timestamp(data.get('quote_as_of'))
        quote = FastQuote(str(data.get('symbol', '')), price(data.get('bid')), price(data.get('ask')),
                          price(data.get('current_price')), at, str(data.get('provider', 'NORMALIZED')), None) if at else None
        result = cls.from_quote(quote, symbol=str(data.get('symbol', '')), now=now, max_age=max_age)
        from dataclasses import replace
        from agent.candidate_analyzer import Candle, completed_candles
        rows = data.get('candles') or []
        bars = tuple(completed_candles(tuple(bar for row in rows if isinstance(row, dict)
                     and not row.get('is_forming') and (bar := Candle.from_mapping(row)) is not None), now))
        latest = timestamp(bars[-1].begins_at) if bars else None
        status = Quality.UNAVAILABLE if latest is None else Quality.STALE if (now-latest).total_seconds() >= 600 else Quality.FRESH
        return replace(result, completed_candles=bars, candle_timestamp=latest.isoformat() if latest else None, candle_status=status)
