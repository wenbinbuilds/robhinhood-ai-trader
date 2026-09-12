# Robinhood MCP non-trading snapshot refresh

Read `AGENTS.md` first. The application may be in `ANALYSIS_ONLY` or local-only
`SHADOW_TRADING`; Robinhood access remains read-only except for the exact named
saved-scanner exception.

Return exactly one JSON object matching `schemas/market_snapshot.schema.json`.
Do not edit files; the calling Python process owns the snapshot file.
Use the MCP immediately, make independent retrieval calls in parallel
when the tool supports it, and finish as soon as the required snapshot is
complete. Do not inspect or modify project code during this collection task.

## Absolute prohibitions

Do not preview, review, place, replace, modify, cancel, or submit any order. Do
not close a position, transfer funds, change account settings, or call any tool
that changes positions, cash, buying power, or trading exposure. Do not use
options or crypto tools. Never output an account number, order ID, access
token, session cookie, credential, authentication secret, or 2FA value.

The sole permitted non-trading mutation is creation or maintenance of exactly
one saved scanner titled `AI_INTRADAY_MOMENTUM_V1`. For that scanner only,
`create_scan`, `update_scan_filters`, `update_scan_config`, and `run_scan` are
permitted. Never create, update, or run a different saved scan, and never delete
any scan. Do not mutate watchlists, alerts, or any other Robinhood object.

Use only authenticated `robinhood-trading` MCP tools for brokerage and market
data within the boundary above. Public web/news search is permitted only for
recent candidate catalyst research; never use it as a substitute for Robinhood
quotes or account data. Do not use a different broker, invented data, or cached
market data. Actual permitted Robinhood operations are limited to account lookup,
portfolio retrieval, equity positions, equity-order retrieval, realized P&L,
scanner filter specs, saved scanner management/execution, equity quotes, equity
historicals, equity technical indicators, equity price books, and index quotes.

## Failure statuses

First determine whether those Robinhood read-only tools are available. If they
are not present, return `mcp_status: MCP_UNAVAILABLE`. If Robinhood or MCP asks
for authentication or returns an authentication error, return
`mcp_status: MCP_NOT_AUTHENTICATED`. For another Robinhood retrieval failure,
return `mcp_status: ROBINHOOD_ERROR`. In a failure response, use empty/null
values and an honest error message; never label fake or empty data `CONNECTED`.

Set `mcp_status: CONNECTED` only after real Robinhood data was retrieved.
Set `mcp_access_path` to `CUSTOM_MCP` when the named MCP provided the tools, or
`UNKNOWN` only on failure.

## Required collection

Complete the project scanner lifecycle first so a missing scanner is created
within this invocation. Then collect the remaining account and market data.

1. Use the account lookup to find exactly the account marked Agentic-accessible.
   Other accounts may be read for context but must not be normalized. Use full
   account identifiers transiently only as required between MCP calls and omit
   them completely from the output.
2. Retrieve that account's portfolio value, buying power, unleveraged buying
   power, current equity positions, existing open equity orders via retrieval
   only, and today's realized equity P&L when available. Preserve a `null` P&L
   plus a warning when the tool cannot supply it.
3. Enrich position rows with current prices from read-only equity quotes when
   available. Never change a position or order.
4. Determine current regular U.S. market-session status in America/New_York.
   Use `OPEN`, `CLOSED`, or `UNKNOWN`, and include an `as_of` timestamp.
5. Every cycle, call `get_scans` and look only for the exact title
   `AI_INTRADAY_MOMENTUM_V1`.
   - If absent, call `get_scanner_filter_specs`, inspect the live tool schemas,
     then create exactly that scanner using only supported syntax.
   - If present, inspect its filters, columns, and sorting. Update that scanner
     only when its configuration differs from the target below.
   - Never modify an unrelated scan. Never delete a scan.
   - Run the named scan every cycle. On creation, update, or execution failure,
     use `mcp_status: ROBINHOOD_ERROR` and the precise scanner status
     `CREATE_FAILED`, `UPDATE_FAILED`, or `RUN_FAILED`.
   - Record `lifecycle_action` as `CREATED`, `UPDATED`, or `REUSED`.

   The verified initial target is deliberately broad and long-only:
   - Asset type: `FILTER_TYPE_INSTRUMENT_TYPE`, `=`, `STOCK`.
   - Last: `FILTER_TYPE_LAST`, `BETWEEN`, `10`, `500`.
   - Volume: `FILTER_TYPE_VOLUME`, `>=`, `500000`, interval `1d`, length `1`.
   - Average volume: `FILTER_TYPE_AVERAGE_VOLUME`, `>=`, `500000`, interval
     `1d`, length `30`.
   - Relative volume: `FILTER_TYPE_RELATIVE_VOLUME`, `>=`, `1.1`, interval
     `1d`, length `30` (Robinhood's canonical accepted lookback).
   - % Change: `FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE`, `>=`, `0.01`, interval
     `1d`, plot `Close`. Percentages are decimal ratios, so `0.01` is 1%.
   - Robinhood generates Last, Volume, Average volume, Relative volume, and
     % Change columns from these filters; do not add duplicate extra columns.
   - Configure sort-only by the exact supported `% Change` display-name column
     in `desc` direction.

   Set `result_count` to the total returned by `run_scan`. Preserve only the
   first `MAX_CANDIDATES_TO_ANALYZE` (currently 10) results in `candidates`.
   Zero results is a successful `OK` scan with an empty candidate list.
6. Always perform an NVDA connectivity check, including when the market is
   closed. Retrieve the latest quote, bid, ask, their relevant timestamps, and
   recent regular-session 5-minute OHLCV history when available. Over a range
   with enough warm-up bars, request read-only 5-minute RSI 14, MACD, EMA 9,
   EMA 20, and VWAP. Use `null` and list the field in `unavailable_values` when
   Robinhood cannot supply it. Ignore interpolated bars for analytics. Set the
   NVDA check to `OK` when a real quote is returned even if closed-market bid or
   ask data is unavailable; otherwise use `ERROR` or `UNAVAILABLE` honestly.
   Put this data only in `connectivity_checks.NVDA.market_data`. Do not add NVDA
   to `candidate_data` unless NVDA is actually among the retained scan results.
7. For every scanner candidate (up to the configured maximum), collect the same
   quote, 5-minute history, indicator, and optional Level 2 fields and normalize
   them as entries in the `candidate_data` array. Every candidate-data symbol
   must occur in `scanner.candidates`; never use a diagnostic symbol as a
   fallback strategy candidate. Store the observed/derived intraday low as
   `intraday_support_reference` and intraday high as
   `intraday_resistance_reference`. Do not call them sophisticated support or
   resistance unless Robinhood provides a genuine such indicator.
   Retain 30 actual recent 5-minute candles per candidate when Robinhood makes
   them available, including prior regular-session history if needed. Six real
   bars is the unchanged technical-analysis minimum. NEVER truncate analysis
   history to three bars for brevity. Preserve source timestamps and OHLCV;
   do not synthesize, pad, or duplicate missing bars. Use supported historical
   intervals/spans only; mark insufficient history honestly when unavailable.
   Collect histories, indicators, fundamentals, benchmarks and news FIRST.
   Defer the final candidate and shadow-position quote retrieval until AFTER
   these slower calls, immediately before producing the final JSON. Batch final
   quotes where supported; do not redundantly quote each symbol at every stage.
   quote_as_of must remain the actual source quote timestamp, not completion time.
   Record candidate_quote_retrieved_at as the actual UTC clock time immediately
   after the final quote tool response; use null if that time cannot be measured.
   Do not substitute the source quote timestamp or invent a retrieval time.
8. Retrieve read-only SPY and QQQ quote, VWAP, 5-minute EMA 9/20, previous
   close, intraday change, and recent 5-minute candles once per cycle. Put them
   in `market.benchmarks`. If a read-only VIX or volatility proxy is available,
   summarize it in `market.volatility_context`; otherwise use null.
   Set `market.direction` objectively: bullish when the available benchmark
   evidence is predominantly price above VWAP, EMA 9 above EMA 20, and positive
   intraday change; bearish for the inverse; mixed when evidence conflicts; and
   unknown when insufficient. Python recomputes this field deterministically.
9. For each retained candidate, retrieve sector and industry through available
   read-only instrument/fundamental data. When practical, retrieve one relevant
   sector ETF context bundle once and reuse it: SMH or SOXX for semiconductors,
   XLV/XBI for healthcare or biotech, XLE for energy, or XLF for financials.
   Store price, VWAP, EMA 9/20, intraday movement, and unavailable fields under
   `sector_benchmark`; use null when unavailable or unsupported.
10. Only while the regular U.S. session is open, search current public web/news
    sources for each retained candidate to answer why it is moving today.
    Prefer company releases, regulatory filings, major wires, and reputable
    financial news. Store normalized `news_items` containing headline, source,
    real publication timestamp (or null), URL (or null), factual summary,
    source-quality tier, catalyst type, sentiment, and importance 0..1. Never
    fabricate a timestamp, URL, catalyst, or publisher. Python will cluster
    reports about the same event. Social-media rumors must use LOW_CONFIDENCE
    unless corroborated. Use an empty array when no meaningful report exists or
    the market is closed.
11. The calling process appends a validated list of open local shadow-position
    symbols. For each listed symbol, retrieve a fresh read-only quote, bid/ask,
    recent 5-minute candles, indicators, sector metadata, and current news using
    the same normalized candidate shape. Put these only in
    `shadow_position_data`, even if the symbol is no longer returned by the
    scanner. These are local simulated positions, not Robinhood positions. If
    the appended list is `NONE`, return an empty array.

Choose the freshest regular or non-regular trade price according to its source
timestamp. Set `generated_at` to the actual UTC completion time. Set
`data_source` exactly to `ROBINHOOD_MCP`. Do not include raw MCP response
envelopes, guides, account identifiers, or credentials.

The output schema requires every declared field. On failures, fill account and
portfolio fields with `false`/`null`, keep arrays empty, and use honest failure
statuses. Represent scanner `columns` as `{name, value}` entries and keep
`candidate_data` as an array containing only retained scanner symbols. Always
return `shadow_position_data` as a separate array.
