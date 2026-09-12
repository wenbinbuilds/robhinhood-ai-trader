# Factual Robinhood snapshot — MARKET DATA BATCH

Read `AGENTS.md`. Use only read-only `robinhood-trading` quote, historical, and
fundamental retrieval. Return one JSON object matching the supplied schema. Do
not explain, reason, recommend, use web search, call technical-indicator tools,
call Level 2/price-book tools, or edit files.

Never preview, review, place, modify, replace, cancel, close, or submit an order.
Never mutate any Robinhood state. Never output account identifiers, order IDs,
tokens, cookies, credentials, secrets, or raw MCP response envelopes.

For exactly the appended symbols, retrieve one 5-minute historical series with
up to 50 real OHLCV bars
including enough prior regular-session data for indicator warm-up, previous
close when available, and relative volume when directly available. Do not call
fundamentals: sector and industry must be null unless supplied from the local
cache later. Batch symbols in each tool call when supported. Ignore interpolated
bars for analytics but retain their truthful flag. After historical retrieval,
make exactly one final batched latest-quote call for price, bid, ask, and source
timestamp so quotes are as fresh as possible.
Do not synthesize bars or values. Record quote_retrieved_at immediately after
the quote call. For a per-symbol failure, put that symbol in failures as
DATA_UNAVAILABLE and continue. Return no more than one row or failure per input
symbol. Python calculates EMA, RSI, MACD, VWAP, volume, and intraday high/low.
