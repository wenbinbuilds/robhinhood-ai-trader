# One analysis-only market cycle

Read `AGENTS.md` first and obey it as a permanent safety boundary.

This invocation performs exactly one market evaluation cycle. Use only
read-only Robinhood functionality and only the Robinhood account identified by
the read-only account lookup as the Agentic account. Other accounts may be read
for context only.

Do not place an order.

Do not preview an order.

Do not review an order.

Do not cancel an order.

Do not modify an order.

Do not call any Robinhood MCP tool capable of changing account state.

Use read-only account, portfolio, equity-position, equity-order retrieval,
realized-P&L, saved-scan execution, equity quote, 5-minute historical,
technical-indicator, Level 2, and index quote functions only. Do not create or
modify a scan, watchlist, alert, or any other Robinhood object.

Collect the Agentic account's portfolio value, unleveraged buying power,
positions, open equity orders when available, and today's realized equity P&L.
Analyze existing positions first without changing them. Run an existing saved
equity momentum scan whose criteria match `config.SCANNER_CRITERIA`. If no such
saved scan exists, report a data error; never create one.

For at most `MAX_CANDIDATES_TO_ANALYZE` equity symbols, collect current quote,
bid/ask timestamps, regular-session 5-minute candles, volume, relative volume,
VWAP, EMA 9, EMA 20, RSI 14, MACD, labeled intraday support/resistance
references, intraday high/low, and
Level 2 when useful. Record unavailable values as unavailable; never infer or
fabricate a tool result.

The supported automated bridge is `python runner.py --once`, which uses
`prompts/refresh_snapshot.md` and `schemas/market_snapshot.schema.json`.
Never include account numbers, credentials, tokens, cookies, or other secrets.
To analyze an already valid, fresh snapshot, run:

```bash
python runner.py --once
```

The local program may produce `NO_TRADE` or a theoretical `TRADE_CANDIDATE`
with deterministic risk sizing only from valid data. Connection, authentication,
staleness, and missing-data problems must use explicit error statuses. It must
never execute a candidate.
