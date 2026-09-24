"""Provider-neutral quote and local exit messages."""
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
import config


def price(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if isfinite(value) and value > 0 else None


def timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class FastQuote:
    symbol: str
    bid: float | None
    ask: float | None
    last_price: float | None
    timestamp: datetime
    source: str
    is_market_open: bool | None
    session_close: datetime | None = None
    received_at: datetime | None = None
    request_started_at: datetime | None = None
    request_finished_at: datetime | None = None
    provider_latency_seconds: float | None = None
    provider_status: str = 'OK'
    cache_hit: bool = False
    cache_key: str | None = None
    cache_created_at: datetime | None = None
    cache_expiry: datetime | None = None
    poll_cycle_id: str | None = None

    @property
    def mid_price(self) -> float | None:
        bid, ask = price(self.bid), price(self.ask)
        if bid and ask and bid <= ask and (ask - bid) / ((bid + ask) / 2) <= config.MAX_SPREAD_PERCENT:
            return (bid + ask) / 2
        return None

    @property
    def mark_price(self) -> float | None:
        return self.mid_price or price(self.last_price)

    @property
    def exit_price(self) -> float | None:
        # Crossed books are invalid; with no usable book the last trade is an
        # explicit lower-quality fallback (recorded on the resulting trade).
        return self.executable_bid or price(self.last_price)

    @property
    def executable_bid(self) -> float | None:
        bid, ask = price(self.bid), price(self.ask)
        return bid if bid and (ask is None or bid <= ask) else None

    def age_at(self, now: datetime) -> float:
        return (now - self.timestamp).total_seconds()

    @property
    def age_seconds(self) -> float:
        return self.age_at(datetime.now(timezone.utc))


@dataclass(frozen=True)
class ExitRequest:
    trade_id: str
    symbol: str
    reason: str
    quote: FastQuote
    monitoring_mode: str
