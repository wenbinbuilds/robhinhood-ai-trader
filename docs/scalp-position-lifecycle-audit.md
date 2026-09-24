# SCALP_V1 Position Lifecycle Audit

## HALO incident evidence

Episode `SCALP-HALO-fb3f6ab122be4d43a9afa846` opened in shadow state at
`2026-09-18T16:17:42.290386Z`. The append-only portfolio-open event and the
closed trade both preserve that same entry timestamp. The original fast watcher
last produced a scalp cycle at `16:18:04.319563Z`. Its 180-second wall-clock
deadline was `16:20:42.290386Z`, after the original process had stopped.

The next `FAST_WATCHER_STARTED` event was `16:36:30.047663Z`; the first cycle
observed restored HALO at `16:36:30.209064Z`, age 1,127.919 seconds. The available
HALO exchange quote was already 20.017 seconds old and therefore failed the
unchanged 2-second scalp quote limit. Subsequent quotes remained stale. A quote
at `16:37:13.626587Z` was 2.189 seconds old and still failed. The first usable
quote arrived at `16:37:33.545252Z`, age 1.930 seconds, with exchange timestamp
`16:37:31.615022Z`. The controller exited at its executable bid and the shared
shadow fill engine applied configured exit slippage. Recorded hold was 1,191.255
seconds.

The old implementation did not reset the timer: it derived age from the
persisted UTC entry timestamp. The failure was that it checked max hold only
after finding a fresh quote and silently skipped a missing/stale quote. It also
had no startup overdue classification, so the process gap and later quote wait
were invisible.

## Corrected production lifecycle

1. `ShadowPortfolio` remains the sole canonical position owner and atomically
   restores the original `entry_timestamp`.
2. `ScalpRuntime` reconciles restored positions during construction, before the
   watcher can run discovery or entry admission.
3. Age is recomputed as timezone-aware UTC wall-clock time minus the persisted
   fill time. No monotonic value is persisted. Monotonic time remains limited to
   in-process watcher cadence measurements.
4. A restored position at or beyond 180 seconds is persisted as
   `OVERDUE_SCALP_POSITION` / `OVERDUE_EXIT_PENDING`.
5. Every watcher cycle processes open scalp positions before candidate work.
   Mandatory stop, target, EOD, hard-risk, and max-hold decisions run before any
   signal/history lookup.
6. A time exit requires a current quote within 2 seconds and an executable bid.
   No last-trade fallback is used for a scalp exit. Configured shadow slippage
   remains applied by `ShadowExecutionEngine`.
7. Missing or stale pricing leaves the position open but persists the exact
   blocker (`QUOTE_UNAVAILABLE` or `STALE_QUOTE`) and blocks new scalp admission
   until the mandatory exit can resolve.
8. An already-overdue restored position closes as
   `SCALP_RECOVERY_TIME_EXIT`; a position crossing the limit in the current
   process closes as `SCALP_TIME_EXIT`.
9. Closure remains idempotent: the canonical position is removed once, cash and
   P&L are booked once, and deterministic portfolio journal identifiers prevent
   duplicate open/close facts during replay.

## Scheduling and observability

The slow momentum/LLM loop and fast watcher are separate threads. Scalp position
management was moved ahead of momentum candidate processing inside the fast
cycle, so a momentum pre-execution refresh cannot own the max-hold path. The
five-minute history refresher remains a background worker with atomic cache
publication; max-hold evaluation precedes scheduling or reading that refresh.

Persisted lifecycle diagnostics now include symbol, episode, entry/current UTC
times, hold, configured maximum, remaining/overdue seconds, exit status/reason,
latest quote age, recovery flag, and next action. Closed trades include:

- `crossed_max_hold_at`
- `exit_decision_at`
- `max_hold_decision_delay_seconds`
- `max_hold_close_delay_seconds`
- `recovery_exit`
- `max_hold_delay_reasons`

Session and performance summaries separate normal and recovery time exits and
report average/median/p90/max holds, overdue incidence, maximum overdue time,
quote-unavailable delays, watcher-scheduling delays, and per-trade max-hold SLA
timestamps.

## Safety and unchanged strategy parameters

This change is local shadow-state logic only. `MODE=SHADOW_TRADING`,
`SCALP_MODE=SHADOW`, default scalp activation remains disabled,
`LIVE_TRADING_ENABLED=False`, `ROBINHOOD_EXECUTION_ENABLED=False`, and the local
kill switch remains blocked. No Robinhood order or account mutation path is
used.

Entry rules are unchanged: quote freshness 2 seconds, spread 0.10%, relative
volume 1.20, signal score 0.70, minimum net edge 0.05%, scalp risk/reward 1.10,
and maximum hold exactly 180 seconds. Momentum thresholds and behavior are
unchanged.
