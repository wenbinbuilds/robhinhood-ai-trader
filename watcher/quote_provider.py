"""Safe quote boundary for snapshots and direct factual Robinhood reads."""
import json
import time
from statistics import median
from datetime import datetime, timedelta, time as datetime_time, timezone
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

    def get_quotes(self, symbols: Sequence[str], *, poll_cycle_id=None) -> Mapping[str, FastQuote]:
        request_started = datetime.now(timezone.utc)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        request_finished = datetime.now(timezone.utc)
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
        generated_at = timestamp(value.get('generated_at'))
        # Shadow-specific rows take precedence over scanner candidates.
        for section in ("candidate_data", "scalp_candidate_data", "shadow_position_data"):
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
                    received_at=request_finished,
                    request_started_at=request_started,
                    request_finished_at=request_finished,
                    provider_latency_seconds=(request_finished-request_started).total_seconds(),
                    provider_status='OK', cache_hit=True,
                    cache_key=str(self.path.resolve()),
                    cache_created_at=generated_at,
                    cache_expiry=(
                        generated_at + timedelta(seconds=config.SNAPSHOT_MAX_AGE_SECONDS)
                        if generated_at else None
                    ),
                    poll_cycle_id=poll_cycle_id,
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
    metrics_sample_limit = 25_000

    def __init__(self, client: DirectRobinhoodMcpClient, *, clock=None) -> None:
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.request_count = 0
        self.failure_count = 0
        self.total_latency = 0.0
        self.max_latency = 0.0
        self.total_quote_age = 0.0
        self.quote_age_count = 0
        self._latency_samples: list[float] = []
        self._quote_age_samples: list[float] = []
        self.last_request_trace: dict[str, dict[str, object]] = {}

    @staticmethod
    def _percentile(values: Sequence[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = (len(ordered) - 1) * percentile
        low, high = int(index), min(int(index) + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)

    def _sample(self, values: list[float], item: float) -> None:
        values.append(item)
        if len(values) > self.metrics_sample_limit:
            del values[: len(values) - self.metrics_sample_limit]

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
            "median_latency_seconds": median(self._latency_samples) if self._latency_samples else None,
            "p95_latency_seconds": self._percentile(self._latency_samples, 0.95),
            "max_latency_seconds": self.max_latency if self.request_count else None,
            "average_quote_age_seconds": average_age,
            "median_quote_age_seconds": median(self._quote_age_samples) if self._quote_age_samples else None,
            "p95_quote_age_seconds": self._percentile(self._quote_age_samples, 0.95),
            "suitable_for_configured_fast_watcher": suitable,
        }

    def get_quotes(self, symbols: Sequence[str], *, poll_cycle_id=None) -> Mapping[str, FastQuote]:
        request_started_at = self.clock()
        if request_started_at.tzinfo is None:
            request_started_at = request_started_at.replace(tzinfo=timezone.utc)
        started = time.monotonic()
        self.request_count += 1
        try:
            call = self.client.get_quotes(symbols)
            received = self.clock()
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            raw = normalized_quotes(call.value, symbols, retrieved_at=received)
            provider_latency = time.monotonic() - started
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
                    self._sample(self._quote_age_samples, age)
                result[symbol] = FastQuote(
                    symbol=symbol,
                    bid=price(row.get("bid")),
                    ask=price(row.get("ask")),
                    last_price=price(row.get("current_price")),
                    timestamp=source_at,
                    source=self.name,
                    is_market_open=is_open,
                    session_close=session_close,
                    received_at=received,
                    request_started_at=request_started_at,
                    request_finished_at=received,
                    provider_latency_seconds=provider_latency,
                    provider_status='OK', cache_hit=False,
                    cache_key=None, cache_created_at=None, cache_expiry=None,
                    poll_cycle_id=poll_cycle_id,
                )
            self.last_request_trace = {
                symbol: quote_provenance(
                    result.get(symbol), symbol=symbol,
                    evaluation_at=received, maximum_age_seconds=None,
                    provider_status=('OK' if symbol in result else 'NO_QUOTE'),
                    poll_cycle_id=poll_cycle_id,
                )
                for symbol in symbols
            }
            return result
        except Exception:
            self.failure_count += 1
            raise
        finally:
            elapsed = time.monotonic() - started
            self.total_latency += elapsed
            self.max_latency = max(self.max_latency, elapsed)
            self._sample(self._latency_samples, elapsed)


def quote_provenance(
    quote: FastQuote | None, *, symbol: str, evaluation_at: datetime,
    maximum_age_seconds: float | None, provider_status: str,
    poll_cycle_id: str | None = None, previous_exchange_timestamp: datetime | None = None,
    requested_this_cycle: bool = True,
) -> dict[str, object]:
    """Explain quote age without treating provider success as freshness."""

    evaluated = evaluation_at.astimezone(timezone.utc)
    if quote is None:
        return {
            'symbol': symbol.upper(), 'poll_cycle_id': poll_cycle_id,
            'request_started_at': None, 'request_finished_at': None,
            'provider_latency_ms': None,
            'provider_status': provider_status,
            'exchange_timestamp': None, 'received_timestamp': None,
            'evaluation_timestamp': evaluated.isoformat(),
            'exchange_quote_age_seconds': None,
            'exchange_quote_age_at_receive_seconds': None,
            'local_cache_age_seconds': None,
            'stale_reason': (
                'QUOTE_NOT_POLLED_THIS_CYCLE' if not requested_this_cycle
                else 'SYMBOL_NOT_REFRESHED' if provider_status in {'OK', 'NO_QUOTE'}
                else 'UNKNOWN'
            ),
            'requested_this_cycle': requested_this_cycle,
            'cache_hit': False, 'cache_key': None,
            'cache_created_at': None, 'cache_expiry': None,
            'quote_source': None, 'bid': None, 'ask': None, 'mid': None,
            'spread_pct': None,
            'freshness_threshold_seconds': maximum_age_seconds,
            'exchange_timestamp_changed': None,
        }
    received = quote.received_at or quote.request_finished_at or evaluated
    received = received.astimezone(timezone.utc)
    exchange_at = quote.timestamp.astimezone(timezone.utc)
    exchange_age_at_receive = (received-exchange_at).total_seconds()
    exchange_age_at_evaluation = (evaluated-exchange_at).total_seconds()
    local_age = (
        (evaluated-quote.cache_created_at.astimezone(timezone.utc)).total_seconds()
        if quote.cache_hit and quote.cache_created_at is not None
        else 0.0 if quote.cache_hit else (evaluated-received).total_seconds()
    )
    stale = (
        maximum_age_seconds is not None
        and exchange_age_at_evaluation > maximum_age_seconds
    )
    reason = None
    if stale and quote.cache_hit:
        reason = 'LOCAL_CACHE_REUSE'
    elif (stale and quote.request_started_at is not None
          and exchange_at >= quote.request_started_at.astimezone(timezone.utc)
          and (quote.provider_latency_seconds or 0) > (maximum_age_seconds or 0)):
        reason = 'PROVIDER_REQUEST_DELAY'
    elif stale and exchange_age_at_receive > (maximum_age_seconds or 0):
        reason = 'PROVIDER_RETURNED_OLD_EXCHANGE_TIMESTAMP'
    elif stale and local_age > 0:
        reason = 'BATCH_REFRESH_DELAY'
    elif stale:
        reason = 'UNKNOWN'
    bid, ask = quote.bid, quote.ask
    mid = ((bid+ask)/2 if bid is not None and ask is not None and ask >= bid else None)
    spread = ((ask-bid)/mid if mid else None)
    return {
        'symbol': symbol.upper(),
        'poll_cycle_id': poll_cycle_id or quote.poll_cycle_id,
        'requested_this_cycle': requested_this_cycle,
        'request_started_at': (quote.request_started_at.isoformat()
                               if quote.request_started_at else None),
        'request_finished_at': (quote.request_finished_at.isoformat()
                                if quote.request_finished_at else None),
        'provider_latency_ms': (
            quote.provider_latency_seconds*1000
            if quote.provider_latency_seconds is not None else None
        ),
        'provider_status': provider_status,
        'exchange_timestamp': exchange_at.isoformat(),
        'received_timestamp': received.isoformat(),
        'evaluation_timestamp': evaluated.isoformat(),
        'exchange_quote_age_seconds': exchange_age_at_evaluation,
        'exchange_quote_age_at_receive_seconds': exchange_age_at_receive,
        'local_cache_age_seconds': local_age if quote.cache_hit else 0.0,
        'cache_hit': quote.cache_hit,
        'cache_key': quote.cache_key,
        'cache_created_at': (quote.cache_created_at.isoformat()
                             if quote.cache_created_at else None),
        'cache_expiry': (quote.cache_expiry.isoformat()
                         if quote.cache_expiry else None),
        'quote_source': quote.source,
        'bid': bid, 'ask': ask, 'mid': mid, 'spread_pct': spread,
        'freshness_threshold_seconds': maximum_age_seconds,
        'stale_reason': reason,
        'exchange_timestamp_changed': (
            exchange_at > previous_exchange_timestamp
            if previous_exchange_timestamp is not None else None
        ),
    }
