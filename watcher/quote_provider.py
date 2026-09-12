"""Safe quote boundary for snapshots and direct factual Robinhood reads."""
import json
import time
from datetime import datetime, time as datetime_time, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Mapping, Protocol, Sequence

import config
from agent.market_calendar import early_close, regular_session
from robinhood_mcp.client import DirectRobinhoodMcpClient
from robinhood_mcp.normalization import normalized_quotes
from watcher.models import FastQuote, price, timestamp


class FastQuoteProvider(Protocol):
    name: str
    mode: str

    def get_quotes(self, symbols: Sequence[str]) -> Mapping[str, FastQuote]:
        """One bounded batch; future network adapters MUST enforce IO timeouts.

        Timestamps must be source quote times, never poll times. No model calls,
        order tools, credential extraction, or background mutation threads.
        """
        ...


class SnapshotQuoteProvider:
    name = "PERIODIC_ROBINHOOD_SNAPSHOT"
    mode = "DEGRADED_SNAPSHOT"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get_quotes(self, symbols: Sequence[str]) -> Mapping[str, FastQuote]:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("data_source") != "ROBINHOOD_MCP"
                or value.get("mcp_status") != "CONNECTED"
                or timestamp(value.get("generated_at")) is None):
            raise ValueError("invalid Robinhood snapshot")
        market = value.get("market", {})
        if not isinstance(market, dict):
            raise ValueError("invalid market state")
        is_open = market.get("is_regular_session")
        if not isinstance(is_open, bool):
            is_open = None
        result = {}
        # Shadow-specific rows take precedence over scanner candidates.
        for section in ("candidate_data", "shadow_position_data"):
            rows = value.get(section, [])
            if not isinstance(rows, list):
                raise ValueError("invalid quote rows")
            for row in rows:
                if not isinstance(row, dict) or row.get("symbol") not in symbols:
                    continue
                at = timestamp(row.get("quote_as_of"))
                if at is None:
                    continue
                quote = FastQuote(
                    symbol=row["symbol"], bid=price(row.get("bid")), ask=price(row.get("ask")),
                    last_price=price(row.get("current_price")), timestamp=at,
                    source=self.name, is_market_open=is_open,
                    session_close=timestamp(market.get("closes_at")),
                )
                old = result.get(quote.symbol)
                if old is None or old.timestamp <= at:
                    result[quote.symbol] = quote
        return result


class RobinhoodDirectQuoteProvider:
    """One batched direct MCP quote call; never invokes an LLM or order tool.

    Until real measurements establish stable sub-two-second requests and fresh
    source timestamps, the provider reports an explicitly unvalidated mode so
    the watcher remains conservative.
    """

    name = "DIRECT_ROBINHOOD_MCP"
    mode = "DIRECT_MCP_UNVALIDATED"

    def __init__(self, client: DirectRobinhoodMcpClient, *, clock=None) -> None:
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.request_count = 0
        self.failure_count = 0
        self.total_latency = 0.0
        self.max_latency = 0.0
        self.total_quote_age = 0.0
        self.quote_age_count = 0

    @property
    def metrics(self) -> dict[str, float | int | bool | None]:
        average_latency = self.total_latency / self.request_count if self.request_count else None
        average_age = self.total_quote_age / self.quote_age_count if self.quote_age_count else None
        failure_rate = self.failure_count / self.request_count if self.request_count else None
        suitable = bool(
            self.request_count >= 3
            and failure_rate == 0
            and average_latency is not None
            and average_latency <= config.FAST_QUOTE_INTERVAL_SECONDS
            and average_age is not None
            and average_age <= config.FAST_QUOTE_MAX_AGE_SECONDS
        )
        return {
            "request_count": self.request_count,
            "failure_count": self.failure_count,
            "failure_rate": failure_rate,
            "average_latency_seconds": average_latency,
            "max_latency_seconds": self.max_latency if self.request_count else None,
            "average_quote_age_seconds": average_age,
            "suitable_for_configured_fast_watcher": suitable,
        }

    def get_quotes(self, symbols: Sequence[str]) -> Mapping[str, FastQuote]:
        started = time.monotonic()
        self.request_count += 1
        try:
            call = self.client.get_quotes(symbols)
            received = self.clock()
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            raw = normalized_quotes(call.value, symbols, retrieved_at=received)
            _status, is_open, _source = regular_session(received)
            local = received.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
            close_time = early_close(local.date()) or datetime_time(*config.REGULAR_MARKET_CLOSE)
            session_close = datetime.combine(local.date(), close_time, tzinfo=local.tzinfo).astimezone(timezone.utc)
            result: dict[str, FastQuote] = {}
            for symbol, row in raw.items():
                source_at = timestamp(row.get("quote_as_of"))
                if source_at is None:
                    continue
                age = (received.astimezone(timezone.utc) - source_at.astimezone(timezone.utc)).total_seconds()
                if age >= 0:
                    self.total_quote_age += age
                    self.quote_age_count += 1
                result[symbol] = FastQuote(
                    symbol=symbol,
                    bid=price(row.get("bid")),
                    ask=price(row.get("ask")),
                    last_price=price(row.get("current_price")),
                    timestamp=source_at,
                    source=self.name,
                    is_market_open=is_open,
                    session_close=session_close,
                )
            return result
        except Exception:
            self.failure_count += 1
            raise
        finally:
            elapsed = time.monotonic() - started
            self.total_latency += elapsed
            self.max_latency = max(self.max_latency, elapsed)
