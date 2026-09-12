# Factual Robinhood snapshot — CORE

Read `AGENTS.md`. Use only the enabled `robinhood-trading` MCP tools. Return one
JSON object matching the supplied schema. Do not explain, reason about markets,
write prose, recommend a trade, or edit files.

Never preview, review, place, modify, replace, cancel, close, or submit an order.
Never transfer funds or change account settings. Do not expose identifiers,
tokens, cookies, credentials, secrets, or personally identifying account data.

Retrieve the Agentic account, portfolio, equity positions, open equity orders
using retrieval only, and today's realized P&L. If no enabled Robinhood tool
provides market-session status, return UNKNOWN/null; Python owns the exchange
calendar and will replace it deterministically. This absence is not an MCP or
account-data failure.

Call `get_scans` and find only `AI_INTRADAY_MOMENTUM_V1`. If missing, inspect
`get_scanner_filter_specs` and create only that scan. If present, update only
that scan and only if needed. Never delete a scan. Run the named scan on every
snapshot collection cycle; Python decides whether closed-market results are
eligible for strategy analysis. The authorized target is: STOCK; Last 10–500; Volume >=500000
(1d/1); Average Volume >=500000 (1d/30); Relative Volume >=1.1 (1d/30); Percent
Change From Close >=0.01 (1d, Close); sort `% Change` descending. Use only live
supported syntax. Return every result up to the schema limit as factual scalar
columns; Python ranks and limits them. No quote history, indicators, fundamentals, Level 2,
news, web search, or analysis belong in this stage.

Set CONNECTED only after real required data was retrieved. Missing account or
scanner data is a real failure, never `NO_TRADE`.
