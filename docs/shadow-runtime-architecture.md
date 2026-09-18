# Shadow runtime architecture refactor

## Report

1. **Architecture found.** Slow research populated mutable candidate context;
   a fast watcher combined slow/live alpha, confirmed entries, refreshed geometry,
   risk-sized them and invoked the local shadow engine. A separate fast position
   watcher and a legacy periodic snapshot fallback monitored exits. An asynchronous
   event bus maintained another lifecycle projection.

2. **Ownership conflicts.** Candidate transitions could claim a position without
   a canonical fill, and reject/expire a symbol already held. Mutable state was
   also consulted when logging older queued events. Fill persistence depended on
   the caller saving after the mutation.

3. **Split-brain cause.** `POSITION_OPEN` was independently persisted in candidate
   state; a stale/replayed lifecycle event or missed close notification could leave
   that projection open after the portfolio was flat. There was no authoritative
   reconciliation that repaired both directions. The save-after-notification gap
   was an additional restart risk, not proof of a particular historical incident.

4. **Position authority.** `ShadowPortfolio` alone owns local holdings, cash and
   closed trades. Candidate state is a display projection for position states.
   Bound production stores ignore conflicting lifecycle events and candidate
   rejection/expiry cannot close a canonical position. Standalone simulations
   retain their existing unbound projection behavior.

5. **Setup episodes.** IDs are `SYMBOL-<24 hex digits>` derived from the research
   cycle ID. Cached/preserved research retains its episode; a new research cycle
   creates a new episode. Active contexts remain in the atomic context snapshot;
   immutable research/alpha facts are archived in its sibling `.episodes.jsonl`.
   New episodes do not inherit old transition history. The same stopped episode
   cannot reopen. The exact new-evidence rule is **a new slow research cycle**;
   no cooldown, new threshold, or automatic re-entry was introduced.

6. **Alpha/geometry/risk/portfolio boundaries.** `AlphaSnapshot` captures each
   symbol's slow/live scores, weights and component provenance without changing
   the formula. Missing provenance is explicitly UNKNOWN, not inferred NEGATIVE.
   `GeometryEngine` wraps existing deterministic technical geometry in a
   `GeometryDecision`; it does not optimize target/stop to make RR pass.
   `RiskManager.evaluate_geometry` consumes fixed levels and returns a typed
   `RiskDecision` alongside its backward-compatible result. `PortfolioController`
   checks canonical cash, counts, daily limits, requested size and duplicates
   under the same lock held through fill commit. Unsupported sector/correlation
   constraints are not invented (`EXISTING_LIMITS_ONLY`). Fill slippage still
   triggers the existing final RR/risk checks.

7. **Position controller.** The fast watcher implementation is now formally
   `PositionController`; `FastPositionWatcher` is a compatibility alias. It owns
   independent quote-driven stop, target, hard-loss and EOD exits. The candidate
   watcher removes held symbols from its entry work, never closes them. Slow
   alpha refreshes are informational for open symbols. Existing deterministic
   periodic snapshot invalidation/bar-reconstruction fallback is retained to
   avoid an unrequested strategy change; see remaining debt below.

8. **Market quality.** `MarketSnapshot` normalizes provider quotes and completed
   candles with FRESH/STALE/UNAVAILABLE/INVALID quality, exchange timestamp, age,
   spread and provider. Candidate and position watchers use its quote quality;
   pre-execution validation records normalized quality and retains the mandatory
   15-second refreshed exchange-timestamp gate and completed-structure checks.
   Refresh failures remain recoverable infrastructure blocks. A healthy fast
   quote alone does not clear a failed pre-execution refresh.

9. **Event schema.** Typed runtime facts carry event ID, UTC timestamp, symbol,
   episode ID, research cycle ID and payload. Existing `MarketEvent` now carries
   episode ID and defensively copies input payloads. Non-quote runtime events are
   durably journaled before dispatch; the async logger deduplicates by event ID.
   It no longer fills historical facts from newer mutable state. Journals are
   append-only JSONL with fsync; rotating operational logs remain compatible.
   Geometry, risk, portfolio, confirmation, infrastructure and exit facts are
   available in the episode/portfolio journals. Raw quote spam is omitted.

10. **Restart/replay.** Canonical atomic portfolio records recover positions,
    stops/targets, episodes and closed trades. Context snapshots recover latest
    slow research and active setup state with their original expiry. Canonical
    records also act as an outbox: startup repairs missing position journal and
    trade-log entries. `EventJournal.replay()` builds audit projections only; it
    has no executor, broker, or order dependency. Corrupt journals fail visibly
    rather than silently discarding facts.

11. **Idempotency.** Episode-bearing plans receive deterministic trade IDs and
    `entry:<episode>` intent IDs. Closed trades have `exit:<trade_id>` IDs.
    Canonical duplicate-symbol/episode checks happen under the fill lock.
    Repeated close is a no-op and cannot credit cash twice. Event IDs deduplicate
    journal writes. Portfolio saves occur before lifecycle facts are published;
    save failures roll memory back. If audit append fails after the canonical
    commit, restart repairs the audit from canonical truth rather than executing
    again.

12. **Reconciliation.** Portfolio open repairs a stale/absent candidate projection
    to POSITION_OPEN. Candidate open without a canonical position becomes CLOSED
    if a closed trade exists, otherwise EXPIRED. `STATE_RECONCILED` includes old
    state, canonical state, symbol, episode, reason and timestamp. No holding is
    fabricated. Delayed explicitly tagged old-episode events cannot mutate a new
    episode. Duplicate lifecycle notifications never create financial state.

13. **Added modules.** `trading_runtime/{contracts,setup_controller,
    portfolio_controller,reconciliation,journal}.py`, package initializer,
    `tests/test_runtime_boundaries.py`, and this report.

14. **Modified boundaries.** `agent/candidate_context.py`, `agent/market_cycle.py`,
    `event_driven/{events,state,orchestrator,logging}.py`,
    `execution/{models,geometry,pre_execution,shadow_executor}.py`,
    `risk/risk_manager.py`, `shadow/{models,portfolio,execution}.py`,
    `watcher/{candidate_watcher,fast_watcher}.py`, portfolio schema and exit/refresh
    regression expectations. Existing unrelated worktree edits were preserved.

15. **Migration.** No historical user portfolio/trade files were erased or
    rewritten by this task. Schema version 1 remains readable; new metadata
    fields are optional. Missing episode/intent metadata gets clearly marked
    `legacy:<trade_id>` identity in memory. Startup may append missing audit
    projections, but does not rewrite original trade facts or the loaded snapshot.
    Normal future saves include the added metadata. No mode/config migration.

16. **Verification.** Full suite run repeatedly at incremental checkpoints;
    final result: **393 passed** (`python -m pytest -q`). New regressions cover
    reconciliation both directions, duplicate lifecycle events, stopped-episode
    rejection/new-cycle admission, durable restart, save rollback, missing-audit
    recovery, legacy loading, late events, independent alpha/geometry/risk,
    portfolio rejection, quality/infrastructure blocks, position-controller exits,
    candidate non-ownership and an end-to-end no-real-executor assertion. Existing
    refresh, EOD, fast/slow isolation and execution-safety tests remain green.

17. **Remaining architectural debt.** This is incremental, not full event sourcing.
    Canonical snapshots, not a merged global event log, remain recovery authority.
    Journals are single-process/thread-safe, not multi-process transactional
    storage. Operators still need retention/backup policy for unbounded audit
    journals. Legacy dict APIs, compatibility simulation projections, and the
    periodic deterministic exit fallback remain. Some provenance is UNKNOWN
    because upstream research does not supply it. MarketSnapshot coexists with
    legacy analyzer freshness checks. Research-only nonadmitted rows retain
    legacy event vocabulary. No claim of distributed exactly-once execution.

18. **Strategy unchanged.** This refactor did not edit configuration: watch .60,
    trade .72, two confirmations, minimum RR 1.5, minimum stop distance, weights,
    sizing, daily limits, scanner filters and top-N remain unchanged. Existing
    score components may use structural location as before; separation does not
    silently redesign those features. Only state/identity/recovery errors changed.

19. **Mode:** verified `SHADOW_TRADING`.
20. **Execution enablement:** verified both live flags False and local kill switch
    `trading_blocked=True` (`BLOCKED`). No enablement or confirmation value changed.
21. **Broker safety:** no real Robinhood order/account operation occurred. Tests
    used local temporary portfolios and mocked data. No LLM agents were added and
    no LLM call was introduced into fast processing.

## Transition ownership

| Owner | Transition/fact | Authority |
|---|---|---|
| Scanner/research | DISCOVERED → WATCHLIST → SETUP_FORMING | Setup context |
| SetupController/fast candidate watcher | Confirmation → TRADE_READY | Episode; unchanged two-update rule |
| Data layer | Temporarily blocked ↔ recovered | Infrastructure, not terminal strategy rejection |
| GeometryEngine | GEOMETRY_VALIDATED / GEOMETRY_REJECTED | Refreshed Python structural validation |
| RiskManager | RISK_APPROVED / RISK_REJECTED | Fixed geometry and deterministic limits |
| PortfolioController | PORTFOLIO_APPROVED / PORTFOLIO_REJECTED | Canonical capital/count/duplicate checks |
| Shadow executor | Local fill → POSITION_OPEN | Durable ShadowPortfolio |
| PositionController | Exit intent → CLOSED | Durable ShadowPortfolio; never entry alpha |
| Reconciliation | Repair position-state projections | Canonical open/closed records |

Geometry/portfolio decisions are typed facts, not additional independently mutable
candidate-state enums. `RISK_APPROVED` remains a legacy transient projection.

## Exit facts

Canonical exit audit payloads include episode/position IDs, entry/exit prices and
times, holding seconds, stop/target at exit, realized P&L, reason, price source and
quote timestamp. Next-day recovery uses `MISSED_EOD_RECOVERY_EXIT` at the actual
fresh recovery quote, never a fictional previous-day EOD fill. Legacy unavailable
price provenance remains null rather than invented.
