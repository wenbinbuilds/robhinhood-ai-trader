# SCALP_V1 next-bottleneck audit

Evidence set: `logs/scalp_diagnostics.jsonl`, 262 discovery cycles and 2,880
candidate observations from the 2026-09-18 shadow session. Counts are repeated
runtime observations unless explicitly labeled as unique episodes. No live request
was made for this audit.

1. **Exact liquidity formula.** The production fallback is
   `latest completed 5m bar volume / mean(previous up to five completed 5m bar volumes)`.
   The latest bar is excluded from the baseline. With six available bars this compares
   five minutes of current volume with the preceding 25-minute rolling mean. The hard
   threshold remains 1.20.

2. **Provenance.** The preferred source is a provider-supplied relative-volume field
   when present. In this session the persisted source was
   `COMPLETED_MICRO_BAR_RATIO`; momentum-scanner relative volume is a last fallback.
   New observations persist source, source timestamp, current volume, baseline volume,
   baseline bar count, timeframe, completed-only flag, formula, freshness, fallback use,
   threshold, and final status.

3. **Units and timeframe.** The active fallback is internally consistent: numerator and
   denominator are share volume from completed five-minute candles. Mixed candle
   timeframes are now rejected from a feature series. Robinhood describes a candlestick
   volume bar as volume traded during its corresponding time period, which supports the
   five-minute interpretation ([Robinhood technical indicators](https://robinhood.com/us/en/support/articles/viewing-indicators/)). It is not daily volume divided by average
   daily volume. It is also not same-time-of-day normalized; it is a short rolling
   expansion measure. No replacement formula was introduced.

4. **Relative-volume distribution.** Across 2,880 observations: min 0.123, p10 0.520,
   p25 0.692, median 0.856, p75 1.195, p90 2.874, p95 5.986, max 5.986.
   Percent at or above: 0.75 = 72.7%, 1.00 = 33.4%, 1.10 = 25.3%, 1.20 = 20.3%,
   1.30 = 19.3%, and 1.50 = 11.1%.

5. **Per-symbol numeric liquidity pass rates.** These rates describe the intrinsic
   relative-volume value, before the sequential spread/quote funnel:

   | Symbol | Observations | RV median | RV p90 | RV >=1.20 |
   |---|---:|---:|---:|---:|
   | AAPL | 262 | 0.520 | 0.881 | 0.0% |
   | AMD | 262 | 0.765 | 0.838 | 0.0% |
   | AMZN | 262 | 0.505 | 0.963 | 0.8% |
   | GOOGL | 262 | 1.355 | 1.355 | 64.5% |
   | HALO | 262 | 2.874 | 2.874 | 90.8% |
   | META | 262 | 0.900 | 1.042 | 0.8% |
   | MSFT | 262 | 0.856 | 0.856 | 0.0% |
   | MSTR | 260 | 1.195 | 1.195 | 0.0% |
   | NCI | 262 | 5.986 | 5.986 | 65.3% |
   | NVDA | 262 | 0.840 | 0.935 | 0.0% |
   | TSLA | 262 | 0.692 | 0.692 | 0.8% |

6. **Near misses versus low activity.** Under the current rolling-expansion definition,
   most failures were not near 1.20: 66.6% were below 1.00 and 74.7% below 1.10.
   Only 5.0 percentage points sat between 1.10 and 1.20. MSTR at 1.195 was the clear
   repeated near-miss; most large-cap seed symbols were materially below their preceding
   25-minute baseline. That supports “currently low short-term expansion,” but does not
   validate this rolling measure as the ideal intraday RVOL definition.

7. **Exact score formula.** With `clip(x)=min(1,max(0,x))`:

   `score = .25*clip(return_3/.003)`
   `+ .20*(price_vs_vwap > 0)`
   `+ .15*clip((ema9_slope/price)/.001)`
   `+ .15*clip(relative_strength_spy/.002)`
   `+ .15*clip((volume_expansion-1)/.5)`
   `+ .10*clip(1-spread/.001)`
   `- .20*clip((entry_extension-.003)/.003)`.

8. **Component behavior.** For 740 valid observations with feature provenance, mean
   normalized value / mean weighted contribution were: momentum 0.253/0.0633, VWAP
   0.364/0.0727, EMA slope 0.169/0.0254, relative strength 0.287/0.0430, volume
   expansion 0.040/0.0060, spread quality 0.793/0.0793, and extension penalty
   0.063/-0.0127. Volume expansion was clamped at zero 72.6% of the time; EMA slope
   64.2%; momentum and relative strength each 52.6%. The full raw, normalized, weight,
   contribution, clamp, missing, and fallback breakdown is now persisted per decision.

9. **Score distribution.** Across 2,309 valid scores: min 0.040, p10 0.088, p25 0.108,
   median 0.205, p75 0.250, p90 0.388, p95 0.681, max 0.820.

10. **Threshold attainment.** 61/2,309 observations, or 2.64%, reached 0.70. Rates at
    or above 0.50/0.60/0.65/0.75/0.80 were 8.36%/7.54%/6.32%/1.39%/0.17%.

11. **Compression finding.** A score over 0.70 is demonstrably attainable, so the
    mathematical range is not broken. The empirical distribution is strongly compressed
    toward low scores because several components are frequently clamped at zero,
    especially volume expansion. The eligible MOMENTUM_BURST episode was not a 0.69
    near-miss; it stayed around 0.50–0.51.

12. **Overlapping penalties.** Volume affects the liquidity gate, volume score, and
    MICRO_BREAKOUT classification. Spread affects a hard gate, score, and expected cost.
    Extension affects score and a hard gate. Momentum affects setup, score, and projected
    move. VWAP affects setup, score, and stop geometry. These overlaps were not removed.

13. **Scores by setup.** Repeated-observation results:

   | Setup | Detected | Eligible | Median | p90 | Max | >=0.70 | Attempts |
   |---|---:|---:|---:|---:|---:|---:|---:|
   | MICRO_BREAKOUT | 26 | 0 | 0.400 | 0.400 | 0.400 | 0 | 0 |
   | VWAP_RECLAIM | 98 | 1 | 0.388 | 0.392 | 0.396 | 0 | 0 |
   | EMA9_CONTINUATION | 447 | 1 | 0.689 | 0.766 | 0.820 | 60 | 0 |
   | MICRO_PULLBACK | 695 | 1 | 0.106 | 0.295 | 0.706 | 1 | 1 |
   | MOMENTUM_BURST | 317 | 19 | 0.214 | 0.230 | 0.513 | 0 | 0 |
   | UNCLASSIFIED | 0 | 0 | 0.150 | 0.250 | 0.426 | 0 | 0 |

14. **MOMENTUM_BURST near miss.** All 19 eligible score-rejected observations were one
    GOOGL episode (`SCALP-GOOGL-343ee4f9e70b689c5853b50c`) from 16:14:16–16:15:00Z.
    RV was 1.2166, quote ages 0.42–1.79s, spread 0.0086–0.0229%, and scores
    0.4992–0.5135 (median 0.5106), leaving margins of -0.2008 to -0.1865. A representative
    0.5049 score contributed momentum 0.1837, VWAP 0, EMA slope 0.0274, relative strength
    0.1460, volume 0.0650, spread 0.0828, and approximately zero extension penalty.
    Negative price/VWAP alignment and modest EMA/volume contributions explain the miss.

15. **Offline edge for that episode.** All 19 observations exceeded the unchanged 0.05%
    net-edge minimum. Expected net edge ranged 0.110%–0.159%, median 0.124%. Failed-score
    candidates remained blocked; these calculations did not change gate order.

16. **Offline geometry for that episode.** Gross RR ranged 1.00–1.83, median 1.22;
    15/19 met the unchanged 1.10 gross-RR gate. Net RR ranged 0.65–1.32, median 0.79;
    only 1/19 reached 1.10 net RR. Score was the first blocker, but geometry/economic
    quality was not uniformly strong behind it.

17. **Did refresh block quotes?** Yes. The old implementation called the history batch
    synchronously inside `ScalpRuntime.on_quotes`. Both recorded refresh cycles had zero
    fresh quotes and 11 stale quotes, while the next cycles recovered.

18. **Refresh duration.** The two saved boundary batches took 2.989s and 2.834s. That is
    longer than the unchanged two-second quote-age limit and explains the boundary spike.
    New timing records include start/end, duration, requested symbols, provider latency,
    per-symbol quote age before/after, crossings, scheduling/blocking time, and watcher
    loop delay.

19. **Normal quote-age distribution.** Across the whole session: median 1.656s, p75
    3.410s, p90 22.56s, p95 66.78s, p99 212.27s, max 241.05s. Percent <=1/2/3/5s was
    24.7%/59.0%/72.4%/81.5%. Excluding recorded refresh cycles, fresh-quote pass rate was
    59.4%, median 1.643s, p95 66.82s. Therefore there is also a broader provider/update
    staleness issue; not every stale quote was caused by history refresh.

20. **Refresh-cycle quote ages.** The two refresh cycles contained 22 observations:
    fresh pass rate 0%, stale count 22, median age 4.943s, p95 13.817s. Compared with the
    59.4% normal-cycle rate, the refresh-specific spike is unambiguous.

21. **Scheduling change.** History now runs in one dedicated background worker. The fast
    loop schedules it and immediately continues with the current quotes. Completed data
    is validated while readers retain the old generation, then published under one lock;
    each signal cycle captures one atomic multi-symbol snapshot. Deterministic tests show
    the scheduler returns while a blocked provider call remains in progress. Production
    improvement must be confirmed in the next authenticated shadow session.

22. **Provider impact.** Request frequency is unchanged: one deduplicated batch of due
    scalp symbols plus SPY/QQQ at a five-minute boundary, with the existing retry throttle.
    No per-symbol polling loop or per-quote history fetch was added.

23. **Universe quality.** GOOGL was the useful liquid/tight-spread contributor. HALO and
    NCI had strong rolling volume but median spreads of 0.208% and 2.05%, above the 0.10%
    gate. MSTR was an RV near-miss at 1.195. The liquid mega-cap seeds generally had tight
    spreads but sub-1.0 rolling volume expansion during this window. Thus composition
    contributes in two ways: quiet mega-caps fail RV, while two high-RV scanner symbols
    fail spread. Existing logs did not persist realized volatility; it is now included in
    each market observation for future sessions. The universe itself was not changed.

24. **Offline counterfactuals.** Among observations already through micro-bar and setup
    gates, RV 1.20/1.10/1.00 produced 21/53/139 observations and 3/4/6 unique episodes.
    Median gross RR fell from 1.22 to 0.81 to 0.50; gross-RR pass rate fell from 76.2% to
    30.2% to 11.5%. Lower RV would expose much weaker geometry. Among currently eligible
    signals, score 0.70/0.65/0.60 all produced the same two observations—lowering to 0.60
    would not admit the 0.50–0.51 MOMENTUM_BURST. No future-outcome label was available,
    so no lookahead claim is made.

25. **Files modified.** `strategies/scalp/{models,signals,diagnostics,analytics,history,
    runtime,freshness}.py`, `watcher/fast_watcher.py`,
    `tests/test_scalp_microbar_freshness.py`, `tests/test_scalp_observability.py`, and this
    report.

26. **Tests added.** New deterministic checks cover formula inputs and persisted units,
    timeframe isolation, component summation/clamping/missing inputs, unchanged score
    threshold, offline read-only analytics, failed-score non-entry, refresh timing,
    quote-age crossing, non-blocking scheduling, atomic cache generations, no duplicate
    batch, and no quote mutation. Existing stale-volume and broker-surface tests remain.

27. **Full suite.** `python -m pytest -q`: **483 passed in 4.58s**.

28. **Thresholds.** Unchanged: quote age 2s, spread 0.10%, relative volume 1.20, score
    0.70, net edge 0.05%, gross RR 1.10, and maximum hold 180s.

29. **Momentum strategy.** Its 0.60 watch threshold, 0.72 trade threshold, two
    confirmations, and 1.50 RR behavior were not changed.

30. **Shadow safety.** Verified `MODE=SHADOW_TRADING`, `SCALP_MODE=SHADOW`, default scalp
    disabled, `LIVE_TRADING_ENABLED=False`, `ROBINHOOD_EXECUTION_ENABLED=False`, and kill
    switch `trading_blocked=true`.

31. **External operations.** No Robinhood order, preview, cancellation, modification,
    account mutation, account read, quote call, or history call occurred during this
    work. Analysis used local session artifacts; provider behavior was tested with fakes.
