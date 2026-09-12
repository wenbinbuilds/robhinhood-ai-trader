# Robinhood AI Trader

This project is a local Robinhood research and shadow-trading system. Its
permanent mode is currently `SHADOW_TRADING`: entries, exits, positions, cash,
and P&L are simulated only in local files. It cannot preview, review, place,
modify, replace, cancel, close, or submit a real order.

The only permitted Robinhood mutation is maintenance and execution of the one
project-owned saved scanner `AI_INTRADAY_MOMENTUM_V1`, as defined in
`AGENTS.md`. All other Robinhood use is factual retrieval.

## Current event-driven architecture

Routine factual collection no longer uses `codex exec`:

```text
DirectRobinhoodMcpClient (official Python MCP SDK, Streamable HTTP + OAuth/PKCE)
  -> Robinhood Trading MCP
  -> Python field allowlisting and normalization
  -> AI_INTRADAY_MOMENTUM_V1 ranking (top 10)
  -> one batched quote request + bounded concurrent 5-minute histories
  -> deterministic Python EMA9/EMA20/RSI14/MACD/VWAP/session calculations
  -> schema validation + atomic state/market_snapshot.json
  -> event-gated LlmReasoningBridge refresh for meaningful slow evidence
  -> deterministic CoordinatorAgent
  -> persisted slow CandidateContext + typed CandidateStateStore (300-second TTL)

Fast loop (approximately every two seconds; no LLM/subprocess/web calls)
  -> one direct batched quote request
  -> QuoteEvent (newest quote wins per symbol under backpressure)
  -> deterministic LiveMarketScorer
  -> AlphaCombiner (age-weighted slow/live score)
  -> explicit candidate state machine
  -> hard quote/spread/market/geometry/risk validation
  -> two consecutive qualifying updates
  -> typed TradePlan -> portfolio construction -> local ShadowExecutor
  -> independent FastPositionWatcher -> stop/target/close events
```

The in-process event bus uses these priority bands: stop/risk/position events
are critical or high, open-position quotes are high, candidate quotes and alpha
updates are medium, scanner/bar/context events are low, and news/LLM work is
background. Quote events are coalesced per symbol, so a delayed consumer sees
the newest queued quote instead of replaying stale updates. Component-handler
failures are recorded and isolated.

Candidate states and legal forward path are:

```text
DISCOVERED -> WATCHLIST -> SETUP_FORMING -> TRADE_READY
-> RISK_APPROVED -> POSITION_OPEN -> EXIT_PENDING -> CLOSED
```

`WATCHLIST`/`SETUP_FORMING` may expire or be rejected; `SETUP_FORMING` may
return to `WATCHLIST`; infrastructure failures have an explicit
`INFRASTRUCTURE_BLOCKED` state. Impossible transitions raise an error. State
and transition history are atomically persisted in
`state/candidate_states.json`, while meaningful typed events are written to
`logs/market_events.jsonl`. The dashboard projection exposes `UNIVERSE`,
`WATCHLIST`, `SETUP FORMING`, `TRADE READY`, `OPEN POSITIONS`, and
`RECENTLY CLOSED` collections.

The direct snapshot path does not run a subprocess, model, web search,
fundamentals query, technical-indicator MCP tool, Level 2 query, or news query.
News and qualitative context remain in a separate slow reasoning layer. The
event-driven reasoning cache refreshes for a new completed five-minute bar,
candidate-set change, news event, sector change, market-regime change, or
technical disposition change. A mere quote or wall-clock change does not call
the model, and the fast candidate and position paths cannot import it.
Strategy thresholds, coordinator weights, and deterministic risk limits are
unchanged.

`agent/codex_mcp_bridge.py` remains only as the explicitly selectable
`LEGACY_CODEX_MCP` diagnostic provider. There is no automatic fallback: a direct
failure reports `DIRECT_MCP_UNAVAILABLE` or `DIRECT_MCP_NOT_AUTHENTICATED` and
does not start the old multi-minute collection path.

## Installation and independent authorization

Python 3.12 or newer is required. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

The implementation pins the official Model Context Protocol Python SDK
(`mcp==2.2.0`) and uses its Streamable HTTP OAuth client. The SDK performs MCP
OAuth discovery, dynamic client registration, authorization-code flow with
PKCE, state/issuer validation, and token refresh.

This Python application requires its own one-time Robinhood consent. It does
not read Codex credentials, reuse Codex tokens, read browser cookies, scrape
Robinhood, or call undocumented endpoints. On macOS, this application's OAuth
token and dynamic client registration are stored in the login Keychain under
service `robinhood-ai-trader.direct-mcp`; secrets are not placed in repository
files, logs, snapshots, or reasoning prompts.

Run the explicit setup command:

```bash
python runner.py --robinhood-auth
```

It starts a localhost callback on `127.0.0.1:8765`, opens the Robinhood consent
page, and performs authentication only. It invokes no Robinhood order tool.
After browser consent, verify factual access:

```bash
python runner.py --robinhood-mcp-check
```

The check discovers the live tool schemas, retrieves the Agentic account using
read-only access, and requests one NVDA quote solely as connectivity evidence.
NVDA does not enter scanner candidates or strategy decisions unless the saved
scanner independently returns it.

## Running

One fresh research/shadow cycle:

```bash
python runner.py --once
```

Persistent slow-cycle research plus local position monitoring:

```bash
python runner.py --loop
```

Read-only local status and performance:

```bash
python runner.py --shadow-status
python runner.py --shadow-summary
```

Every slow cycle requests a new direct snapshot. Existing snapshots are never
silently reused after a collection error. Core authentication/account/portfolio
or scanner failures block analysis. A single candidate history failure is
recorded as `DATA_UNAVAILABLE` while complete candidates remain analyzable.
Closed regular markets preserve the existing behavior: no candidate analysis
and no new shadow entry.

In `SHADOW_TRADING`, a slow-cycle score never opens a position directly. A
candidate that passes every true-hard data, access, liquidity, structural, and
risk gate and scores at least `0.60` is saved to
`state/candidate_watchlist.json`. Signal-quality failures remain explicit
warnings and do not create a terminal rejection by themselves. The slow score
is the documented existing coordinator formula:

```text
0.35 technical + 0.20 news + 0.10 sector + 0.10 market + 0.25 qualitative
```

The deterministic live score is:

```text
0.25 spread quality + 0.25 price/VWAP state + 0.15 price/EMA9 state
+ 0.20 support/resistance location + 0.15 controlled momentum
```

Raw appreciation is limited to 15% and sharp extensions receive no momentum
credit. Slow/live weights interpolate linearly from 70%/30% at research time
to 50%/50% at the 300-second expiration. The dynamic score is their weighted
sum. A local shadow entry requires a dynamic score of at least `0.72` for two
consecutive fresh-quote updates, plus every unchanged hard market, spread,
stop, target, risk/reward, portfolio, daily-loss, sizing, and session rule.

### Candidate gate classification

The executable policy is defined in `agent/gate_policy.py`. Existing numeric
rules are unchanged.

| Classification | Rules/evidence | Admission effect |
|---|---|---|
| `DATA_VALIDITY_HARD` | symbol, required fields, minimum completed candles | terminal for the current slow result; retry on a later factual cycle where applicable |
| `MARKET_ACCESS_HARD` | configured price/instrument range, regular session, long-only asset policy | no fast-watch admission |
| `LIQUIDITY_HARD` | quote freshness and maximum spread | no admission; fast quotes recheck transient freshness/spread failures |
| `STRUCTURAL_TRADE_HARD` | stop reference, stop distance, target above entry | no admission and never overridable by alpha |
| `RISK_HARD` | minimum risk/reward and portfolio/daily-loss/sizing/duplicate limits | no admission or entry and never overridable by alpha |
| `SLOW_SIGNAL_QUALITY` | price/VWAP, EMA, RSI, MACD, relative volume, candle structure, market direction, minimum strategy score, maximum conflicts, minimum confidence | contributes to the unchanged slow score and warnings; does not itself terminally reject |
| `LIVE_SIGNAL_QUALITY` | live spread quality, VWAP/EMA hold, location, controlled momentum | changes live/dynamic alpha; cannot override a hard gate |

`MINIMUM_STRATEGY_SCORE`, `MAXIMUM_CONFLICTS`, and `MINIMUM_CONFIDENCE`
remain calculated with their original values. They now diagnose slow signal
quality. The technical score is the sole technical component in the unchanged
coordinator formula; derived technical confidence is persisted as a diagnostic
and is not applied as a second numeric penalty or veto.

Score history is sampled every 20 seconds (plus threshold transitions) in the
bounded `logs/candidate_score_history.jsonl`. Entry-time slow/live/dynamic
scores, weights, context age, discovery/watchlist/trade-ready/entry timestamps,
and complete candidate-transition attribution are stored on the local shadow
position and eventual closed-trade record.

The direct MCP connection is owned by a dedicated asyncio thread for the
lifetime of `--loop`. Slow collection and the quote provider submit work to the
same bounded async session. Read-only transient failures use limited exponential
backoff and a clean reconnect; scanner mutation calls are never automatically
retried. Ctrl+C closes the MCP session and joins the watcher.

## Fast quote provider

`RobinhoodDirectQuoteProvider` implements the existing `FastQuoteProvider`
boundary with one direct batched quote request. It measures request latency,
source quote age, and failure rate. Until at least three real successful samples
show request latency within the configured two-second interval and source age
within five seconds, its mode remains `DIRECT_MCP_UNVALIDATED`; the watcher
therefore behaves conservatively and does not claim `REALTIME_FAST`.

`SnapshotQuoteProvider` remains available only for the opt-in legacy data
provider. It is not used by the default direct configuration.

## Configuration and data

Important defaults in `config.py`:

| Setting | Default |
| --- | --- |
| `MODE` | `SHADOW_TRADING` |
| `ROBINHOOD_DATA_PROVIDER` | `DIRECT_MCP` |
| `FAST_QUOTE_PROVIDER` | `DIRECT_MCP` |
| `SLOW_CYCLE_TARGET_SECONDS` | `300` |
| `FAST_QUOTE_INTERVAL_SECONDS` | `2` |
| `CANDIDATE_CONTEXT_TTL_SECONDS` | `300` |
| `SLOW_WEIGHT_AT_RESEARCH` | `0.70` |
| `SLOW_WEIGHT_AT_EXPIRATION` | `0.50` |
| `FAST_ENTRY_CONFIRMATION_UPDATES` | `2` |
| `SCORE_HISTORY_SAMPLE_SECONDS` | `20` |
| `ROBINHOOD_MCP_MAX_CONCURRENCY` | `4` |
| `ROBINHOOD_MCP_READ_RETRIES` | `2` |
| `MAX_CANDIDATES_TO_ANALYZE` | `10` |
| `LIVE_TRADING_ENABLED` | `False` |
| `ROBINHOOD_EXECUTION_ENABLED` | `False` |

Normalized private state and generated logs are gitignored. The snapshot has
`data_source=ROBINHOOD_MCP`, `mcp_access_path=DIRECT_MCP`, a fresh UTC
`generated_at`, explicit MCP status, normalized Agentic account/portfolio state,
scanner information, SPY/QQQ context, candidate data, and warnings/errors. It is
validated against `schemas/market_snapshot.schema.json` and atomically replaced.

Direct timing telemetry is atomically written to
`state/direct_mcp_timings.json` and includes connection, scanner, quote,
historical, candidate collection, indicators, normalization, snapshot total,
and individual allowlisted tool durations. It never contains tool arguments,
raw results, account identifiers, or credentials. Reasoning and overall cycle
durations remain in the existing slow-cycle logs.

## Tests

```bash
python -m pytest -q
```

All direct-client tests use mocked SDK/Robinhood behavior. Tests never contact
Robinhood and never invoke an order tool. Coverage includes OAuth metadata and
PKCE, secure token persistence, discovery/schema-derived arguments, direct
snapshot generation, atomic replacement, local indicators, partial candidate
failure, direct quotes, authentication absence, safety allowlists, lifecycle,
and clean shutdown.

The deterministic between-bar proof is available without network access:

```bash
python -c 'from event_driven.simulation import run_deterministic_shadow_simulation as run; print(run())'
```

It demonstrates discovery/research, a live-alpha threshold crossing with
two-update confirmation, risk approval, a local shadow entry before the next
five-minute scan, a fast target exit, and a separate high-alpha candidate that
is still blocked by the daily-loss risk veto.

Official references: [Robinhood Agentic Trading](https://robinhood.com/us/en/support/articles/agentic-trading-overview/),
[MCP Python SDK OAuth clients](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md),
and [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).
