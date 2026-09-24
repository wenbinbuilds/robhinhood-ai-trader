# SCALP_V1 micro-bar freshness audit

This report covers the production-code trace and the shadow-only correction made on
2026-09-18. No Robinhood tool was called during the investigation or test run.

1. **Root cause.** `DirectSnapshotCollector` fetched history during the slow snapshot
   only. `ScalpMarketDataCache` then re-read that file while the fast watcher refreshed
   quotes every two seconds. `ScalpSignalEngine` expired a completed bar 120 seconds
   after that bar's close, rather than allowing it through the next five-minute close
   boundary. There was no scalp-owned history refresh to obtain the next completed bar.

2. **Timeframe.** `historical_arguments()` requests Robinhood `5minute`, `day`,
   `regular` history. Normalized candles now retain `interval_seconds=300` and
   `bar_source=ROBINHOOD_MCP_HISTORICALS_5MINUTE` instead of making downstream code
   infer the timeframe.

3. **Previous freshness logic.** The old gate calculated
   `now - latest_bar_begins_at - interval > 120`. For a bar beginning 15:45 and closing
   15:50, it therefore rejected the bar just after 15:52 even though the 15:50–15:55
   bar could not yet be complete.

4. **Corrected freshness logic.** Completed bars are causal: their declared interval
   must have ended, and future, forming, interpolated, malformed, or invalid OHLCV rows
   are excluded. A bar is `FRESH` until `expected_next_bar_close + 2s`, `AGING` during
   the existing 120-second provider-lag allowance, `STALE` after that, and `UNAVAILABLE`
   if no completed bar exists. Quote freshness remains an independent two-second gate.

5. **Boundary refresh.** `ScalpHistoryRefresher` seeds a per-symbol in-memory cache from
   the snapshot, calculates each symbol's next completed-bar boundary, and makes one
   deduplicated `get_historicals_many` request for due scalp symbols plus SPY/QQQ. A
   successful new bar advances the next boundary, so the two-second quote loop does not
   refetch history. Failures have a 30-second retry throttle and preserve the last good
   bars while marking the provider error. Both values are operational cadence settings,
   not trading thresholds.

6. **Momentum coupling.** Initial scalp history was in the snapshot request union and
   did not require momentum admission, but all subsequent history updates were coupled
   to the slow snapshot/research cycle. The new refresher is owned by the fast scalp
   runtime and has no scanner, momentum-candidate, or LLM dependency.

7. **Cache audit.** The old cache key was just the symbol and had no TTL or boundary
   invalidation; it only reloaded when the snapshot file mtime changed. It did not share
   one history object across symbols. A second precedence defect allowed later general
   snapshot sections to overwrite the scalp-owned row; load order now makes the scalp
   row authoritative. The new cache retains symbol, declared timeframe, last attempt,
   last refresh evidence, provider state, and boundary-derived refresh eligibility.

8. **Provider freshness.** The captured snapshot was generated at
   `15:50:39.613265Z`; all captured scalp histories ended with a bar beginning 15:45
   and closing 15:50, so returned history was about 39.6 seconds old and appropriate
   for a completed five-minute feed. The evidence does not show a provider-cadence
   failure. Runtime metrics now record returned-history age so future provider lag is
   distinguishable from application caching.

9. **Relative volume.** Scalp data uses provider-history relative volume when supplied;
   otherwise it calculates the latest completed bar volume divided by the mean of up to
   five preceding completed bars. That bar-aligned fallback now precedes a momentum
   scanner fallback. Its source, source timestamp, status, and fallback flag are carried
   in feature provenance. No quote count is represented as trade volume.

10. **Liquidity classification.** A fresh numeric value below 1.20 produces only
    `VOLUME_EXPANSION_BELOW_MINIMUM`. Missing/error data produces
    `VOLUME_DATA_UNAVAILABLE`; a value tied to stale bars produces
    `VOLUME_DATA_STALE`. Thus some previously observed low numeric ratios were real,
    but classifications emitted after bar expiry were data-induced and are no longer
    counted as genuine low liquidity.

11. **Score dependencies.** The score's three-bar momentum, price/VWAP relation, EMA9
    slope, SPY relative strength, completed-bar volume expansion, extension penalty,
    recent returns, and realized volatility depend wholly or partly on bar history.
    Spread quality depends on the quote. EMA9 slope is now derived from the normalized
    completed close series when no explicit prior EMA is present; SPY/QQQ returns are
    derived from their own completed histories.

12. **Old score misclassification.** Yes. Missing or stale values were coerced through
    expressions such as `value or 0`, yielding a low numeric score and
    `SIGNAL_SCORE_BELOW_THRESHOLD` even when the score was not trustworthy.

13. **New rejection behavior.** Every critical score input records feature name, value,
    source timestamp, status, and fallback use. Stale critical data yields no score and
    `SIGNAL_DATA_STALE`; missing, conflicting, or provider-error input yields no score
    and `SIGNAL_DATA_INVALID`. Neither case emits `SIGNAL_SCORE_BELOW_THRESHOLD`.
    Provider refresh failures additionally emit `MICRO_BAR_REFRESH_FAILED`.

14. **Provider impact.** The saved real timing sample made 13 concurrent historical
    calls in one batch: batch latency 3.685s, candidate collection 3.983s, total snapshot
    6.811s; recorded individual examples were 0.481s, 0.668s, and 1.021s. One sample is
    insufficient for a reliable production p95. The new cumulative metrics expose batch
    count, symbols requested, success rate, and latency/history-age median, p95, and max.
    Entry quote quality is re-evaluated after history IO, so that latency cannot bless an
    aged quote.

15. **Before/after behavior.** Production diagnostics went from 8 passing symbols at
    15:51:58 to zero at 15:52:00, with 11 `MICRO_BARS_STALE` blocks, even though the next
    bar closed at 15:55. The local regression proves `micro_bars_pass=8`, then 8 again
    at 15:54:59 with the same valid completed bar, then 9 after one boundary refresh.

16. **Files modified for this fix.** `config.py`, `runner.py`,
    `agent/technical_indicators.py`, `agent/staged_snapshot.py`,
    `robinhood_mcp/normalization.py`, `schemas/market_snapshot.schema.json`,
    `watcher/fast_watcher.py`, and the scalp modules `models.py`, `market_data.py`,
    `signals.py`, `runtime.py`, and `diagnostics.py`. New files are
    `strategies/scalp/freshness.py`, `strategies/scalp/history.py`, this report, and
    `tests/test_scalp_microbar_freshness.py`.

17. **Tests added.** Deterministic coverage includes fresh and stale bars, validity
    through the current five-minute window, allowed-lag expiry, one-shot boundary
    refresh, cache advance/recovery, refresh failure, zero momentum/LLM coupling,
    stale versus truly low liquidity, score provenance and validity, future/incomplete
    exclusion, duplicate-fetch prevention, shadow-only surface safety, the 8/8/9 funnel
    sequence, and position monitoring before entry blocking.

18. **Full suite.** `python -m pytest -q` passes: **477 passed in 2.75s**.

19. **Thresholds.** Unchanged: quote age 2s, spread 0.10%, relative volume 1.20,
    signal score 0.70, minimum net edge 0.05%, minimum risk/reward 1.10, and maximum hold
    180s. The existing 120-second bar-provider lag was retained and given correct
    boundary-relative semantics.

20. **Momentum behavior.** Momentum thresholds, scoring, admission, and execution were
    not changed. The only shared candle change preserves explicit timeframe/provenance
    and makes completed-only filtering honor the declared interval; scalp-specific
    relative-volume precedence is gated by `strategy_id == SCALP`.

21. **Shadow safety.** Verified runtime values are `MODE=SHADOW_TRADING`,
    `SCALP_MODE=SHADOW`, `LIVE_TRADING_ENABLED=False`,
    `ROBINHOOD_EXECUTION_ENABLED=False`, and local kill switch
    `trading_blocked=true`. Position monitoring executes before history refresh and entry
    evaluation, so stale or failed entry data cannot disable existing-position handling.

22. **External operations.** No real Robinhood request, order, preview, cancellation,
    account mutation, or account read was performed. The new component can call only the
    already allowlisted read-only historical batch method; all verification used saved
    local artifacts and fakes.

Local quote aggregation was audited but intentionally not implemented. The quote feed
does not supply authoritative trade volume, so generating OHLC-only synthetic bars would
make the volume and relative-volume features incomparable with provider history and
would violate the provenance requirement.
