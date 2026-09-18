# Entry geometry and overdue shadow exits

## Evidence and root cause

The original 80-analysis audit does not prove that its 40 slow-analysis RR
failures used stale geometry. Direct slow collection already recomputes levels
from completed candles. An unchanged session high is often a legitimate ceiling.
Twelve declining-RR comparisons with unchanged targets establish entry drift,
not stale target detection. The current local log contains an additional cycle:
90 analyses, 49 true-hard rejections. Do not compare totals from these two
snapshots as if they were the same sample.

A separate implementation defect was confirmed: FastCandidateWatcher compared
the live entry with the stored slow stop/target and rejected INVALID_STOP,
PRICE_BEYOND_TARGET, or RISK_REWARD before invoking PreExecutionValidator. That
prevented the selected candidate from reaching the existing candle refresh.
These geometry decisions now occur after confirmed alpha and structural refresh.
Quote availability, age, session and spread still block before confirmation.

## Field ownership

| Field | Created/owned by | Fast watch | Entry validation |
|---|---|---|---|
| Research entry | Slow CandidateAnalyzer: max(price, ask); CandidateContext retains price/ask | Fixed attribution reference | Retained for comparison |
| Live entry | Fresh quote: max(price, ask) | Changes with quotes | Replaced by newly fetched quote |
| Support/resistance | candidate_bundle / completed candle indicators | Stored context used for score | Direct provider recomputes from completed candles; validator verifies evidence |
| Stop | Highest valid support, session low, EMA20 or VWAP below entry | Stored reference used for score | Rebuilt from refreshed structural inputs |
| Target | Observed current-session high | Stored reference used for score | Rebuilt from refreshed completed candle highs |
| RR and size | CandidateAnalyzer / RiskManager | Stored RR is diagnostic only | Recomputed; unchanged 1.5 and sizing rules |

The existing target is the high of the current session's retained candle window,
not ATR, fixed percentage, nearest swing, or a searched-for profitable target.
The indicator engine retains a configured candle window; it may not contain the
entire session. The new quality evidence distinguishes minor bar highs, the
observed session high and repeated exact touches, and records touch volume.
These labels do not change target selection. Nearest observed higher bar high
is logged separately and is not promoted to a new gate.

## Structural refresh

The direct provider fetches the selected symbol's candles before its quote.
It recalculates indicators and structure from completed, non-interpolated bars.
It requires current-session structure and the latest expected completed
five-minute interval (bar age below ten minutes). This is a completed-bar
validity condition, not a new entry-drift threshold. Forming bars cannot extend
the target. Provider results include evidence, and the validator recomputes
evidence-bearing structure itself, overwriting proposed target/support/VWAP/EMA
values with the candle calculation. Existing quote-only provider adapters retain
their canonical-context compatibility; the production direct provider always
uses the completed-structure path.

Stops can rise because completed-candle VWAP/EMA or support changed. They never
rise because RR failed. Target selection never reads required RR. The existing
stop-distance gate can reject the closest structural stop rather than selecting
another stop to improve acceptance.

The maximum entry compatible with a given stop S and target T is
`(T + minimum_RR * S) / (1 + minimum_RR)`. It is logged, not a new tunable gate.
The existing RR check rejects entries beyond that window. An existing rejected
context cannot run another entry attempt; a fresh slow cycle can reconsider it.

Every entry record includes research RR, live RR with frozen research levels,
refreshed RR, degradation, drift, level evidence, selected stop source and
structural entry ceiling. Boundary crossings distinguish price drift, target
contraction and stop widening where paired evidence exists. Wide stops and
nearby resistance are otherwise joint causes of RR, so exclusive counts remain
unavailable rather than being invented.

The retained September 17 data has 75 adjacent slow-snapshot comparisons. Seven
cross below 1.5 when only entry is moved against preceding geometry; five remain
RR failures after the next slow recalculation, while two regain valid RR. These
are observational comparisons, not replayed trades or proof of extra fills.
Historical entry attempts lack the newly added paired fields. Their entry-RR
distributions and exclusive cause counts cannot be recovered reliably.

## BBNX and end of day

BBNX opened September 16 at 15:10:58 UTC and was recorded closed September 17
at 16:20:07 UTC: 1,509.16 minutes. Fast telemetry ends September 16 around
17:00:08 UTC; slow telemetry ends around 16:57:18 UTC. Fast monitoring restarts
September 17 at 16:20:00 UTC. There is no evidence of monitoring at the prior
session's closing cutoff. The logs establish a monitoring gap, but do not prove
whether a user stop, process exit, machine sleep, or another outage caused it.

Positions persist in local JSON across restarts. Both watchers already had EOD
logic. The slow recovery path inspected historical target/stop touches before
the carried-position EOD check, producing TARGET_HIT / RECONSTRUCTED_FROM_BAR_DATA
at recovery time. The fast path similarly prioritized target/stop labels.

Both paths now prioritize overdue/cutoff END_OF_DAY_EXIT at a fresh available
price. Slow recovery explicitly records MISSED_EOD_MONITORING_WINDOW. It never
fabricates a prior-session fill or rewrites the historical BBNX trade. Existing
early-close handling in the fast watcher remains intact. No local code can
execute while the process is stopped; continuous monitoring is still required
for timely intraday exits.

## Verification

Deterministic tests cover unchanged structure passing, entry drift rejecting,
legitimate completed-high target refresh, justified VWAP stop refresh,
unsupported target inflation and stop tightening, forming/stale candles,
confirmation reaching refresh despite frozen RR, and EOD taking priority over
retrospective or current target hits. No strategy thresholds, weights, risk
limits, scanner count, mode, live flags, or real positions are changed.
