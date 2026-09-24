# Shadow trader forensic debug — 2026-09-23

## Executive finding

The zero-entry result was not one ambiguous bottleneck.

- At the universe layer, stale exchange timestamps removed many observations. The direct fast provider made one fresh 12-symbol request every two seconds with no local quote cache; the batch was usually much faster than the freshness SLA. IRTC, BPRE, and QMCO repeatedly came back with old exchange timestamps.
- At the eligible SCALP layer, 168 score-eligible observations failed `SIGNAL_SCORE_BELOW_THRESHOLD`; eight eligible META observations passed 0.70 and then correctly failed the unchanged 1.10 gross-RR gate. No candidate reached an entry attempt.
- `STALE_SCALP_EPISODE` is not a time-age expiry. It means the exact structural episode identity was previously closed. A materially different timestamp/high-low fingerprint creates a new ID and is allowed.
- ANDG's recorded resistance calculation was arithmetically correct for the inputs, but the inputs were not temporally coherent: the 53.92 ask was timestamped inside the 16:35–16:40 candle later used to set 53.84 resistance. The stale/mixed evidence could therefore have influenced the rejection. Final pre-execution geometry now fails closed on that condition.

No threshold, confirmation count, risk/reward requirement, scoring weight, live flag, or kill-switch value was changed in this debug pass.

## Quote transport and freshness

### Fast SCALP path

Measured from the latest run:

| Metric | Result |
|---|---:|
| Universe size | 12 symbols |
| Configured poll interval | 2.000 s |
| Observed median start-to-start interval | 1.999 s |
| Observed p95 start-to-start interval | 2.129 s |
| Quote requests / failures | 141 / 0 |
| Provider concurrency | one client batch request; server internals unknown |
| Median provider batch latency | 0.1887 s |
| p95 provider batch latency | 0.2934 s |
| Maximum provider batch latency | 2.0461 s |
| Mean complete watcher-cycle duration | 0.2652 s |
| Median exchange quote age (provider metric) | 1.6484 s |
| p95 exchange quote age (provider metric) | 83.0188 s |
| Direct-provider local quote cache | none |

The 12-symbol batch does not normally take longer than the two-second freshness requirement. One measured request exceeded two seconds, but the systematic stale tail is far larger than request latency and concentrated by symbol. Re-polling cannot make an exchange timestamp advance when no newer quote is returned.

The 3,012 persisted SCALP observations from 251 cycles had this exchange-age distribution:

| min | median | p75 | p90 | p95 | p99 | max |
|---:|---:|---:|---:|---:|---:|---:|
| 0.111 s | 1.715 s | 4.732 s | 40.254 s | 73.862 s | 541.993 s | 602.433 s |

| Age bucket | Percent |
|---|---:|
| <= 1 s | 22.01% |
| <= 2 s | 56.14% |
| <= 3 s | 67.73% |
| <= 5 s | 75.66% |
| <= 30 s | 87.68% |
| > 60 s | 6.77% |
| > 120 s | 2.79% |
| > 300 s | 1.79% |

Worst repeatedly observed symbols:

| Symbol | observations | median age | p90 age | max age | <=2 s |
|---|---:|---:|---:|---:|---:|
| IRTC | 251 | 56.473 s | 552.268 s | 602.433 s | 3.6% |
| BPRE | 251 | 37.407 s | 87.658 s | 146.737 s | 2.8% |
| QMCO | 251 | 14.648 s | 47.062 s | 82.822 s | 6.4% |
| AMD | 251 | 1.905 s | 5.885 s | 12.849 s | 52.2% |
| AMZN | 251 | 1.563 s | 3.987 s | 19.619 s | 64.5% |
| META | 251 | 1.146 s | 2.134 s | 4.818 s | 86.5% |
| NVDA | 251 | 1.092 s | 1.932 s | 5.479 s | 91.2% |

The background historical refresh took 2.435 seconds in the last observed cycle, but it ran asynchronously; measured event-loop blocking was about 0.0000006 seconds. It did not serialize the quote poll. Its diagnostics correctly showed that wall-clock quote ages continue to grow while history is being fetched.

### POSITION slow path

The five latest slow candidates had ages 4.589, 23.790, 146.684, 223.178, and 504.049 seconds.

| min | median | p75 | p90 | p95 | p99 | max |
|---:|---:|---:|---:|---:|---:|---:|
| 4.589 s | 146.684 s | 223.178 s | 391.700 s | 447.874 s | 492.814 s | 504.049 s |

Only 20% were <=5 seconds, 40% were <=30 seconds, 60% were >120 seconds, and 20% were >300 seconds. IRTC failed the unchanged 300-second slow-analysis freshness gate; ANDG passed that slow gate at 223.178 seconds.

The slow quote call itself took 0.153 seconds. The full snapshot took 7.933 seconds, including 2.891 seconds for histories. Those timings rule out local request latency as the cause of the 147–504 second quote ages.

### Root-cause classification

For the current direct path, the evidence supports `PROVIDER_RETURNED_OLD_EXCHANGE_TIMESTAMP`:

- every fast cycle issued a new direct provider request;
- there were 141 requests and zero request failures;
- all 12 symbols were sent in one batch;
- median request latency was 0.189 seconds;
- the direct provider does not cache quotes;
- old ages were strongly symbol-specific.

The new provenance journal records every symbol/request with request start/end, latency, provider status, exchange/receive/evaluation timestamps, exchange age, cache metadata, bid/ask/mid/spread, poll cadence, batch size, consumer strategy, and one of:

`PROVIDER_RETURNED_OLD_EXCHANGE_TIMESTAMP`, `LOCAL_CACHE_REUSE`, `QUOTE_NOT_POLLED_THIS_CYCLE`, `BATCH_REFRESH_DELAY`, `PROVIDER_REQUEST_DELAY`, `SYMBOL_NOT_REFRESHED`, or `UNKNOWN`.

It covers fast-watch batches, slow snapshot batches, the degraded snapshot cache, and single-symbol pre-execution quote refreshes. The real run predates this new journal, so the attribution above is an inference from the measured provider metrics and code path; the next run will persist the per-request proof directly.

## SCALP candidate forensics

### Eligible symbols and scores

`eligible_micro_signals` counted 281 observations. A production score was available for 176 after excluding exact closed-episode identities. Repeated observations are intentionally not deduplicated.

| Symbol | Setup | eligible observations | scored | score range |
|---|---|---:|---:|---:|
| AMD | MICRO_BREAKOUT | 20 | 15 | 0.388111–0.433606 |
| AMD | VWAP_RECLAIM | 22 | 22 | 0.373688–0.406196 |
| IONQ | EMA9_CONTINUATION | 15 | 15 | 0.271075–0.317826 |
| IONQ | MICRO_BREAKOUT | 74 | 5 | 0.271108–0.294497 |
| IONQ | MICRO_PULLBACK | 21 | 6 | 0.270993–0.294432 |
| META | EMA9_CONTINUATION | 124 | 110 | 0.458456–0.746121 |
| META | MICRO_PULLBACK | 5 | 3 | 0.455613–0.487390 |

Eligible production-score distribution:

| min | median | p75 | p90 | p95 | max |
|---:|---:|---:|---:|---:|---:|
| 0.270993 | 0.484702 | 0.531278 | 0.613387 | 0.686094 | 0.746121 |

| threshold | percent at/above |
|---|---:|
| 0.50 | 36.36% |
| 0.60 | 13.64% |
| 0.65 | 7.39% |
| 0.68 | 6.25% |
| 0.70 | 4.55% |
| 0.75 | 0.00% |

This is mostly a materially weak-score population, not a population clustered just under 0.70. There is a small near-miss tail: the best score rejection was META at 0.690953, 0.009047 short.

Setup-specific distribution:

| Setup | detected | eligible | mean score | median | max | >=0.70 |
|---|---:|---:|---:|---:|---:|---:|
| EMA9_CONTINUATION | 396 | 139 | 0.513445 | 0.500577 | 0.746121 | 6.4% |
| MICRO_BREAKOUT | 197 | 94 | 0.382834 | 0.416041 | 0.433606 | 0.0% |
| MICRO_PULLBACK | 111 | 26 | 0.346528 | 0.294432 | 0.487390 | 0.0% |
| MOMENTUM_BURST | 91 | 0 | unavailable | unavailable | unavailable | unavailable |
| VWAP_RECLAIM | 264 | 22 | 0.392604 | 0.394009 | 0.406196 | 0.0% |

Eight eligible META/EMA9_CONTINUATION observations passed 0.70, with scores from 0.707723 to 0.746121. Every one then failed `SCALP_RR_BELOW_MINIMUM`; gross RR ranged from about 0.718 to 0.929, below the unchanged 1.10 gate. This is correct downstream rejection, not a score-attainability failure.

### Exact best near-miss score math

META at `2026-09-23T16:43:43.237086+00:00` scored 0.690953:

| Component | raw | normalized | weight | contribution | clamp |
|---|---:|---:|---:|---:|---|
| micro_momentum | 0.00117020 | 0.390067 | 0.25 | 0.097517 | none |
| vwap_alignment | 0.00717070 | 1.000000 | 0.20 | 0.200000 | max |
| ema_slope | 0.00043355 | 0.433545 | 0.15 | 0.065032 | none |
| relative_strength | 0.00127432 | 0.637158 | 0.15 | 0.095574 | none |
| volume_expansion | 1.621227 | 1.000000 | 0.15 | 0.150000 | max |
| spread_quality | 0.00017169 | 0.828307 | 0.10 | 0.082831 | none |
| entry_extension_penalty | 0.00195592 | 0.000000 | -0.20 | -0.000000 | min |

Score before penalties was 0.690953, total penalties were zero, final score was 0.690953, and margin to 0.70 was -0.009047. Its later-gate counterfactual still failed gross RR: expected net edge was 0.001181, but gross RR was 0.909 and net RR was 0.580.

### Top 20 signal-score rejections

All 20 were META `EMA9_CONTINUATION` observations with volume expansion 1.621227.

| # | UTC time | score | margin | quote age s | spread | offline net edge | offline net RR | downstream feasible |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | 16:43:43.237 | 0.690953 | -0.009047 | 1.007 | 0.000172 | 0.001181 | 0.580 | no |
| 2 | 16:43:29.294 | 0.684474 | -0.015526 | 0.786 | 0.000330 | 0.001057 | 0.442 | no |
| 3 | 16:43:31.227 | 0.684474 | -0.015526 | 1.970 | 0.000330 | 0.001057 | 0.442 | no |
| 4 | 16:43:41.322 | 0.673073 | -0.026927 | 1.032 | 0.000528 | 0.000813 | 0.340 | no |
| 5 | 16:42:45.145 | 0.650307 | -0.049693 | 1.024 | 0.000766 | 0.000234 | 0.085 | no |
| 6 | 16:42:53.132 | 0.643601 | -0.056399 | 1.122 | 0.000383 | 0.000617 | 0.271 | no |
| 7 | 16:43:19.232 | 0.643429 | -0.056571 | 0.232 | 0.000291 | 0.000970 | 0.447 | no |
| 8 | 16:43:27.267 | 0.643169 | -0.056831 | 1.342 | 0.000304 | 0.001052 | 0.500 | no |
| 9 | 16:42:41.093 | 0.633740 | -0.066260 | 0.122 | 0.000858 | 0.000142 | 0.052 | no |
| 10 | 16:44:07.304 | 0.616530 | -0.083470 | 0.608 | 0.000330 | 0.001111 | 0.723 | yes |
| 11 | 16:44:05.323 | 0.610243 | -0.089757 | 1.032 | 0.000330 | 0.001112 | 0.724 | yes |
| 12 | 16:43:51.284 | 0.609727 | -0.090273 | 0.639 | 0.000608 | 0.000832 | 0.476 | yes |
| 13 | 16:43:25.208 | 0.608281 | -0.091719 | 1.498 | 0.000370 | 0.001004 | 0.463 | no |
| 14 | 16:42:25.089 | 0.605045 | -0.094955 | 0.734 | 0.000225 | 0.000775 | 0.418 | no |
| 15 | 16:43:23.223 | 0.603343 | -0.096657 | 1.125 | 0.000607 | 0.000760 | 0.316 | no |
| 16 | 16:43:57.330 | 0.601545 | -0.098455 | 1.611 | 0.000396 | 0.001031 | 0.671 | yes |
| 17 | 16:42:23.125 | 0.597943 | -0.102057 | 1.420 | 0.000264 | 0.000736 | 0.397 | no |
| 18 | 16:43:59.280 | 0.590750 | -0.109250 | 0.245 | 0.000463 | 0.000965 | 0.628 | yes |
| 19 | 16:44:01.378 | 0.589635 | -0.110365 | 0.516 | 0.000264 | 0.001167 | 0.872 | yes |
| 20 | 16:42:55.215 | 0.589363 | -0.110637 | 0.884 | 0.000476 | 0.000524 | 0.257 | no |

Six of these 20 would have passed the production downstream extension, stop, net-edge, target, and gross-RR gates. Fourteen would still have failed later geometry. These are offline calculations only; no entry was attempted.

## Episode lifecycle

`STALE_SCALP_EPISODE` means `EPISODE_ID_PREVIOUSLY_CLOSED`. The identity is a hash of symbol, setup type, evidence timestamp, and rounded recent high/low. It does not mean “older than N seconds”; `stale_after_seconds` is therefore `null`.

The runtime now persists and prints creation/update timestamps, age, both fingerprints, structure-change status, reset permission, and the stale reason. Re-observing an exact closed fingerprint remains blocked, preventing replay. A new evidence timestamp or changed high/low fingerprint creates a different episode ID; the deterministic reset test confirms it can become eligible.

## POSITION post-admission and ANDG

Every POSITION candidate at or above 0.60 now gets a `[POSITION WATCH CANDIDATE]` trace containing both scores, both thresholds, quote freshness, entry/support/resistance/stop/target/RR, every hard-gate status, final state, primary/secondary blocks, and geometry timestamps.

For ANDG:

| Field | Value |
|---|---:|
| technical score | 0.769 |
| combined score | 0.602 |
| quote timestamp | 2026-09-23T16:36:49.536388Z |
| analysis / geometry time | 2026-09-23T16:40:32.714511Z |
| quote age | 223.178 s |
| slow freshness result | pass under unchanged 300 s limit |
| current / last | 53.825 |
| executable entry reference | ask 53.92 |
| selected stop | EMA20 53.349788 |
| structural resistance/target | 53.84 |
| resistance minus entry | -0.08 (-0.1484%) |
| recorded gross RR | -0.1403, not evaluated after resistance failed |
| minimum resistance price for 1.50 RR | 54.775318 |
| latest structural bar | began 16:35:00Z; completed 16:40:00Z |
| primary block | `RESISTANCE_ABOVE_ENTRY` |

The rule itself is correct: `53.84 > 53.92` is false. The historical rejection is not safely attributable to current market geometry, however, because the ask timestamp (16:36:49) precedes the close of the candle used to set resistance (16:40:00). This is a stale-quote/fresh-candle mixture. The candle high really was 53.84 and the recorded ask really was 53.92, so the input geometry was negative; freshness could have influenced which ask was compared.

Actual refresh architecture:

1. Slow watch admission uses the newly requested snapshot quote and the unchanged 300-second slow-analysis bound. It does not perform another request after LLM reasoning.
2. Admitted candidates are revalidated by the fast watcher; fast confirmation sees only quotes within its unchanged five-second bound.
3. Before any POSITION shadow entry, the pre-execution provider fetches one symbol's candles and quote again and enforces the unchanged 15-second bound.
4. Pre-execution now records quote/support/resistance/geometry timestamps and rejects `GEOMETRY_TIMESTAMPS_INCOHERENT` if the quote predates the completed structural bar set.

ANDG stopped at step 1, so it never reached fast confirmation or pre-execution refresh.

## Status and observability changes

The runtime no longer calls one early-stage drop the overall `primary_bottleneck`. It prints:

- `universe_primary_filter`, derived only from early/universe filters;
- `eligible_candidate_primary_block`, derived only after eligibility;
- `UNIVERSE_FILTER_COUNTS`;
- `ELIGIBLE_CANDIDATE_BLOCK_COUNTS`;
- `SESSION_CUMULATIVE_COUNTS`.

Every eligible candidate is printed even without `--scalp-debug`. The full score contribution trace and stale-episode lifecycle are also persisted. `--scalp-summary` includes SCALP quote provenance, and `--strategy-status` includes separate POSITION and SCALP quote-health reports.

## Files changed in this debug pass

- `agent/cycle_diagnostics.py`
- `config.py`
- `docs/shadow-trader-forensic-debug-2026-09-23.md` (new)
- `execution/pre_execution.py`
- `robinhood_mcp/pre_execution.py`
- `robinhood_mcp/snapshot.py`
- `runner.py`
- `strategies/scalp/analytics.py`
- `strategies/scalp/diagnostics.py`
- `strategies/scalp/execution.py`
- `strategies/scalp/models.py`
- `strategies/scalp/runtime.py`
- `strategies/scalp/setup.py`
- `watcher/fast_watcher.py`
- `watcher/models.py`
- `watcher/quote_diagnostics.py` (new)
- `watcher/quote_provider.py`
- `watcher/storage.py`
- `tests/test_diagnostic_forensics.py` (new)

Some of these files already contained earlier work in the dirty worktree; only the diagnostic additions described above belong to this pass.

## Verification and safety

Ten new deterministic forensic tests cover quote provenance, provider-old and cache-stale classification, polling cadence, eligible trace output, score sums/margins, stale episode lifecycle, structural reset, >=0.60 POSITION tracing, resistance math, coherent geometry timestamps, status separation, and frozen safety/configuration values. Existing tests already cover fresh single-symbol pre-execution refresh and fail-closed stale quotes.

Full suite: **509 passed**.

Frozen values verified:

- POSITION watch 0.60, trade 0.72, confirmations 2, minimum RR 1.50.
- POSITION weights remained technical 0.70, news 0.10, sector 0.05, market 0.05, qualitative 0.10 throughout this task.
- SCALP quote age 2 s, spread 0.10%, volume expansion 1.20, score 0.70, net edge 0.05%, RR 1.10, max hold 180 s.
- `MODE = SHADOW_TRADING`.
- `LIVE_TRADING_ENABLED = False`.
- `ROBINHOOD_EXECUTION_ENABLED = False`.
- local kill switch remains `trading_blocked: true`.

No Robinhood order, preview, cancellation, modification, account change, or other broker mutation occurred. All analysis used existing local runtime artifacts and deterministic tests with fake providers.
