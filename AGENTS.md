# Robinhood AI Trader — Permanent Agent Instructions

MODE = SHADOW_TRADING

Only the human user may change `MODE`.

Only the human user may change live-execution enablement values, confirmation
values, or unblock the local live kill switch. Agents may inspect these values
but must never enable live execution or change `trading_blocked` to `false`.

Allowed future modes are:

- `ANALYSIS_ONLY`
- `SHADOW_TRADING`
- `REVIEW_ONLY`
- `LIVE_AUTONOMOUS`

## Safety boundary

While `MODE` is `SHADOW_TRADING`, every Robinhood prohibition and scanner-only
exception below remains identical to `ANALYSIS_ONLY`. Shadow entries, exits,
positions, cash, and P&L exist only in local application state and must never
cause a Robinhood order or account mutation.

While `MODE` is `ANALYSIS_ONLY` or `SHADOW_TRADING`:

- Never place, preview, review, modify, cancel, or submit an order.
- Never use an MCP tool that can create or alter an order.
- Never call any Robinhood MCP tool capable of changing account state, except
  for the single saved-scanner exception defined below.
- Use only read-only Robinhood functionality except for the named saved-scanner
  operations explicitly permitted below.
- All trade plans are non-broker outputs. In `SHADOW_TRADING`, quantities and
  fills may change only the dedicated local shadow state.
- `NO_TRADE` must always be an acceptable result.
- `NO_TRADE` means valid, fresh Robinhood data was evaluated and the strategy
  rejected every setup. Missing snapshots, stale data, MCP failures,
  authentication failures, and account-identification failures must be
  reported as data/connection errors instead.

## Permitted non-trading Robinhood mutation

While `MODE` is `ANALYSIS_ONLY` or `SHADOW_TRADING`, the agent may create,
update, configure, and run exactly one saved Robinhood scanner named:

`AI_INTRADAY_MOMENTUM_V1`

Only for that scanner, these Robinhood MCP operations are permitted when
needed:

- `create_scan`
- `update_scan_filters`
- `update_scan_config`
- `run_scan`
- read-only `get_scans`
- read-only `get_scanner_filter_specs`

The agent must not create or modify any unrelated saved scan. The agent must
not delete any saved scan unless the human user explicitly authorizes that
separately. This exception does not authorize trading or any mutation of
positions, cash, buying power, trading exposure, transfers, or account
settings.

All trading actions remain prohibited. While `MODE` is `ANALYSIS_ONLY` or
`SHADOW_TRADING`, the agent must never place an equity, options, or crypto
order; preview or review an order; modify or replace an order; cancel an order;
close a real position; submit an order; transfer funds; change account
settings; or perform any other action that changes real positions, cash,
buying power, or trading exposure.

## Account rules

- Only the Robinhood Agentic account may ever be considered for future trading.
- Other Robinhood accounts may be read for context only.
- Never persist account numbers, credentials, passwords, authentication tokens,
  session cookies, API secrets, 2FA codes, or similar sensitive values.
- A successful bridge snapshot must declare `data_source = ROBINHOOD_MCP`,
  `mcp_status = CONNECTED`, and a fresh UTC `generated_at` value.

## Asset rules

- Equities only.
- No options.
- No crypto.
- No short selling.
- No margin borrowing.
- No leveraged ETFs.
- No inverse ETFs.
- No overnight positions.

## Trading rules

- Focus initially on liquid U.S. equities suitable for intraday momentum trading.
- A stock appearing in a scanner must never automatically result in a trade
  candidate.
- Never average down.
- Do not chase a stock solely because its price is rising quickly.
- Do not create a trade candidate when market data is stale, missing,
  conflicting, or insufficient.
- Manage and analyze existing real positions before looking for new candidates,
  but never alter those positions in `ANALYSIS_ONLY` or `SHADOW_TRADING` mode.
- Stop considering new candidates once the regular U.S. market session ends.

Before recommending a trade candidate, attempt to evaluate:

- current price
- bid
- ask
- spread
- volume
- relative volume
- 5-minute candles
- VWAP
- 9 EMA
- 20 EMA
- RSI 14
- MACD
- support
- resistance
- intraday high
- intraday low
- overall market direction, if available

Every `TRADE_CANDIDATE` must contain:

- symbol
- direction
- entry
- stop
- target
- risk_per_share
- risk_reward_ratio
- confidence
- setup_name
- thesis
- invalidation_condition
- supporting_indicators

Do not invent unavailable data. Clearly mark unavailable values.
