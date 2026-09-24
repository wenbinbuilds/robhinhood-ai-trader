"""Single-symbol read-only refresh used immediately before local execution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config
from agent.staged_snapshot import InstrumentMetadataCache, candidate_bundle
from robinhood_mcp.client import DirectRobinhoodMcpClient
from robinhood_mcp.normalization import normalized_historicals, normalized_quotes
from execution.geometry import completed_structure
from watcher.models import FastQuote, price, timestamp
from watcher.quote_provider import quote_provenance
from watcher.storage import event


class DirectPreExecutionMarketDataProvider:
    """Fetch exactly one quote and one recent-candle series; never calls orders."""

    def __init__(self, client: DirectRobinhoodMcpClient, *, project_dir: str | Path,
                 market_direction: str = "UNKNOWN", clock=None) -> None:
        self.client = client
        self.project_dir = Path(project_dir)
        self.market_direction = market_direction
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh_symbol(self, symbol: str, *, now: datetime) -> Mapping[str, Any]:
        symbol = symbol.upper()
        # Collect bars first so quote age is measured after the slower request.
        historical_call = self.client.get_historicals(symbol)
        historical = normalized_historicals(historical_call.value, symbol)
        request_started = self.clock()
        quote_call = self.client.get_quotes([symbol])
        retrieved = self.clock()
        quote = normalized_quotes(
            quote_call.value, [symbol], retrieved_at=retrieved,
            request_started_at=request_started,
        ).get(symbol)
        source_at = timestamp(quote.get('quote_as_of')) if quote else None
        fast_quote = (
            FastQuote(
                symbol=symbol, bid=price(quote.get('bid')), ask=price(quote.get('ask')),
                last_price=price(quote.get('current_price')), timestamp=source_at,
                source='DIRECT_ROBINHOOD_MCP_PRE_EXECUTION', is_market_open=True,
                received_at=retrieved, request_started_at=request_started,
                request_finished_at=retrieved,
                provider_latency_seconds=quote_call.duration_seconds,
                provider_status='OK', cache_hit=False,
                poll_cycle_id='PREEXEC-' + symbol + '-' + retrieved.isoformat(),
            ) if quote is not None and source_at is not None else None
        )
        trace = quote_provenance(
            fast_quote, symbol=symbol, evaluation_at=retrieved,
            maximum_age_seconds=config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS,
            provider_status='OK' if fast_quote is not None else 'NO_QUOTE',
            poll_cycle_id='PREEXEC-' + symbol + '-' + retrieved.isoformat(),
        )
        trace.update({
            'request_context': 'PRE_EXECUTION',
            'strategy_scopes': ['POSITION'], 'batch_size': 1,
            'provider_concurrency': 1,
            'full_universe_cycle_duration_seconds': quote_call.duration_seconds,
            'poll_interval_since_previous_seconds': None,
            'freshness_by_strategy': {
                'POSITION': bool(
                    fast_quote and 0 <= fast_quote.age_at(retrieved)
                    <= config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS
                ),
            },
            'freshness_thresholds_seconds': {
                'POSITION': config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS,
            },
        })
        event(
            self.project_dir/config.QUOTE_PROVENANCE_LOG_PATH,
            'QUOTE_REQUEST_TRACE', retrieved,
            max_bytes=config.QUOTE_PROVENANCE_LOG_MAX_BYTES, **trace,
        )
        if quote is None:
            raise ValueError("refreshed quote unavailable")
        raw = {
            **historical,
            **quote,
            "previous_close": quote.get("previous_close") or historical.get("previous_close"),
            "relative_volume": historical.get("relative_volume"),
        }
        cache = InstrumentMetadataCache(
            self.project_dir / config.INSTRUMENT_METADATA_CACHE_PATH,
            now=now,
        )
        result = candidate_bundle(
            symbol, raw, scanner_row=None,
            market_direction=self.market_direction,
            cache=cache, now=retrieved,
            max_quote_age_seconds=config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS,
        )
        result.update(completed_structure(historical.get('candles', []), now=retrieved))
        result['refresh_completed_at'] = retrieved.isoformat()
        cache.save()
        return result
