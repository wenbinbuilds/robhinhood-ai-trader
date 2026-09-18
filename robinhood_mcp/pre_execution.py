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
        )
        result.update(completed_structure(historical.get('candles', []), now=retrieved))
        result['refresh_completed_at'] = retrieved.isoformat()
        cache.save()
        return result
