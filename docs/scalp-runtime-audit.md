# SCALP_V1 shadow-runtime audit

## Implementation report

1. **Main-loop wiring.** `SCALP_V1` is wired into `python runner.py --loop`.
   `runner.main` establishes the shared `ShadowPortfolio`, persistent direct MCP
   client, market-data cache and fast watcher. `load_scalp_runtime` then builds
   `ScalpRuntime`, which runs `ScalpPositionController` before
   `ScalpEntryController`. Approved entries go through the shared
   `RiskManager`, `ShadowExecutionEngine` and canonical portfolio only. Analytics
   read strategy-attributed records from that same portfolio.

2. **Why it was absent.** `SCALP_ENABLED=False` caused
   `load_scalp_runtime` to return `None`, so no scalp universe was requested and
   no scalp diagnostics appeared. This was correct default behavior, but there
   was no explicit process-local activation command.

3. **Previous discovery dependency.** The original `ScalpMarketDataCache` read
   only `candidate_data`. Snapshot validation requires every row in that array
   to come from `AI_INTRADAY_MOMENTUM_V1`; consequently the scalp controller was
   deterministic and LLM-free, but its discovery feed was momentum-scanner
   dependent.

4. **New discovery design.** The direct collector now produces separate
   `candidate_data` (`strategy_id=MOMENTUM`) and `scalp_candidate_data`
   (`strategy_id=SCALP`). The scalp universe is a configurable, bounded union of
   eight liquid-equity seeds, current scanner symbols, recent local candidates
   and shadow-position data. It is capped at 12 and then shares the watcher's
   global 20-symbol ceiling. Quotes and historicals are requested once for the
   deduplicated union. Strategy state, episodes and decisions remain separate.

5. **Momentum scanner funnel.** The latest persisted 2026-09-18 snapshot contains
   `provider_raw=1`, one reused saved definition, `after_provider_filters=1`,
   `after_local_filters=1` and `top_n=1`, symbol NCI. Older 2026-09-17 event records contain 10 retained
   candidates, the configured local cap; their provider-raw count was not
   persisted by that older code, so a claimed 20–30 raw count cannot be
   reconstructed. New snapshots persist every funnel stage, configuration,
   query timestamp and result source.

6. **Meaning of `SCAN ACTION: REUSED`.** It means the existing saved definition
   named `AI_INTRADAY_MOMENTUM_V1` was reused. It does not mean result rows were
   reused. The direct collector called `get_scans` and then `run_scan`; the
   persisted direct timing record shows the `scanner_run` call took about 0.195
   seconds in the observed cycle.

7. **Result freshness.** Direct snapshots now persist
   `results_source=FRESH_PROVIDER_RUN` and `query_executed_at`. Terminal output
   reports scan age. No local candidate-result cache participates in the direct
   scanner path. Saved definition reuse therefore cannot silently masquerade as
   fresh-result reuse.

8. **Provider success versus quote freshness.** Candidate rows and fast-watcher
   status separately report `provider_status`, `quote_status` and
   `quote_age_seconds`. A successful batch with an old exchange timestamp is
   `provider_status=OK` and `quote_status=STALE`, not a fresh quote. The observed
   NCI exchange timestamp was 266.399 seconds behind snapshot generation.

9. **Strategy-specific freshness.** Momentum retains its existing 300-second
   slow-analysis limit. Scalp retains its stricter 2-second limit, chosen to
   match the configured two-second fast polling cadence: an observation older
   than one complete polling interval is not used for a new scalp entry.
   Provider-successful stale observations reach the scalp hard gate so
   `ScalpEntryBlocked` can record `STALE_QUOTE`; they do not reach shadow
   execution. Positions are managed only with the watcher's usable quote set.

10. **NCI disposition.** NCI was correctly rejected. The provider returned only
    one scanner row; local filtering did not remove other returned rows. NCI
    failed spread, minimum RR and several technical conditions and had zero
    technical/qualitative scores. Nothing in this change adjusts its score or
    overrides those gates. Its 266-second quote is within the unchanged
    momentum 300-second tolerance but far outside the scalp two-second limit.

11. **Zero momentum candidates.** A zero-row momentum scan still yields
    `MOMENTUM: NO_TRADE`, while `scalp_candidate_data` is collected for the
    independent scalp universe and the fast watcher continues evaluating it.
    No momentum candidate, watch score or LLM result is required to populate
    that universe.

12. **Independent strategy state.** Momentum context/state remains in the
    candidate watchlist and event-driven state store. Scalp uses its own typed
    decisions, `SCALP-*` episode IDs, durable setup store and event journal.
    Market-data cache reads return deep copies. A rejection by either strategy
    cannot delete or mutate the other's candidate state. The shared portfolio
    lock still prevents two positions in the same symbol and double-counted
    exposure.

13. **Startup and status output.** Loop startup now prints either
    `SCALP: DISABLED` or `SCALP: ENABLED (SHADOW ONLY)` beside the momentum
    status. `--scalp-status` reports the discovery source and seed universe,
    freshness/spread/net-edge/hold limits, symbol-trade and daily-loss limits,
    open scalp positions, mode, both live flags and kill-switch state. Fast-loop
    sampled output reports the scalp universe, provider successes, fresh quotes,
    spread/liquidity passes, micro setups and entries without per-quote spam.

14. **Safe activation.** The default remains disabled. A human may run
    `python runner.py --loop --scalp-shadow` for that process only. The command
    neither edits configuration nor creates a live mode. Startup refuses unless
    mode is `SHADOW_TRADING`, scalp mode is `SHADOW`, both live flags are false
    and the kill switch is blocking. Entry checks repeat these safety conditions.

15. **LLM independence.** Scalp discovery, signal evaluation, episodes, entries
    and exits contain no LLM/reasoning dependency. An empty momentum watchlist or
    unavailable qualitative reasoning does not suppress the scalp runtime.
    Background context may fill an absent non-freshness field only.

16. **Scalp liquidity data.** Scanner relative volume remains unchanged for
    momentum. When an independent scalp seed lacks that scanner-only field, the
    collector records a scalp-specific completed-micro-bar volume ratio and its
    source. The existing 1.20 threshold is still applied; this is an observable
    data-source correction, not a looser threshold.

17. **Files changed for this audit.** Runtime/configuration changes are in
    `runner.py`, `config.py`, `watcher/fast_watcher.py`,
    `watcher/quote_provider.py`, `strategies/scalp/{runtime,market_data,execution}.py`,
    `robinhood_mcp/{snapshot,normalization}.py`, `agent/{staged_snapshot,codex_mcp_bridge}.py`
    and `schemas/market_snapshot.schema.json`. Tests were extended in
    `test_scalp_strategy.py`, `test_direct_mcp.py`, `test_fast_watcher.py` and
    `test_runner.py`.

18. **Tests.** Added deterministic coverage for process-local activation,
    unsafe activation refusal, expanded status, zero momentum results with an
    independent scalp universe, immutable shared snapshots, separate provider
    and freshness status, configurable runtime enablement and repeated safety
    checks. Existing stale/fresh, LLM-free, episode-idempotency, coexistence and
    no-real-executor tests remain green.

19. **Full suite.** The original runtime audit passed 450 tests; the subsequent
    observability extension brings the current repository suite to `464 passed`.
    Python compilation, snapshot-schema parsing and `git diff --check` also pass.

20. **Momentum parameters.** Unchanged: watch threshold 0.60, trade threshold
    0.72, confirmation count 2 and minimum RR 1.50. Momentum scoring weights,
    LLM weights, stop/target behavior and scanner criteria were not changed.

21. **Scalp parameters.** No signal, spread, edge, stop, RR, risk, overtrading or
    holding threshold was lowered to generate activity. In particular maximum
    spread remains 0.10%, minimum net edge 0.05%, minimum RR 1.10 and quote age
    two seconds. `SCALP_ENABLED` remains `False` by default.

22. **Safety configuration.** Verified `MODE=SHADOW_TRADING`,
    `LIVE_TRADING_ENABLED=False`, `ROBINHOOD_EXECUTION_ENABLED=False` and local
    kill switch `trading_blocked=true` / `BLOCKED`.

23. **Broker safety.** No real Robinhood order, preview, modification,
    cancellation or account mutation was called. This audit used local tests and
    persisted diagnostics; it did not start a live-data loop or contact
    Robinhood.

The purpose of these changes is correct independent operation and diagnostic
truthfulness. They do not establish that the scalp strategy is profitable or
that it should generate trades under current market conditions.
