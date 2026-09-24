# Deterministic shadow scalping strategy

## Implementation report

1. **Architecture.** Strategy B lives under `strategies/scalp` with separate
   signal, setup, entry, position, event, simulation, analytics and future-policy
   modules. It reuses normalized quotes, `ShadowPortfolio`, `RiskManager`,
   `PortfolioController`, `SafetyOverride`, durable portfolio/event journals,
   reconciliation and restart recovery.

2. **Momentum separation.** The original momentum pipeline is unchanged. The
   scalp universe now has dedicated `scalp_candidate_data` built from a bounded
   liquid-equity seed plus the deduplicated shared active universe; it does not
   require momentum scanner or watchlist admission. Optional slow context only
   fills missing background fields and cannot make quotes or bars fresh. The
   scalp package has no LLM/reasoning imports. A lazy `agent` package export
   compatibility fix prevents strategy imports from initializing the slow
   market-cycle graph.

3. **Setup types.** Deterministic labels are MICRO_BREAKOUT, VWAP_RECLAIM,
   EMA9_CONTINUATION, MICRO_PULLBACK, MOMENTUM_BURST and UNCLASSIFIED. Evidence,
   latest completed micro-bar timestamp, high/low structure and episode ID are
   persisted; insufficient evidence remains UNCLASSIFIED.

4. **Signal features.** The typed feature record contains quote age/spread,
   price relative to VWAP/EMA9/EMA20, EMA9 slope/structure, one/three-bar returns,
   very-short momentum, optional RSI/MACD histogram, relative volume, volume
   slope/acceleration/expansion/contraction, SPY/QQQ/sector relative strength,
   extension, recent high/low distance and realized volatility. The interpretable
   signal score combines momentum, VWAP, EMA slope, relative strength, volume,
   spread quality and an explicit extension penalty. Hard gates are separate.

5. **Expected move.** The model uses only completed prefix bars. It takes the
   larger of three-bar momentum and 1.25× short realized volatility, bounded by
   configurable 0.15%/0.60% floors/caps and capped by known nearby resistance
   when applicable. It is an estimate, not a promise or profitability claim.

6. **Friction.** Estimated round-trip cost is current spread plus entry and exit
   slippage plus optional per-share fees. Base scalp slippage is 2.5 bps each
   side; fees default to zero. Entry requires expected move − estimated cost >=
   0.05%. Spread must be known and <=0.10%. Offline low/base/high friction
   sensitivity is reported and flags a strategy whose positive base expectancy
   disappears under high slippage.

7. **Entry fill.** A long shadow fill is `ask × (1 + scalp entry slippage)` and
   records quoted bid/ask, fill, spread cost and entry slippage. Midpoint is not
   the default.

8. **Exit fill.** Exits use the executable bid (last trade only where the shared
   quote model explicitly permits fallback), then `bid × (1 − scalp exit
   slippage)`. The closed trade records both books, fill, entry/exit slippage,
   spread estimate, gross P&L and net P&L.

9. **Stop.** The stop is the highest observed valid reference below entry from
   recent completed micro swing low, VWAP and EMA9. A separate minimum distance
   gate prevents a zero/tiny structural stop. It is never tightened to force RR.
   STOP_HIT has priority over every target, time, momentum, profit-protection or
   EOD rule.

10. **Target.** The target is entry plus the bounded expected-move projection,
    capped by known resistance. It is not pushed outward to manufacture edge.
    Gross RR, post-friction reward and net RR are logged. The separate scalp RR
    minimum is 1.10; momentum remains 1.50.

11. **Maximum hold.** Default is 180 seconds. A fresh executable quote at/after
    the deadline closes locally as SCALP_TIME_EXIT.

12. **Early exits.** The dedicated controller supports STOP_HIT, TARGET_HIT,
    EOD_EXIT, HARD_RISK_EXIT, SCALP_TIME_EXIT, MOMENTUM_REVERSAL, VWAP_LOSS,
    EMA9_LOSS, VOLUME_FAILURE and optional SCALP_PROFIT_PROTECTION_EXIT. Soft
    signals can exit early but cannot postpone mandatory stop/EOD/global-risk
    exits. Breakeven-style profit protection exists but defaults disabled.

13. **Re-entry.** Episode identity is derived from symbol, setup type, latest
    completed evidence bar and micro high/low. Closed and superseded IDs are
    permanently retired. Re-entry therefore needs a genuinely new completed bar
    or changed structure/classification; time passage alone does not reopen stale
    evidence. No arbitrary long cooldown is enabled.

14. **Overtrading.** Separate conservative defaults enforce three trades per
    symbol/session, eight scalp trades/session, two consecutive losses, 0.5%
    daily scalp-loss budget, a transaction-cost budget, new-evidence episodes and
    the shared simultaneous-position ceiling. The simulator applies the same
    trade/consecutive-loss/daily-loss/position-count concepts.

15. **Portfolio coexistence.** Both strategies use one canonical portfolio.
    Duplicate-symbol checking occurs under the portfolio fill lock, so MOMENTUM
    and SCALP cannot simultaneously hold independent longs in the same symbol.
    Exposure and cash cannot be double-counted. Position controllers skip the
    other strategy’s holdings.

16. **Allocation.** Scalp risk is capped separately at 0.10% equity per trade,
    2% position size and 20% aggregate scalp capital, while also remaining under
    all shared portfolio/daily limits. Positions/trades persist strategy ID,
    capital/risk allocated, realized/unrealized P&L and trade count. Attribution
    reports MOMENTUM and SCALP independently.

17. **Historical simulation.** `ScalpBacktester` replays timestamp-ordered rows,
    filters unclosed/interpolated/future bars, honors session boundaries,
    quote/books, configurable latency, ask/bid slippage, stop/target/time/soft
    exits and structural episode reuse prevention. Outcomes are explicitly
    SIMULATED or UNAVAILABLE. It never calls Robinhood or an LLM.

18. **Sensitivity.** Offline analytics recalculate low/base/high per-side
    slippage and report net expectancy per share plus a fragility flag. Spread
    buckets are <0.05%, 0.05–0.10%, 0.10–0.20% and >0.20%.

19. **Time/regime analysis.** Reports include <30s, 30–60s, 1–2m, 2–5m and 5m+
    holding buckets; opening, mid-morning, midday, afternoon and power-hour
    periods; setup types; existing market regimes; and outperforming/not-
    outperforming SPY, QQQ and sector groups. Each bucket includes count,
    expectancy, win rate, MFE, MAE and cost impact where data exists.

20. **RL compatibility.** Every decision/event includes `strategy_id="SCALP"`,
    episode, timestamp, action-ready features and post-friction edge. A separate
    `ScalpBaselinePolicy` and `ScalpPolicyComparison` boundary reserves
    SCALP_BASELINE_ACTION versus SCALP_RL_ACTION without implementing or training
    a scalp RL model. Scalp and momentum RL records cannot be silently mixed.

21. **Files.** Added `strategies/scalp/{models,signals,setup,events,execution,
    position,runtime,market_data,simulation,analytics,policy}.py`, strategy package
    markers, `tests/test_scalp_strategy.py`, and this report. Modified scalp-only
    config, safe CLI/runtime wiring, shared shadow models/schema/fill routing,
    fast watcher strategy dispatch and lazy analysis exports. Shared fill changes
    branch on strategy ID; momentum still uses its original slippage and RR.

22. **Verification.** Full suite: **450 passed**. Tests cover disabled behavior,
    absence of LLM dependencies, quote/bar freshness, spread and edge gates,
    realistic fills/costs, mandatory/target/time/momentum/EOD/profit exits,
    structural re-entry, overtrading/loss limits, coexistence/attribution,
    future-bar leakage, restart/idempotency, simulator/analytics/events/future-RL
    boundaries, scanner-universe independence and no real executor invocation.

23. **Momentum parameters.** Verified unchanged: watch 0.60, trade 0.72,
    confirmations 2, minimum RR 1.50, scoring/geometry/risk/scanner behavior.

24. **Scalp default.** `SCALP_ENABLED=False`; `SCALP_MODE="SHADOW"`. No code or
    test automatically enables it outside temporary test monkeypatches.

25. **Mode.** `MODE="SHADOW_TRADING"`.

26. **Live flags.** `LIVE_TRADING_ENABLED=False` and
    `ROBINHOOD_EXECUTION_ENABLED=False`.

27. **Kill switch.** Local kill switch remains `trading_blocked=True` / BLOCKED.

28. **Broker safety.** No real Robinhood order, preview, modification,
    cancellation or account mutation occurred. The scalp runtime contains only
    local shadow execution and read-only market inputs.

## Commands

```text
python runner.py --scalp-status
python runner.py --scalp-summary
python runner.py --scalp-backtest --scalp-input historical.jsonl
python runner.py --loop --scalp-shadow
```

`--scalp-shadow` is a process-local shadow-only override and does not edit the
disabled default. There is no live scalp command. The repository currently has zero scalp trades,
so no profitability or real disagreement claim is made. The milestone delivered
correct simulation, safety, attribution and instrumentation—not maximum frequency.
