# Zero-trade attainability audit

Evidence window: the authenticated 2026-09-23 shadow artifacts in `logs/` and
`state/`. All replay and counterfactual work was local and chronological. No
Robinhood order, preview, cancellation, modification, or account mutation was
called.

## POSITION

### Formula and defect

The logged production formula was:

```text
0.35 technical + 0.20 news + 0.10 sector + 0.10 market + 0.25 qualitative
```

The qualitative value itself is:

```text
0.30 setup_quality
+ 0.25 catalyst_quality
+ 0.30 continuation_probability
+ 0.15 (1 - conflicting_evidence_severity)
```

`technical_confidence` is reported but is not an additional combined-score
term. Sector and market LLM biases map `SUPPORTS=.8`, `NEUTRAL=.5`,
`CONFLICTS=.2`, `UNKNOWN=.4`. News starts at neutral `.5` and moves toward the
sentiment anchor (`VERY_POSITIVE=1`, `POSITIVE=.8`, `NEUTRAL/MIXED=.5`,
`NEGATIVE=.2`, `VERY_NEGATIVE=0`, `UNKNOWN=.4`) in proportion to
`.4 importance + .3 freshness + .3 source_quality`; unavailable news is `.35`.

All inputs use `[0,1]`; contextual `0.5` means neutral. The defect was not the
qualitative normalization. It was that contextual inputs occupied 65% of a
strategy intended to be technically led. In addition, missing LLM output was
scored as `0.0` even though it was separately vetoed, conflating missing data
with negative evidence.

Production now uses:

```text
0.70 technical + 0.10 news + 0.05 sector + 0.05 market + 0.10 qualitative
```

Missing qualitative data is represented as neutral `0.5`, while the existing
`LLM_REASONING_UNAVAILABLE` veto still fails closed. Inputs are validated as
finite values in `[0,1]`, and each contribution is persisted. Thresholds remain
watch `0.60`, trade `0.72`.

IONQ reconstructed exactly as:

```text
old = .35(.615) + .20(.35) + .10(.20) + .10(.20) + .25(.195)
    = .374
new = .70(.615) + .10(.35) + .05(.20) + .05(.20) + .10(.195)
    = .505
```

IONQ remains below watch because it was below VWAP, had bearish MACD, bearish
market and sector context, and low technical confidence. The fix does not force
it through.

BPRE reconstructed exactly as:

```text
old = .35(.923) + .20(.35) + .10(.20) + .10(.20) + .25(.217)
    = .4873 -> .487
new = .70(.923) + .10(.35) + .05(.20) + .05(.20) + .10(.217)
    = .7228 -> .723
```

This is the clearest scale mismatch: a 0.923 qualified technical setup could
not reach watch under the old weighting, while the corrected technically led
score can reach the trade threshold without weakening a hard gate.

### Session distributions

There were 34 candidate-cycle observations. Logged combined scores were:

| Metric | Value |
|---|---:|
| min / p10 / p25 | .119 / .119 / .283 |
| median / p75 | .302 / .362 |
| p90 / p95 / max | .432 / .432 / .487 |
| >=.40 / >=.50 | 17.6% / 0% |
| >=.60 / >=.65 / >=.70 / >=.72 / >=.75 | 0% / 0% / 0% / 0% / 0% |

Technical scores were min `.000`, p10 `.000`, p25 `.462`, median `.462`, p75
`.615`, p90 `.769`, p95 `.769`, max `.923`. Qualitative scores were min `.038`,
p10 `.038`, p25 `.075`, median `.120`, p75 `.149`, p90 `.210`, p95 `.210`, max
`.217`. Thus the observed qualitative context was genuinely poor, not neutral;
the old 25% qualitative weight made it an oversized penalty.

Replaying the same components with the corrected production weights yields min
`.059`, p10 `.059`, p25 `.383`, median `.390`, p75 `.500`, p90 `.614`, p95
`.614`, max `.723`; 17.6% reach watch and 2.9% reach trade. This does not claim
those rows would all enter: hard gates, confirmation, fresh geometry, risk and
portfolio checks still apply. It proves the score scale is now compatible with
the unchanged thresholds.

Deterministic tests cover poor, moderate, strong-neutral, negative-context and
very-strong cases. Strong-neutral reaches watch; a complete strong setup can
reach trade. Negative catalysts and every hard gate remain vetoes.

## SCALP

The production score remains:

```text
.25 clip(return_3 / .003)
+ .20 I(price > VWAP)
+ .15 clip((EMA9 slope / price) / .001)
+ .15 clip(relative_strength_SPY / .002)
+ .15 clip((completed_bar_volume_expansion - 1) / .5)
+ .10 clip(1 - spread / .001)
- .20 clip((entry_extension - .003) / .003)
```

The strong realistic production fixture scores `.959726`, passes net edge and
RR, and opens a local shadow position. Therefore `.70` is mathematically
reachable; it was not lowered.

The session contained 1,320 symbol observations, 726 fresh quotes, 711 spread
passes, 706 usable micro-bar observations, 126 volume-expansion funnel passes,
406 setup detections, 44 eligible setup observations, 37 open episodes, zero
score passes, and zero entries. Setup detections were MICRO_BREAKOUT 64,
VWAP_RECLAIM 124, EMA9_CONTINUATION 141, MICRO_PULLBACK 77, MOMENTUM_BURST 0;
914 observations were unclassified.

The setup predicates are alternatives in priority order, so no setup requires
mutually contradictory price relationships. A breakout requires price above
the prior completed-bar high and >=1.20 completed-bar volume expansion; reclaim
requires a prior close below VWAP and current price at/above VWAP; continuation
requires price >= EMA9 > EMA20 with rising EMA9; pullback requires price within
0.15% of EMA9 with EMA9 > EMA20; burst requires >0.10% micro return plus
positive volume acceleration. Extension, stop, edge and RR are downstream and
do not participate in classification. The observed zero MOMENTUM_BURST count
means the session did not satisfy that last alternative after higher-priority
matches, not that its predicate is impossible.

Valid signal scores: min `.000`, p10 `.091`, p25 `.109`, median `.136`, p75
`.237`, p90 `.360`, p95 `.408`, max `.608`; >=.50 was 0.70%, >=.60 was 0.42%,
and >=.65/70/75/80 were all 0%. Eligible breakout and reclaim episodes were
usually missing immediate positive quote momentum and strong relative strength.
Those are real score inputs, not unavailable values silently changed to zero.

Of the 37 eligible score failures, offline geometry shows 34 had valid stop
distance, 27 sufficient net edge, and 31 gross RR >=1.10. No entry was attempted.
Per-episode score components, threshold margin, friction, stop, target, gross
and net RR, and downstream counterfactuals are now emitted by the session
analytics.

Volume expansion distribution: min `.212`, median `.917`, p75 `1.376`, p90
`2.549`, max `2.935`; >=.75 62.0%, >=1.00 41.8%, >=1.10 39.5%, >=1.20 34.8%,
>=1.30 26.4%, >=1.50 20.4%. The 1.20 threshold is selecting bursts and remains
unchanged.

The old `relative_volume` field is a compatibility alias for latest completed
short-bar volume divided by the preceding completed-bar mean. It is now called
`VOLUME_EXPANSION` in production diagnostics. Rejections are
`VOLUME_EXPANSION_BELOW_MINIMUM`, `VOLUME_DATA_STALE`, or
`VOLUME_DATA_UNAVAILABLE`. `EXECUTION_LIQUIDITY` is independently defined by a
fresh valid bid/ask and acceptable spread. The ambiguous
`INSUFFICIENT_LIQUIDITY` production reason is removed.

Gate reasons are now emitted in production order. Later calculations are kept
for offline counterfactual inspection, but an unclassified or volume-filtered
symbol is no longer also counted as a signal-score, edge, stop and RR rejection.
Repeated uses of volume, spread, extension, momentum and VWAP are documented;
no hard safety gate was removed.

Quote age was min `.114s`, median `1.771s`, p75 `4.530s`, p90 `36.192s`, p95
`64.106s`, max `260.899s`; 20.5% were <=1s, 55.0% <=2s, 67.3% <=3s, and 76.1%
<=5s. Poll latency was median `.200s`, p95 `.321s`, max `2.332s`, so the long
tail is primarily exchange-update cadence/cache reuse, not request latency.
Liquid seeds typically updated every 2-5s. BPRE had only four unique exchange
updates (median gap 55.9s), IRTC eight (max gap 261.7s), and QMCO twelve (median
7.1s). The 2s entry freshness threshold remains unchanged.

Scalp runs in its own `shadow-fast-watcher` thread, independent of the slow LLM
cycle. POSITION entry already performs a fresh pre-execution refresh and
revalidates bid, ask, spread, entry, stop, target, RR and required hard gates,
so 30-50s LLM latency cannot authorize stale entry geometry.

## Safety and verification

- `MODE = SHADOW_TRADING`.
- `LIVE_TRADING_ENABLED = False`.
- `ROBINHOOD_EXECUTION_ENABLED = False`.
- The local kill switch remains `{"trading_blocked": true}`.
- SCALP remains shadow-only and defaults disabled unless the human opts in.
- No threshold was changed.
- No real order/account mutation occurred.
- Full suite: `497 passed`.
