# Runtime observability and NBIS extension forensics

Date: 2026-09-24  
Mode: `SHADOW_TRADING`  
Scope: local runtime output and chronological, offline analysis of persisted provider observations

## Executive conclusion

`ENTRY_OVEREXTENDED` correctly prevented the observed NBIS chase. NBIS was classified and made eligible at the first usable observation; the first episode did not wait for classification. It waited 178.284 seconds for its score to move from 0.698607 to 0.701242. By then it was 1.359% above the production EMA9 reference and 0.700% above the actual prior-high breakout structure. Even if extension were bypassed offline, all seven score-passing observations failed the 1.10 gross-RR requirement; the last two also had negative expected net edge. The first group then mean-reverted over the observable 30- and 60-second horizons.

The protection is therefore doing useful work. There is also a real architecture issue: the production extension reference is always EMA9, regardless of setup. That reference is structurally aligned for `EMA9_CONTINUATION` and `MICRO_PULLBACK`, but not for `MICRO_BREAKOUT` or `VWAP_RECLAIM`. For NBIS, using the actual breakout level would reduce the reported extension magnitude but would not change the rejection: every score pass remained beyond 0.30% from the relevant breakout level (the first post-refresh pass was almost exactly at it by mark and beyond it by executable ask).

No production reference or threshold was changed. New diagnostics now record both the production reference and the setup-structural reference so a future, separately authorized strategy change can be evaluated without conflating it with this terminal refactor.

## 1–7. Terminal changes

1. **Normal layout.** `python runner.py --loop --scalp-shadow` now starts with `TRADER — SHADOW MODE | SHADOW ONLY | LIVE EXECUTION BLOCKED`, then emits a compact state-diff dashboard. The header shows UTC time, market state, provider connection, safety, equity, cash, open positions, and realized/unrealized P&L. Separate POSITION and SCALP panels show counts, open positions, at most three candidates, and a bottleneck.

2. **Debug behavior.** `--debug` enables the former full funnel, micro-bar, component, lifecycle, and per-candidate trace stream. `--scalp-debug` remains a backward-compatible verbose alias. Diagnostics remain persisted in both modes.

3. **CLI changes.** The supported commands are:

   - `python runner.py --loop --scalp-shadow` — normal dashboard
   - `python runner.py --loop --scalp-shadow --debug` — verbose diagnostics
   - `python runner.py --strategy-status` — readable one-shot summary
   - `python runner.py --scalp-drilldown NBIS` — latest persisted full candidate trace (the requested `--scalp-debug NBIS` equivalent)

4. **Suppressed repetition.** Normal mode no longer prints every eligible candidate, score component, full funnel, micro-bar status, reason-count block, setup-count block, or entry-revalidation payload on every poll. A semantic fingerprint suppresses identical dashboards. Time alone does not trigger output; candidate score/state/reason, freshness class, funnel depth, position/P&L, and bottleneck changes do.

5. **Always-visible events.** Shadow POSITION/SCALP entries and entry-attempt rejections, SCALP exits (including stop, target, time, and recovery reasons), candidate state events, connection failures, and safety-start refusal remain immediate output. These are local events only.

6. **Readable reasons.** Normal output maps codes such as `ENTRY_OVEREXTENDED` to “price already moved too far to chase,” `SIGNAL_SCORE_BELOW_THRESHOLD` to “signal score below 0.70,” and similarly explains stale quotes, wide spreads, insufficient volume expansion, stale bars, stale episodes, invalid stops, insufficient edge, and poor RR. Drilldown/debug retains the machine code.

7. **Depth-aware bottleneck.** The selector first considers candidates that reached `eligible_micro_signals`, then chooses the deepest recorded blocking stage. Universe attrition is reported separately. Thus one `ENTRY_OVEREXTENDED` after a score pass outranks six early volume filters. If no candidate reaches eligibility, the dashboard explicitly says so and reports the primary universe filter instead. POSITION separately reports its active candidate blocker or “NONE” when a position is open.

## 8–16. Exact extension and timing findings

8. **Production formula.** In `ScalpSignalEngine.features`, `entry_extension = candidate mark price / EMA9 - 1`. The candidate price is `FastQuote.mark_price` (normally bid/ask midpoint), not the executable ask. EMA9 comes from completed historical bars. The hard gate fires only after the signal score passes and when `entry_extension > 0.003`. It is percentage-based, not ATR- or volatility-adjusted, has one threshold for every setup, and is not setup-specific.

   The score also applies `-0.20 × clamp((extension - 0.003) / 0.003, 0, 1)`. The maximum -0.20 penalty is reached at 0.60% extension.

9. **NBIS production references.** Before the 17:00 UTC bar refresh, EMA9 was **244.27011688976484**, sourced from the history generation whose latest completed bar closed at 16:55:00 UTC. After refresh, EMA9 was **244.94529043021785**, with latest completed close 17:00:00 UTC.

10. **Every NBIS score pass.** “From start” uses the first usable mark in that episode (246.89 and 247.34). “From breakout” uses the actual prior-completed-high classification trigger reconstructed from the persisted candles (245.87 and 246.56). Production extension uses mark/EMA9; current ask is listed separately.

| UTC | Episode suffix | Episode start | EMA9 reference | Ask | Production extension | Score | From start | From breakout | Quote age | Micro-bar age | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 16:59:55 | 9e44fa | 16:56:56 | 244.27012 | 247.66 | 1.359% | 0.701242 | +0.284% | +0.700% | 1.068s | 295.060s | `ENTRY_OVEREXTENDED` |
| 16:59:57 | 9e44fa | 16:56:56 | 244.27012 | 247.70 | 1.380% | 0.705897 | +0.304% | +0.720% | 1.252s | 297.065s | `ENTRY_OVEREXTENDED` |
| 16:59:59 | 9e44fa | 16:56:56 | 244.27012 | 247.70 | 1.390% | 0.734527 | +0.314% | +0.730% | 1.056s | 299.071s | `ENTRY_OVEREXTENDED` |
| 17:00:01 | 9e44fa | 16:56:56 | 244.27012 | 247.74 | 1.400% | 0.731141 | +0.324% | +0.740% | 0.856s | 301.200s | `ENTRY_OVEREXTENDED` |
| 17:00:03 | 9e44fa | 16:56:56 | 244.27012 | 247.69 | 1.390% | 0.749371 | +0.314% | +0.730% | 1.246s | 303.078s | `ENTRY_OVEREXTENDED` |
| 17:01:43 | 7e7e52 | 17:00:06 | 244.94529 | 247.40 | 0.961% | 0.714150 | -0.016% | +0.300% | 1.076s | 103.368s | `ENTRY_OVEREXTENDED` |
| 17:01:45 | 7e7e52 | 17:00:06 | 244.94529 | 247.45 | 0.978% | 0.711054 | -0.000% | +0.316% | 1.489s | 105.515s | `ENTRY_OVEREXTENDED` |

11. **Threshold.** The unchanged maximum extension is **0.003 = 0.30%**. All seven values above exceed it. With breakout structure instead of EMA9, the 17:01:43 mark is effectively on the boundary (+0.3001%), while its executable ask is +0.3407%; the next mark is +0.3164%. The observed decision remains reject.

12. **Timeline.** For episode `…9e44fa`, T0 is the first recorded provider quote already above the prior high at 16:56:56.243; T1 classification, T2 volume pass, T3 episode creation, and T4 eligibility occurred at 16:56:56.776 (0.533s after T0 and mutually simultaneous in the evaluation). T5 score pass and T6 extension rejection both occurred at 16:59:55.060. T5−T4 was **178.284s**; T6−T5 was **0s**.

   For episode `…7e7e52`, T0 is the new completed-bar boundary at 17:00:00; T1/T3 occurred after the async refresh at 17:00:06.098 (**6.098s**), T2/T4 at 17:00:07.057 (**0.959s** later; the first observation had a wide spread), and T5/T6 at 17:01:43.368. T5−T4 was **96.311s**; T6−T5 was **0s**.

13. **Was detection late?** Setup detection was not late: the first usable observation immediately classified, passed volume, created the episode, and was eligible. Entry quality was late in the first episode: the score crossed only 1.716s before the 180-second maximum intended hold. Importantly, the first score was already 0.698607—only 0.001393 below threshold—but the setup was already extended by 1.073% versus EMA9 and about 0.415% versus its breakout trigger. There was no early, valid-extension opportunity in the recorded episode.

14. **Five-minute dependency.** Completed 5-minute data supplies EMA/VWAP/volume/context and can change only on a boundary. The refresh from 17:00:03.078 to 17:00:05.956 took **2.877362s**, returned 14/14 histories, reset median bar age to 6.098s, and reported event-loop blocking of about 0.000001s. It did not synchronously stall the watcher. NBIS stayed `MICRO_BREAKOUT`; volume stepped from 1.45447× to 1.61765× and EMA extension stepped from ~1.39% to ~0.96%. The score initially reset lower and did not pass for another 97 seconds. Therefore the specific NBIS delay was not “waiting for a bar to become detectable,” although the fixed context does create coarse boundary changes.

15. **Reference validity.** One generic EMA9 reference is used for `MICRO_BREAKOUT`, `EMA9_CONTINUATION`, `VWAP_RECLAIM`, `MICRO_PULLBACK`, and `MOMENTUM_BURST`. It is structurally appropriate for the EMA9 setups, but not for breakout (prior completed high/setup trigger is the natural reference), VWAP reclaim (VWAP), or momentum burst (a recent price anchor). NBIS proves the magnitude can be overstated by EMA9. It does not prove a missed viable trade because the breakout-relative values still fail and downstream RR is poor.

16. **Double protection.** Extension is mechanically used twice. Every NBIS pass received the maximum **-0.20** score penalty: pre-penalty scores were 0.901242–0.949371 for the first episode and 0.911054–0.914150 for the second. The surviving final scores then encountered the non-negotiable hard gate. The code explicitly separates attractiveness scoring from hard gates, so this is an intentional soft-plus-hard defense, not an accidental duplicate condition. No part was removed.

## 17–21. Offline counterfactual and architecture

17. **Downstream feasibility.** Production computed geometry for diagnostics but correctly did not evaluate later gates in-funnel after extension failed. Offline values show:

| UTC | Stop | Expected move | Friction | Net edge | Target | Gross RR | Net RR | Feasible? |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 16:59:55 | 244.27012 | 0.2835% | 0.1065% | 0.1770% | 248.36218 | 0.207 | 0.129 | No: RR |
| 16:59:57 | 244.27012 | 0.2794% | 0.0985% | 0.1810% | 248.39210 | 0.202 | 0.131 | No: RR |
| 16:59:59 | 244.27012 | 0.2895% | 0.0783% | 0.2113% | 248.41717 | 0.209 | 0.153 | No: RR |
| 17:00:01 | 244.27012 | 0.3078% | 0.0904% | 0.2174% | 248.50249 | 0.220 | 0.155 | No: RR |
| 17:00:03 | 244.27012 | 0.2977% | 0.0702% | 0.2275% | 248.42726 | 0.216 | 0.165 | No: RR |
| 17:01:43 | 244.94529 | 0.1293% | 0.1309% | -0.0015% | 247.72000 | 0.130 | -0.002 | No: edge and RR |
| 17:01:45 | 244.94529 | 0.1091% | 0.1389% | -0.0298% | 247.72000 | 0.108 | -0.029 | No: edge and RR |

18. **Post-signal behavior.** These are overlapping observations from one move, not seven independent trades. For the first five passes, 30-second returns were **-0.269%, -0.289%, -0.297%, -0.309%, -0.299%**; 60-second returns were **-0.182%, -0.204%, -0.230%, -0.220%, -0.224%**. Maximum favorable excursion over available data was only 0.040%, 0.020%, 0.010%, 0%, and 0%; maximum adverse excursion through 108–116 seconds was about -0.370% to -0.410%. That is mean reversion, not continuation. The two later passes have only 5.6–7.7 seconds of follow-up, with at most +0.049% MFE. Exact 120- and 180-second returns are **UNAVAILABLE** because provider logging ended 116 seconds after the first pass; no value was invented or substituted from a later bar.

19. **Earlier entry comparison.** Across 145 classified NBIS observations, **zero** had production extension ≤0.30%. The first episode began at score 0.698607 with 1.073% EMA extension; later scores fell near 0.27–0.44 before recovering. The second began with 0.529553 and 0.978% extension. Thus the hypothesized “valid extension at .67, invalid later at .74” pattern did not occur in this NBIS sample.

20. **Does scoring lag entry timing?** Partly. Five-minute indicators appropriately describe context, but `return_3` is replaced when possible by `quote_microstructure.short_price_momentum`, whose tape is retained for up to the 180-second max-hold window. That “short” momentum can therefore mature across nearly the whole intended trade horizon. NBIS crossed late as fast momentum/relative-strength contributions recovered. A future design should separate stable 5-minute context qualification from a genuinely short entry-timing score/window. The current evidence supports research, not an automatic score-threshold reduction.

21. **Architecture change.** Runtime observability required a new read-only projection/diff layer only; strategy decisions are untouched. A setup-specific extension-reference change is recommended for a separate controlled change, with replay/evaluation first. It was not made here because this task first required establishing semantics and preserving decision behavior during the output refactor.

## 22–28. Delivery and safety

22. **Files modified in this pass.** `runner.py`; `trading_runtime/observability.py`; `watcher/fast_watcher.py`; `watcher/candidate_watcher.py`; `strategies/scalp/runtime.py`; `strategies/scalp/signals.py`; `strategies/scalp/models.py`; `strategies/scalp/diagnostics.py`; `strategies/scalp/execution.py`; `strategies/scalp/position.py`; `tests/test_runtime_observability.py`; `tests/test_scalp_strategy.py`; `tests/test_scalp_observability.py`; `tests/test_runner.py`; and this report.

23. **Tests.** Added deterministic coverage for normal rendering, open POSITION rendering, SCALP candidate rendering, reason mapping, semantic suppression, depth-aware universe-versus-candidate bottlenecks, candidate drilldown/CLI, normal-mode event visibility, debug/non-debug decision equivalence, production-versus-structural extension references for breakout/EMA9 continuation/VWAP reclaim, valid extension, hard extension rejection, and penalty math. Existing episode-timeline and five-minute-boundary suites remain active.

24. **Full suite.** `PYTHONPATH=. pytest -q` passes **520 tests** after the final rerun.

25. **Thresholds.** Unchanged: signal 0.70, volume expansion 1.20, quote freshness 2s, spread 0.10%, net edge 0.05%, RR 1.10, max hold 180s, and all POSITION thresholds.

26. **POSITION regression.** Existing POSITION entry/revalidation/risk/execution coverage remains green, including the same code path that produced the PRGO shadow entry. The dashboard consumes portfolio/context snapshots after decisions and cannot approve or execute a trade.

27. **Safety.** `MODE=SHADOW_TRADING`, `LIVE_TRADING_ENABLED=False`, `ROBINHOOD_EXECUTION_ENABLED=False`, the local kill switch remains blocked, and SCALP remains shadow-only.

28. **External mutation.** No Robinhood order, preview, modification, cancellation, transfer, account change, or other broker/account mutation was attempted. The investigation read local persisted observations and ran deterministic local tests only.
