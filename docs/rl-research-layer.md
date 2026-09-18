# Offline RL and shadow-comparison layer

## Architecture and safety report

1. **Architecture.** The `rl` package sits beside the event-driven runtime. It
   consumes timestamped market/setup/alpha facts and emits only discrete
   preferences. It has no Robinhood client. In shadow control, an ENTER proposal
   follows the existing pre-execution refresh, `GeometryEngine`, `RiskManager`,
   `PortfolioController`, `SafetyOverride`, and local `ShadowExecutionEngine`.
   Position authority remains `ShadowPortfolio`.

2. **Observation space.** There are 73 causal features plus 73 availability
   masks (146 floats): technical/EMA/VWAP/MACD/RSI and returns; richer volume;
   SPY/QQQ/sector-relative strength; alpha levels and slopes; extension/RR/setup
   age/confirmation; deterministic setup/regime one-hot values; and optional
   position facts. Missing values are zero only alongside a zero availability
   mask, so missing is not silently interpreted as negative.

3. **Actions.** Entry policy is exactly WAIT, ENTER, IGNORE_SETUP. The future
   separated position arbiter supports HOLD/EXIT, where deterministic STOP,
   TARGET, EOD and hard-risk exits always win. RL cannot size, leverage, short,
   move stops, move targets, or invoke live execution.

4. **Reward.** Version 1.0 uses R-multiple from an ask/slippage entry and
   bid/slippage exit, then subtracts spread/slippage/commission friction in R,
   drawdown and MAE penalties, overtrading penalty, and a small holding-time
   penalty. Unavailable entry/exit/initial-risk facts produce an UNAVAILABLE
   outcome and zero training reward, not a fabricated fill. Dollar P&L is not
   the reward basis.

5. **Friction assumptions.** Defaults use the existing 5 bps shadow entry and
   exit slippage, actual recorded bid/ask when supplied, zero configurable
   commission, and an explicit minimum-latency configuration placeholder. The
   pipeline does not default to midpoint fills.

6. **Historical data.** The CLI accepts chronological JSON/JSONL described by
   `rl/historical_row.schema.json`, producing `state/rl/dataset.jsonl` by default.
   Each record stores timestamp, symbol, episode, full vector, baseline action,
   reward, next-state metadata, terminal reason, quality flags, setup/regime
   classification, and outcome provenance. It supports many symbols, sessions,
   setups and regimes. Current production logs are not treated as a sufficient
   training corpus because they contain few completed outcomes and incomplete
   historical quote/candle sequences.

7. **Look-ahead prevention.** The builder slices history at T, resets features
   across session dates, accepts only five-minute candles closed by T, ignores
   forming/interpolated bars, and derives support/resistance only from that
   prefix. Raw support/resistance requires an `*_as_of`/`structure_as_of`
   timestamp no later than T. Outcomes are stored separately from observations.
   Tests inject a future 999 session high and verify it cannot enter features or
   setup classification.

8. **Splits.** Train/validation/test splits use ordered session dates. At least
   three sessions are required. No timestamp is randomly shuffled across a
   boundary. Normalization is fit only on the train split and validation/test
   fitting raises an error.

9. **Walk-forward.** Expanding training windows with configurable minimum train
   sessions and test-window size are supported. Each report retains window dates
   and metrics.

10. **Algorithm.** The only learning backend is Stable-Baselines3 PPO with a
    discrete action space. Imports are lazy, training is explicit/offline, and
    deterministic Python/NumPy/Torch/environment/SB3 seeds are recorded. A
    32-timestep real PPO smoke train passed locally. No alternate algorithms or
    online updates were added.

11. **Normalization.** Mean/std are computed solely from training vectors;
    constant dimensions use scale 1. Availability masks remain binary. Exact
    ordered feature names, mean/std and normalization version are stored with the
    model and validated at load.

12. **Registry.** Models receive immutable unique directories and metadata for
    model/algorithm, train dates, feature/reward/normalization versions,
    hyperparameters, all seeds, git commit and metrics. Existing IDs cannot be
    overwritten. Promotion is explicit and one stage at a time: TRAINED →
    VALIDATED → SHADOW_COMPARE → SHADOW_APPROVED. There is no live state and no
    automatic promotion.

13. **Baseline.** `BaselinePolicy` remains dynamic score >= 0.72 and confirmation
    count >= 2. Existing geometry/hard/risk/portfolio checks remain downstream.
    Watch threshold .60 and all other strategy/risk parameters are unchanged.

14–18. **Offline results.** Baseline and RL evaluators report entries, win rate,
    mean/median R, expectancy, profit factor, maximum drawdown in R, Sharpe-like
    ratio, holding time, MFE, MAE, MFE capture, setup type, regime, score bucket,
    and overtrading. No honest baseline or RL performance number is reported yet:
    this repository does not currently contain the requested multi-symbol,
    multi-day, execution-valid unseen historical corpus. Training-fixture results
    are smoke tests, not evidence of trading quality. Metrics become available
    through `--rl-evaluate` once such a dataset is supplied.

19–20. **Shadow compare.** Runtime comparison is a side channel invoked after a
    causal score update. It records baseline/RL actions, episode, score,
    disagreement and unavailable/observed outcome status; it owns no executor or
    portfolio. The offline CLI compares on the held-out test split. Current real
    disagreement count is **UNAVAILABLE** because no model has been deliberately
    trained, promoted and selected; zero is not claimed.

21. **Known data limitations.** Existing score logs have missing quote samples,
    limited completed trade outcomes, only a few days/regimes, and incomplete
    synchronized SPY/QQQ/sector histories. The builder flags stale/missing quotes,
    unknown spread, invalid/unclosed candles, >120-second monitoring gaps, broken
    timestamp sequences and insufficient warmup. These rows truncate/exclude
    entry learning rather than being silently consumed. Counterfactual fills are
    labeled OBSERVED, SIMULATED or UNAVAILABLE.

22. **Files.** Added `rl/{actions,setups,observations,rewards,dataset,env,policy,
    training,evaluation,diagnostics,registry,safety,shadow_compare}.py`, the row
    schema, this report, and `tests/test_rl_layer.py`. Modified config,
    requirements, runner CLI, the candidate watcher and the local shadow engine.

23. **Tests.** The final full suite passed: **420 tests**.
    Coverage includes future-candle/session-high leakage, session boundaries,
    chronological/walk-forward splitting, train-only normalization, actions,
    data quality, every safety decision, baseline preservation, comparison
    immutability, position mandatory exits, registry immutability, seeded
    reproducibility, actual PPO training, control-mode shadow-only routing, and
    duplicate prevention, in addition to all pre-existing execution tests.

24–31. **Safety confirmation.** Deterministic thresholds and risk limits were not
    changed. An RL ENTER cannot bypass stale quote, spread, minimum RR, stop
    distance, market hours, risk, capital, daily loss, duplicate, or EOD rules.
    Stop/target are absent from the action space and remain frozen geometry facts.
    `MODE` remains `SHADOW_TRADING`; both live flags remain False; the local kill
    switch remains blocked. No real Robinhood order operation was performed.

## Safe CLI

```text
python runner.py --rl-build-dataset --rl-input historical.jsonl
python runner.py --rl-train --rl-timesteps 10000
python runner.py --rl-evaluate --rl-model-id <explicit-id>
python runner.py --rl-shadow-compare --rl-model-id <explicit-id>
python runner.py --rl-status
```

No CLI enables live RL execution. Shadow runtime inference additionally requires
`RL_ENABLED=True`, a deliberately selected `RL_MODEL_ID`, and the required model
registry promotion. Defaults are `RL_ENABLED=False`, `RL_MODE="OFFLINE"`.
