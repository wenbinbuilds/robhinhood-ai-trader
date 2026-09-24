# SCALP End-to-End Latency Audit — 2026-09-24

## Executive finding

The AMD case does **not** show that shadow execution caused the missed entry. It
shows a data-horizon and market-data timing problem before approval:

- The first causally usable post-close quote was evaluated at
  `18:40:00.510782Z`, but the newly completed 5-minute history did not finish
  refreshing until `18:40:05.598867Z`.
- AMD was first classified and eligible at `18:40:05.834142Z`, **5.616 seconds**
  after that quote's exchange timestamp.
- Its first eligible score was already `0.721344`; the `.60`, `.65`, `.68`, and
  `.70` milestones all occurred on that same observation.
- It was already `30.833 bp` above EMA9 and failed `ENTRY_OVEREXTENDED` on that
  first classified observation. Its net RR was also only `0.16696`.
- No entry was approved, so the episode has no actual decision-to-shadow-write
  latency. If the geometry gates had counterfactually passed, the local
  entry-ready timestamp (`T10`) would have been the T3 observation; this is not
  evidence of a real or shadow fill.

The earlier `0.650`, `0.657`, `0.692`, and later `0.708+` observations occurred
*after* the initial `0.721` observation. They do not form a causal climb from
eligibility to `.70` for this episode. Therefore the evidence does not support
lowering the score threshold from this case alone.

**SHADOW TRADING IS NOT THE BOTTLENECK.** A production-sized local shadow write
benchmark measured `3.098 ms` median, `3.483 ms` p90, `3.591 ms` p95, and
`3.701 ms` max. The AMD delay occurred before approval, primarily while waiting
for completed 5-minute context and during an unusually slow shared provider
request.

## Scope and evidence limits

The primary session ran from `2026-09-24T18:22:19.544005Z` through
`2026-09-24T18:43:41.305467Z`. It contains 7,660 SCALP quote observations, 640
quote cycles, 123 episodes, and 108 observations for
`SCALP-AMD-acedf9f33955d164af1a7ba2`.

The AMD case is an offline reconstruction from local journals. Historical logs
recorded provider, exchange, receive, and evaluation timestamps, but did not
record every internal phase with monotonic precision. The code now records the
missing phases. Phase figures explicitly marked *benchmark* were measured
locally with deterministic fixtures; they are not presented as live-market
measurements.

After the changes, a connected shadow-only validation ran from
`19:25:38.224499Z` through `19:26:40.787160Z`: 32 complete instrumented cycles
and 392 SCALP quote observations. This roughly one-minute sample is useful for
validating phase attribution but is too short, and its discovered-symbol mix is
too different, to establish durable before/after market-data percentiles. It
created no SCALP entry. The process was stopped cleanly.

Artifacts:

- [Every AMD observation](scalp-latency-amd-observations-2026-09-24.csv)
- [Machine-readable session metrics](scalp-latency-metrics-2026-09-24.json)
- [Machine-readable post-fix sample](scalp-latency-postfix-sample-2026-09-24.json)
- [Reproducible offline audit script](../scripts/scalp_latency_audit.py)

## Latency budget

| Component | Measurement | Result | Interpretation |
|---|---:|---:|---|
| Provider request | request start → response | median `206.124 ms`; p95 `414.544 ms`; max `4.161 s` | Usually sub-second, but has material tail latency. |
| Provider data age | exchange timestamp → receive | median `1.702 s`; p95 `21.953 s`; max `122.051 s` | The provider often returns an old exchange timestamp for less-active symbols. This is distinct from application compute time. |
| Poll cadence | evaluation cycle → next cycle | median `2.001 s`; p95 `2.142 s`; max `5.929 s` | The configured two-second poll can add nearly two seconds of sampling delay. |
| Local receive delay | response received → evaluation | median `1.374 ms`; p95 `1.776 ms`; max `4.618 s` | Normal local dispatch is fast; the max is a contention/outlier signal. |
| Full-universe quote refresh | one deduplicated batch | median `209.134 ms`; p95 `416.455 ms`; max `4.163 s` | Symbols are not fetched serially on the observed batch path. |
| Feature compute | evaluation start → features | benchmark median `0.101 ms`; p95 `0.129 ms` | Not a bottleneck. Future live runs now persist this phase. |
| Setup classification compute | features → classification | benchmark median `0.0028 ms`; p95 `0.0038 ms` | Not a bottleneck. |
| Setup observation proxy | exchange quote → first classified evaluation | median `1.413 s`; p95 `20.166 s`; max `48.503 s` (`n=86`) | Includes data age and polling; it is not pure setup compute. |
| Classification → eligibility | first classified → first eligible | median `0 s`; p95 `43.608 s`; max `81.824 s` (`n=29`) | Most eligible episodes were eligible immediately; sample is censored because most episodes never qualified. |
| Eligibility → `.70` | first eligible → first `.70` | median `92.075 s`; p95 `174.943 s`; max `184.150 s` (`n=2`) | Too few qualifying episodes for a systematic conclusion. AMD was `0 s`; the other episode was `184.150 s`. |
| Geometry | signal evaluation and entry geometry | benchmark median `0.039 ms`; p95 `0.048 ms` | Not a bottleneck. |
| Risk | geometry → risk result | benchmark median `0.0226 ms`; p95 `0.0325 ms` | Not a bottleneck. |
| Pre-execution | risk → revalidation | benchmark median `1.035 ms`; p95 `1.222 ms` | Safety checks remain intact and are inexpensive. |
| Shadow execution | approved request → local position persisted | production-sized benchmark median `3.098 ms`; p95 `3.591 ms`; max `3.701 ms` | Not a bottleneck. No AMD approval occurred. |

The old fast-watcher status reported average cycle work of `0.501 s`, but its
logs do not contain a trustworthy historical phase distribution. The post-fix
sample now separates those phases in `logs/fast_watcher_latency.jsonl` without
polluting or rotating the strategy-event journal:

| Post-fix phase (`n=32`) | Median | p90 | p95 | Max |
|---|---:|---:|---:|---:|
| Total cycle work | `378.03 ms` | `798.80 ms` | `961.67 ms` | `1,069.02 ms` |
| Intended wait/sleep | `1,621.97 ms` | `1,674.86 ms` | `1,678.21 ms` | `1,684.06 ms` |
| Quote batch | `226.14 ms` | `511.31 ms` | `791.12 ms` | `794.39 ms` |
| Quote ingestion | `3.51 ms` | `5.53 ms` | `6.62 ms` | `7.50 ms` |
| POSITION work | `59.85 ms` | `67.61 ms` | `75.09 ms` | `86.82 ms` |
| SCALP work | `35.85 ms` | `58.18 ms` | `136.28 ms` | `219.04 ms` |
| Other candidate work | `53.38 ms` | `58.47 ms` | `62.30 ms` | `87.37 ms` |
| Terminal rendering | `0.757 ms` | `0.847 ms` | `0.868 ms` | `0.893 ms` |
| Status persistence | `1.024 ms` | `1.221 ms` | `1.821 ms` | `10.027 ms` |

Cycle-start cadence was `2.0051 s` median and `2.0052 s` p95. Every measured
cycle completed inside the configured two-second interval; most elapsed time
was deliberate waiting, not compute.

## AMD exact latency waterfall

The 18:40 bar could only become completed context at `18:40:00Z`. The first
fresh post-close quote above the breakout anchor had exchange time
`18:40:00.218502Z`; it was received at `18:40:00.509387Z` and evaluated at
`18:40:00.510782Z`. The history refresh was scheduled at `18:40:02.506690Z`
and returned at `18:40:05.598867Z` (`3.092 s` refresh, `3.018 s` provider
work). The classification-cycle quote request ran from `18:40:04.348457Z` to
`18:40:05.833332Z` (`1.485 s`).

```text
AMD MICRO_BREAKOUT — SCALP-AMD-acedf9f33955d164af1a7ba2

18:40:00.000000  5m bar closes; final completed-bar context can now exist
18:40:00.218502  T0 first fresh post-close exchange quote above anchor
18:40:00.509387     application receives that quote       +0.290885 s
18:40:00.510782     application evaluates it              +0.001395 s
18:40:02.506690     asynchronous history refresh scheduled
18:40:05.598867     completed 5m history arrives          +3.092177 s
18:40:05.833332     classification-cycle quote received   1.485415 s request
18:40:05.834142  T1 setup classified                      +5.615640 s from T0
18:40:05.834142  T2 episode created                        +0 s
18:40:05.834142  T3 first eligible                         +0 s
18:40:05.834142  T4 score >= .60 (actual .721344)          +0 s
18:40:05.834142  T5 score >= .65                           +0 s
18:40:05.834142  T6 score >= .68                           +0 s
18:40:05.834142  T7 score >= .70                           +0 s
18:40:05.834142  T8 ENTRY_OVEREXTENDED                     +0 s
18:40:52.632111  T9 first SCALP_RR_BELOW_MINIMUM          +46.797969 s
18:40:05.834142  T10 counterfactual entry-ready if every gate passed
not observed         actual approved shadow entry
```

Milestone intervals:

| Interval | Seconds |
|---|---:|
| T1 − T0 | `5.615640` |
| T2 − T1 | `0` |
| T3 − T2 | `0` |
| T4 − T3 | `0` |
| T5 − T4 | `0` |
| T6 − T5 | `0` |
| T7 − T6 | `0` |
| T7 − T3 | `0` |
| T8 − T3 | `0` |

At T0, mid was `$623.395`; at T3 it was `$623.220`. The move while waiting was
`-$0.175`, `-0.02807%`, or `-2.807 bp`. In other words, AMD did not run upward
during this measured delay. Distance above the `$622.4217` breakout anchor fell
from `$0.9733` (`15.637 bp`) to `$0.7983` (`12.826 bp`). Extension above EMA9
fell from about `33.65 bp` to `30.833 bp`, but both were beyond the configured
`30 bp` limit.

Consequently, all requested price-movement windows—detection → eligibility,
eligibility → `.65`, eligibility → `.70`, `.65` → `.70`, and `.70` → first
extension rejection—either share the T3 timestamp or reduce to the T0→T3 move
above. There was no additional price movement while waiting for score milestones.

At T3:

| Field | Value |
|---|---:|
| Bid / ask / mid | `$623.11 / $623.33 / $623.22` |
| Exchange quote age at evaluation | `0.740617 s` |
| Spread | `0.03530%` |
| Breakout reference | `$622.4217` |
| Micro momentum | `0.16232%` |
| Relative strength | `0.08021%` |
| EMA9 / EMA20 | `$621.304329 / $620.937810` |
| VWAP | `$619.146671` |
| Volume expansion | `1.833433×` |
| Score | `0.721344` |
| Extension | `0.30833%` |
| Stop / target | `$621.304329 / $624.199900` |
| Gross / net RR | `0.42944 / 0.16696` |
| Decision | `BLOCKED: ENTRY_OVEREXTENDED` |

The ask needed to be at or below approximately `$622.6832` to meet RR `1.10`
with the same stop and target—only `4.201 bp` above the breakout. The first
post-close ask was already `$623.52`, so the RR geometry was invalid before the
local history delay. The RR failure cannot be attributed solely to a late `.70`
decision.

## Provider, polling, and batching

Across the session, quote age at receive was:

| Age bucket | Share |
|---|---:|
| ≤ 500 ms | `5.08%` |
| ≤ 1 s | `21.71%` |
| ≤ 2 s | `57.51%` |
| ≤ 3 s | `71.04%` |
| > 3 s | `28.96%` |

AMD was somewhat better: median age `1.676 s`, p95 `5.788 s`, and `61.41%`
within two seconds. Its distinct exchange-update gaps were median `2.413 s`,
p90 `5.232 s`, p95 `6.447 s`, and max `12.015 s`.

The observed quote request is one deduplicated provider batch for the current
universe. Every symbol in a cycle shares the same request timing, so there is no
client-side symbol-1-to-symbol-12 processing tail on this path. Older exchange
timestamps vary by symbol, which points to returned market-data age—not local
sequential processing—as the main cross-symbol freshness difference. Provider
internals are not observable. Scalar-only fallback schemas use bounded
concurrency rather than uncontrolled fan-out.

Quote requests near the five-minute slow/history boundaries had p95
`3.585 s` and max `4.161 s`, versus p95 `0.348 s` away from those boundaries.
The medians were similar, so this is tail contention, not a consistently slow
provider. History refreshing is asynchronous to the fast loop, but both paths
share the MCP transport. The history batch could occupy all four request slots.

## POSITION, dashboard, and hot-path persistence

POSITION LLM, news, sector, and market-analysis work runs on the slow loop, not
inside SCALP entry evaluation. The fast loop does perform local POSITION quote
work before SCALP so exits retain priority. Historical evidence does not show
that local work as the main delay; the shared MCP history tail is the measurable
coupling.

A 100-cycle deterministic benchmark measured `0.751 ms` median / `0.931 ms`
p95 headless and `0.828 ms` median / `0.970 ms` p95 with dashboard rendering.
The median cost was approximately `0.077 ms`, so rendering is not materially
blocking the loop and was not moved to an asynchronous renderer.

The old setup controller atomically rewrote a roughly 537 KB state file on
unchanged observations. A local benchmark measured `6.964 ms` median and
`7.802 ms` p95 per rewrite—about `83.6 ms` for 12 symbols. Unchanged episodes
now update in memory and persist only creation, transitions, and new milestones;
that path measured `0.015 ms` median and `0.023 ms` p95 per observation, about
`0.185 ms` for 12 symbols.

## Score timing across recent episodes

There were 123 distinct episodes: 49 EMA9 continuations, 37 unclassified, 20
micro pullbacks, 10 micro breakouts, 4 momentum bursts, and 3 VWAP reclaims.
Only 29 ever became eligible, and only two reached `.70`. This is heavy right
censoring: a median over the two survivors must not be read as a population
estimate.

| Setup type | Episodes with eligibility timing | Classification → eligibility median / p95 / max | Episodes reaching `.70` | Eligibility → `.70` |
|---|---:|---:|---:|---:|
| EMA9_CONTINUATION | 23 | `0 / 14.727 / 81.824 s` | 1 | `184.150 s` |
| MICRO_BREAKOUT | 3 | `0 / 0 / 0 s` | 1 | `0 s` |
| MICRO_PULLBACK | 2 | `0 / 0 / 0 s` | 0 | unavailable |
| VWAP_RECLAIM | 1 | `62.049 / 62.049 / 62.049 s` | 0 | unavailable |
| All eligible | 29 | `0 / 43.608 / 81.824 s` | 2 | median `92.075 s`; p95 `174.943 s` |

For AMD, 62 of 108 observations were fresh enough to score. Scores ranged
from `0.470155` to `0.807544`, median `0.541783`. Of 18 observations passing
the score gate, 13 failed extension and 5 failed RR. That says geometry blocks
strong observations, but it does **not** prove `.70` caused those blocks: AMD
failed extension at its first eligible observation, and its RR was already
impossible at the first post-close causal quote.

## Offline entry comparisons

The replay uses realistic long-side friction: ask × `1.00025` for entry and
future bid × `0.99975` for exit. Returns are therefore net of spread and the
modeled 2.5 bp slippage on each side. Missing future ticks are not interpolated.

| Hypothetical trigger | Adjusted fill | 15 s | 30 s | 60 s | 120 s | 180 s | MFE / MAE through 180 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| First post-close causal context + quote, 18:40:00.510782 | `$623.67588` | `-0.13495%` | `-0.13014%` | `-0.10289%` | `-0.05640%` | `-0.08525%` | `-0.03075% / -0.16861%` |
| First valid setup and `.60/.65/.68/.70`, 18:40:05.834142 | `$623.48583` | `-0.05159%` | `-0.09970%` | `-0.07564%` | `-0.03235%` | `-0.05961%` | `-0.00028% / -0.13818%` |

The raw price crossed the breakout earlier, at exchange time
`18:38:35.124604Z`. A friction-adjusted raw-cross entry would have produced
approximately `+0.0062%`, `+0.1250%`, `+0.1057%`, `+0.0174%`, and `+0.0271%`
at 15/30/60/120/180 seconds, with `+0.1411%` MFE and `-0.0677%` MAE. However,
the only causally available completed-bar volume expansion then was `0.84765`,
below the required `1.20`. Using the later `1.83343` value at that earlier time
would be look-ahead bias. This attractive raw-cross result is not a valid
production prototype under the unchanged rules.

## Feature horizon and provider capability audit

| Feature | Actual source/update horizon | Alignment with ≤180 s hold |
|---|---|---|
| Quote, bid, ask, last/current price | Exchange-stamped quote updates, irregular seconds | **FAST ENOUGH** when fresh |
| Micro momentum | Quote tape, up to a 180-second rolling window | **FAST ENOUGH / BORDERLINE** depending on update gaps |
| EMA9 / EMA20 | Completed 5-minute bars | **TOO SLOW** for entry timing; usable as context |
| VWAP | Completed 5-minute history | **TOO SLOW** for entry timing; usable as context |
| Volume expansion | Latest completed 5-minute bar versus prior bars | **TOO SLOW** for entry timing; cannot change between boundaries |
| Relative strength | Quote-derived stock return against 3-bar 5-minute SPY context | **BORDERLINE** |
| Support / resistance | Six completed 5-minute bars | **TOO SLOW** for entry timing; usable as geometry/context |

This is a **HORIZON MISMATCH**: the intended hold is seconds to 180 seconds,
while critical context arrives on completed five-minute boundaries. Setup
classification and score can still change materially between boundaries from
quote momentum, relative strength, spread, price, and extension. Volume
expansion, EMA, VWAP, and support/resistance cannot.

The currently exercised Robinhood MCP path reliably exposes exchange quote
timestamps, bid, ask, and last/current price, and is explicitly asked for
completed 5-minute regular-session historical bars with volume. Production
does not request completed 1-minute bars, and this audit did not verify reliable
1-minute or intrabar OHLCV support. It would be incorrect to invent those
capabilities or to claim that the MCP can never support them.

The existing score therefore behaves more like setup/context quality than a
standalone timing trigger. A split `SETUP ARMED → FAST ENTRY TRIGGER`
architecture is worth a future offline experiment only if it remains causal:
fresh bid/ask, breakout cross or retest/reclaim, quote-derived 5/10/15/30-second
returns/acceleration, spread, extension, edge, and RR. The AMD replay does not
justify enabling it now because the earlier attractive trigger lacked the
required causal volume confirmation.

## NBIS `PRICE_MONITORING_DEGRADED`

NBIS used the same quote batch as candidates; open-position checks run before
entry work, so it does not receive a weaker scheduling path. Its session quote
age was median `1.869 s`, p95 `6.336 s`, max `11.860 s`, with only `53.13%`
within two seconds. The final available mark had exchange timestamp
`18:43:40.692802Z`; its evaluation/stop-target check was about `0.613 s` later,
and the heartbeat was about `0.940 s` later. Marking used `MIDPOINT`, without a
fallback.

The degraded label has two causes:

1. The monitor requires provider mode `REALTIME_FAST`, while direct MCP quotes
   are labeled `DIRECT_MCP_UNVALIDATED`, even when fresh.
2. The global cycle is degraded if any watched symbol is stale; less-active
   universe members frequently carried old exchange timestamps.

Thus the label is partly provider-certification semantics and partly real data
freshness, not slow stop/target arithmetic. New timing records mark age,
stop/target check time, provider latency, fallback, and status per open position.
The audit deliberately did not relabel an unvalidated feed as real-time.

The post-fix connected sample confirms this attribution: NBIS mark age was
`1.612 s` median, `3.599 s` p95, and `4.175 s` max; its provider request was
`223.080 ms` median and `752.831 ms` p95. There were zero fallbacks. POSITION
work, which includes this check, was only `59.85 ms` median and `75.09 ms` p95.

## Infrastructure changes and before/after

No strategy threshold was changed. The following infrastructure changes were
made:

1. Added UTC wall-clock milestones and monotonic phase durations for provider,
   ingestion, cycle, features, setup, geometry, risk, pre-execution, and shadow
   creation. First-observed episode milestones survive restarts.
2. Added per-cycle and per-open-position timing to a dedicated fast-watcher
   latency journal.
3. Removed unchanged-episode full-file rewrites from the hot path.
4. Allowed signal evaluation to reuse already computed features and bars,
   avoiding duplicate compute without changing signal logic.
5. Limited historical batches to two concurrent calls while retaining the
   global maximum of four, leaving provider capacity for quotes. This does not
   increase request count.

| Metric | Before | After | Status |
|---|---:|---:|---|
| Unchanged episode persistence, per observation | `6.964 ms` median; `7.802 ms` p95 | `0.015 ms` median; `0.023 ms` p95 | Deterministic local benchmark |
| Estimated persistence for 12 unchanged symbols | `83.6 ms` | `0.185 ms` | Deterministic local benchmark |
| Quote wait behind a four-item history batch | `82.30 ms` median; quote third in completion order | `60.80 ms`; quote second | Serialized fake-provider benchmark; `26.1%` reduction |
| Connected median/p95 quote age at receive | `1.702 / 21.953 s` | `1.625 / 11.542 s` | Post-fix `n=392`; symbol mix differs, so not a controlled improvement claim |
| Connected median/p95 provider request | `206.124 / 414.544 ms` | `226.506 / 789.270 ms` | Short post-fix sample overlapped startup/slow refresh; max remained below `0.839 s` |
| Loop cadence | `2.001 s` median; `2.142 s` p95 | `2.0051 s` median; `2.0052 s` p95 | Post-fix cadence is tighter; precise phase timing now available |
| Local receive → evaluation | `1.374 ms` median; `1.776 ms` p95 | `1.355 ms` median; `2.336 ms` p95 | One eight-symbol observation reused a response `4.780 s` later, retained as an outlier |
| AMD decision latency | `0 s` eligibility → `.70`; `5.616 s` causal quote → classification | no comparable post-fix AMD episode | Not fabricated |
| AMD signal passes already overextended | `13/18` (`72.22%`) | no comparable signal pass | Episode-specific |
| AMD signal passes failing RR | `5/18` (`27.78%`) | no comparable signal pass | Episode-specific |
| Shadow execution | no actual AMD entry; benchmark `3.098 ms` median | unchanged logic, now instrumented | Not a bottleneck |

The concurrency benchmark is deliberately deterministic and synthetic; it
demonstrates queueing behavior, not live provider performance. The connected
sample validates the instrumentation and shows no multi-second quote-request
tail, but a longer, same-universe run spanning several five-minute boundaries
is still required for a statistically controlled “after” comparison.

## Requested conclusions

1. **Median provider quote latency:** `206.124 ms`.
2. **p95 provider quote latency:** `414.544 ms`.
3. **Median quote age at evaluation:** approximately `1.703 s` (`1.702 s` at receive plus `1.374 ms` median local dispatch).
4. **p95 quote age:** approximately `21.955 s` (`21.953 s` at receive plus `1.776 ms` p95 local dispatch; percentile sums are descriptive, not a jointly sampled percentile).
5. **Median SCALP cadence:** `2.001 s`.
6. **p95 cadence:** `2.142 s`.
7. **Full-universe refresh:** `209.134 ms` median, `298.122 ms` p90, `416.455 ms` p95, `4.163 s` max.
8. **Median setup detection proxy:** `1.413 s`; pure classification compute benchmark `0.0028 ms`.
9. **Median eligibility latency:** `0 s` among 29 episodes that became eligible.
10. **Median decision latency:** `92.075 s` among only two `.70` survivors; AMD was `0 s`. The sample is too small and censored for a systematic claim.
11. **Median risk/pre-execution:** `0.0226 ms` risk and `1.035 ms` pre-execution in local benchmarks; no live approved entry exists in this session.
12. **Median shadow execution:** `3.098 ms` production-sized local benchmark; p95 `3.591 ms`.
13. **AMD event-to-entry-ready:** no approved entry-ready event occurred. Counterfactually ignoring the failing geometry gates, causal post-close quote → decision-ready was `5.616 s`; the real strategy blocked it immediately.
14. **Price movement during that delay:** `-$0.175`, `-0.02807%`, `-2.807 bp`; no upward price was “lost.”
15. **AMD waterfall:** given above, with T0–T10 and all requested intervals.
16. **Is shadow execution slow?** No. **SHADOW TRADING IS NOT THE BOTTLENECK.**
17. **Is provider data slow?** Request median is reasonable, but request and returned-data tails are material. Old exchange timestamps, not ordinary local compute, dominate p95 age.
18. **Is polling slow?** A two-second cadence is meaningful for a ≤180-second strategy and can add nearly two seconds, though AMD's largest identified delay was completed-bar availability plus provider contention.
19. **Does rendering block?** No material effect in the deterministic benchmark (`~0.077 ms` median increment).
20. **Does POSITION block SCALP?** Slow LLM/news/analysis does not run inline. Shared MCP historical traffic caused quote-tail contention; it has been bounded. Local exit work intentionally precedes entry work and is now timed.
21. **Do 5m bars cause a horizon mismatch?** Yes.
22. **Is `.70` systematically late?** Not established. Only two eligible episodes reached it; AMD reached it immediately and one EMA9 continuation took `184.150 s`.
23. **Is `ENTRY_OVEREXTENDED` mostly caused by late decisions?** Not established. It represented `13/18` AMD signal-pass observations, but AMD was already overextended at the earliest causally valid post-close point and became less extended during the measured delay.
24. **Are RR failures caused by late entry?** Not solely; the same AMD stop/target geometry could not meet RR at the first post-close causal ask.
25. **Infrastructure fixes:** precise timings, dedicated latency journal, hot-path persistence reduction, feature reuse, and bounded history concurrency.
26. **Before/after:** reproducible local comparisons and the short connected post-fix sample are above. The latter validates phase attribution but is not long enough for a controlled market-data claim.
27. **Fast-entry architecture:** recommended for further offline causal testing, not production activation from this evidence.
28. **Prototype comparison:** the two causal offline entries and the explicitly invalid raw-cross counterfactual are above.
29. **`PRICE_MONITORING_DEGRADED`:** direct-feed mode is intentionally unvalidated and the provider often returns old exchange timestamps; stop/target arithmetic is not the bottleneck.
30. **Files changed:** listed below.
31. **Tests added:** latency milestone persistence, phase timing, direct-MCP history concurrency, fast-watcher latency records, monitoring timing, execution latency, and feature-reuse coverage.
32. **Full suite:** `534 passed in 2.87s` on the final run.
33. **Thresholds unchanged:** confirmed—score `.70`, volume `1.20`, freshness `2 s`, spread `.10%`, net edge `.05%`, RR `1.10`, max hold `180 s`, and extension `.30%` remain unchanged.
34. **Shadow-only safety unchanged:** confirmed—`MODE=SHADOW_TRADING`, both live-execution flags false, and the kill switch remains blocked.
35. **Broker/account mutation:** none. The audit read only local state/logs and used deterministic local fakes/benchmarks; it made no broker orders, previews, cancels, modifications, or account changes.

## Files changed for the latency work

- `config.py`
- `robinhood_mcp/client.py`
- `shadow/execution.py`
- `strategies/scalp/diagnostics.py`
- `strategies/scalp/execution.py`
- `strategies/scalp/models.py`
- `strategies/scalp/runtime.py`
- `strategies/scalp/setup.py`
- `strategies/scalp/signals.py`
- `watcher/fast_watcher.py`
- `tests/test_direct_mcp.py`
- `tests/test_fast_watcher.py`
- `tests/test_scalp_observability.py`
- `tests/test_scalp_strategy.py`
- `scripts/scalp_latency_audit.py`
- `docs/scalp-latency-amd-observations-2026-09-24.csv`
- `docs/scalp-latency-metrics-2026-09-24.json`
- `docs/scalp-latency-postfix-sample-2026-09-24.json`
- `docs/scalp-latency-audit-2026-09-24.md`

Other dirty-worktree files predated or belong to the broader lifecycle and
observability work and were preserved rather than overwritten.
