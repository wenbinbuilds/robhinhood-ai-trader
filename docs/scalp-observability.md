# SCALP_V1 shadow observability

This instrumentation answers where scalp observations stop between discovery
and a local shadow entry. It does not change any strategy threshold, admission
rule, position-management rule, or momentum behavior.

## Production path and counter semantics

Position monitoring runs first on every scalp tick and remains active even when
new entries are unavailable or blocked. The entry path is then:

1. Build the independent scalp universe and request provider quotes.
2. Reject a symbol already present in the shared portfolio.
3. Enforce the shadow-only runtime flags and blocked live kill switch.
4. Build completed micro-bars and features, classify the setup, and resolve its
   durable episode.
5. Reject a closed/stale episode.
6. Evaluate the signal engine's hard reasons: quote freshness, regular market,
   bid/ask validity, spread, completed-bar sufficiency and recency, relative
   volume, classified setup, score, extension, stop, expected net edge, target,
   and scalp risk/reward.
7. Append the session/cooldown/overtrading guards.
8. Emit `ScalpEntryReady` and begin an entry attempt only if no reason remains.
9. Run the preliminary scalp risk manager and size the local plan.
10. Run shadow pre-execution checks: episode idempotency, closing-session guard,
    duplicate race check, quote age, modeled fill, and post-fill risk/reward.
11. Run the shared portfolio controller, which revalidates risk and exposure.
12. Run the shadow safety override and add the position to local shadow state.

The persisted strict funnel is a cumulative survival view over those real
checks. Its counters are:

| Counter | Meaning |
|---|---|
| `universe_observations` | Symbol observations presented by discovery this poll. Repeated polls are repeated observations. |
| `provider_ok` | A provider quote object was returned successfully. |
| `duplicate_symbol_pass` | The shared portfolio did not already hold the symbol at the early guard. |
| `runtime_safety_pass` | The scalp instance is enabled, mode is shadow, live flags are false, and the kill switch remains blocked. |
| `quote_fresh` | Quote age is within the unchanged 2-second scalp limit. |
| `market_open_pass` | The quote declares the regular market open. |
| `valid_bid_ask` | A usable bid/ask spread can be computed. |
| `spread_pass` | Spread is at or below the configured limit. |
| `micro_bars_pass` | At least six completed, non-stale micro-bars are available. |
| `liquidity_pass` | Relative volume is available and meets the configured minimum. |
| `eligible_micro_signals` | A classified setup survives all earlier hard gates. |
| `episode_open_pass` | That eligible setup's durable episode is not already closed. |
| `signal_score_pass` | Signal score meets its configured minimum. |
| `extension_pass` | Entry is not overextended. |
| `stop_pass` | A valid structural stop with sufficient distance exists. |
| `expected_edge_pass` | Expected move minus modeled round-trip friction meets the minimum. |
| `target_pass` | A valid target above entry exists. |
| `scalp_rr_pass` | Gross scalp risk/reward meets its minimum. |
| `geometry_pass` | Derived roll-up: stop, target, and scalp RR all survived. It is not a second geometry mutation. |
| `overtrading_pass` | Session, symbol, consecutive-loss, daily-loss, and transaction-cost limits pass. |
| `entry_attempts` | `ScalpEntryReady` was reached and preliminary risk evaluation is about to run. |
| `risk_attempts` / `risk_approved` / `risk_rejected` | Preliminary scalp sizing evaluations and outcomes. |
| `pre_execution_attempts` / `pre_execution_passes` / `pre_execution_failures` | Calls into the local shadow execution engine and their hard pre-portfolio outcomes. |
| `portfolio_attempts` / `portfolio_approved` / `portfolio_blocked` | Shared portfolio/risk revalidation calls and outcomes. |
| `safety_approved` | Final local shadow safety override approvals. |
| `entries` | Positions actually added to local shadow state. |

`micro_signals_detected_anywhere` is deliberately outside the strict funnel.
Classification is calculated for every evaluable observation, even when that
observation also fails liquidity or another early gate. Therefore it can exceed
`liquidity_pass`. The prior label `micro_signal_candidates` was semantically
misleading, not evidence that the entry gates ran out of order.

## Rejections and candidate records

Each cycle stores all rejection reasons plus the first sequential
`blocking_stage` and `blocking_reasons`. Early data/setup failures are reported
under `filtered_reasons`; a setup or entry path stopped later is reported under
`entry_blocked_reasons`. Pre-execution, preliminary risk, and portfolio failures
also have dedicated reason aggregations.

Every universe observation is appended to `logs/scalp_diagnostics.jsonl` as a
`CANDIDATE_OBSERVATION`; every poll adds one `DISCOVERY_CYCLE`. A candidate
record includes:

- provider/quote status, quote age, and the unchanged maximum age;
- observed spread, threshold, and pass/fail;
- relative-volume input, source, fallback flag, observed value, minimum, and
  `UNAVAILABLE` versus `INSUFFICIENT` status;
- setup type/evidence and signal score;
- expected move, spread cost, both slippage assumptions, other friction,
  round-trip cost, expected net edge, threshold, and result;
- entry, stop, target, risk/reward percentages, gross RR, net reward, net RR,
  structural evidence, and geometry reasons;
- preliminary risk, pre-execution, portfolio, safety, and final outcome.

Cycle records include the complete funnel, filtered and blocked reasons, all
raw reasons, setup counts (`detected/eligible/attempted/entered`), quote-age
statistics, and spread statistics.

## Terminal and session views

The fast watcher prints per-cycle `SCALP FUNNEL`, `SCALP SIGNAL FUNNEL`,
`SCALP ENTRY FUNNEL`, reason, and setup-distribution lines. The independent
signal count is named `micro_detected_anywhere` so it cannot be mistaken for a
sequential survivor count.

`python runner.py --scalp-summary` remains local/read-only and now includes the
current U.S. session's:

- observation, unique-symbol, and unique-episode counts;
- every funnel total and a percentage with its explicit denominator;
- ranked raw rejection reasons plus filtered, entry-blocked, pre-execution,
  risk, and portfolio reason groups;
- setup-type counts at detection, eligibility, attempt, and entry;
- fresh/stale/unavailable quote counts and mean/median/p95/max age;
- median/p90/max spread, pass count/rate, and failure count;
- shadow entries, exits, and per-entry setup, expected edge, spread, slippage
  assumptions, holding time, exit reason, gross P&L, friction, and net P&L.

`python runner.py --loop --scalp-shadow --scalp-debug` prints one concise trace
for every observation, including early failures. Full diagnostics are persisted
whether debug mode is on or off. Debug changes output only.

## Safety and validation

- `MODE = SHADOW_TRADING`
- `SCALP_MODE = SHADOW`
- `LIVE_TRADING_ENABLED = False`
- `ROBINHOOD_EXECUTION_ENABLED = False`
- local live kill switch: `trading_blocked = true`
- no broker executor is referenced by the scalp runtime; entries and exits use
  only `ShadowPortfolio` and `ShadowExecutionEngine`
- position monitoring still runs before entry evaluation
- all 464 repository tests pass

No Robinhood order/account mutation was used while implementing or validating
this work. No profitability claim is made; these are observation diagnostics.
