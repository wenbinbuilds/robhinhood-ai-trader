"""Event projection for the existing slow-research and fast-quote lanes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence
import json

import config
from agent.candidate_context import CandidateContext
from agent.gate_policy import failed_gates
from event_driven.bus import InProcessEventBus
from event_driven.events import (
    AlphaUpdatedEvent,
    BarClosedEvent,
    CandidateDiscoveredEvent,
    CandidateRemovedEvent,
    CandidateStateChangedEvent,
    CandidateStillActiveEvent,
    ContextExpiredEvent,
    ContextUpdatedEvent,
    ExitRequestedEvent,
    MarketEvent,
    NewsUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
    QuoteEvent,
    RiskApprovedEvent,
    RiskRejectedEvent,
    ScannerEvent,
    ShadowEntryEvent,
    StopHitEvent,
    TargetHitEvent,
    TradeCandidateEvent,
)
from event_driven.logging import StructuredEventLogger, concise_event_line
from event_driven.state import CandidateState, CandidateStateStore, IllegalStateTransition
from watcher.models import FastQuote


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


class ReasoningTriggerPolicy:
    """Deduplicate LLM research unless meaningful slow evidence changed."""

    def __init__(self) -> None:
        self._fingerprints: dict[str, str] = {}

    @staticmethod
    def fingerprint(symbol: str, *, completed_bar_timestamp: str | None,
                    news_event_ids: Sequence[str], sector_regime: str | None,
                    market_regime: str | None, technical_disposition: str | None) -> str:
        value = {
            "symbol": symbol.upper(),
            "completed_bar_timestamp": completed_bar_timestamp,
            "news_event_ids": sorted(set(news_event_ids)),
            "sector_regime": sector_regime,
            "market_regime": market_regime,
            "technical_disposition": technical_disposition,
        }
        return sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    def should_refresh(self, symbol: str, **evidence: Any) -> bool:
        symbol = symbol.upper()
        current = self.fingerprint(symbol, **evidence)
        if self._fingerprints.get(symbol) == current:
            return False
        self._fingerprints[symbol] = current
        return True


class ShadowEventOrchestrator:
    """Connect typed events to state/logging without owning market-data IO."""

    def __init__(self, *, state_path: str | Path, event_log_path: str | Path,
                 bus: InProcessEventBus | None = None,
                 terminal_output: bool = False) -> None:
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("event orchestrator is SHADOW_TRADING only")
        self.bus = bus or InProcessEventBus()
        self.state_store = CandidateStateStore(state_path)
        self.logger = StructuredEventLogger(event_log_path, self.state_store)
        self.bus.subscribe(None, self.logger)
        if terminal_output:
            self.bus.subscribe(None, self._terminal_report)
        self._lock = RLock()
        self._universe: set[str] = {
            item.symbol for item in self.state_store.snapshot(include_terminal=False)
        }

    @staticmethod
    def _terminal_report(item: MarketEvent) -> None:
        if item.event_type.value not in {
            "SCANNER", "CANDIDATE_DISCOVERED", "CANDIDATE_REMOVED",
            "CANDIDATE_STATE_CHANGED",
            "CONTEXT_EXPIRED", "ALPHA_UPDATED", "TRADE_CANDIDATE",
            "RISK_APPROVED", "RISK_REJECTED", "POSITION_OPENED",
            "SHADOW_ENTRY", "STOP_HIT", "TARGET_HIT", "EXIT_REQUESTED", "POSITION_CLOSED",
            "INFRASTRUCTURE_ERROR",
        }:
            return
        line = concise_event_line(item)
        if line:
            print(line, flush=True)

    def start(self) -> None:
        self.bus.start()

    def stop(self) -> None:
        self.bus.stop()

    def recover_open_positions(self, positions: Sequence[Any]) -> None:
        for position in positions:
            symbol = getattr(position, "symbol", None)
            entry_timestamp = getattr(position, "entry_timestamp", None)
            if symbol and isinstance(entry_timestamp, str):
                self.state_store.recover_open_position(
                    symbol,
                    entry_timestamp=entry_timestamp,
                    transition_history=getattr(position, "state_transition_history", ()),
                )

    def emit(self, item: MarketEvent) -> None:
        # State projection is immediate/deterministic; optional consumers are
        # isolated behind the bounded priority queue.
        self.state_store.handle(item)
        self.bus.publish(item)

    def transition(self, symbol: str, new_state: CandidateState, *, now: datetime,
                   event_type: str, reason: str | None = None) -> None:
        current = self.state_store.get(symbol)
        previous = current.state.value if current else None
        updated = self.state_store.transition(
            symbol, new_state, timestamp=now, event_type=event_type, reason=reason
        )
        self.bus.publish(CandidateStateChangedEvent(
            now, "CANDIDATE_STATE_MACHINE", symbol=symbol,
            payload={"previous_state": previous, "new_state": updated.state.value,
                     "reason": reason or event_type},
        ))

    def ingest_slow_cycle(self, result: Mapping[str, Any], contexts: Sequence[CandidateContext],
                          *, now: datetime, snapshot: Mapping[str, Any] | None = None,
                          scanner_already_ingested: bool = False) -> None:
        now = now.astimezone(timezone.utc)
        cycle_id = str(result.get("generated_at") or now.isoformat())
        scanner_rows = result.get("scanner_candidates", [])
        if not scanner_already_ingested:
            self.ingest_scanner_rows(scanner_rows, now=now, cycle_id=cycle_id)

        data_by_symbol = {
            str(row.get("symbol", "")).upper(): row
            for row in (snapshot or {}).get("candidate_data", [])
            if isinstance(row, Mapping) and row.get("symbol")
        }
        analyzed_rows = [
            row for row in result.get("analyzed_candidates", [])
            if isinstance(row, Mapping) and row.get("symbol")
        ]
        admitted = {context.symbol: context for context in contexts}
        reasoning_trace = _mapping(_mapping(result.get("llm_reasoning")).get("trace"))
        reasoning_cached = reasoning_trace.get("status") == "CACHED"
        for row in analyzed_rows:
            symbol = str(row.get("symbol", "")).upper()
            existing = self.state_store.get(symbol)
            if existing is None or existing.state in {
                CandidateState.CLOSED, CandidateState.EXPIRED, CandidateState.REJECTED
            }:
                self.emit(CandidateDiscoveredEvent(now, "SLOW_RESEARCH", symbol=symbol, cycle_id=cycle_id))
                self.transition(symbol, CandidateState.WATCHLIST, now=now, event_type="RESEARCH_ADMISSION")
            existing = self.state_store.get(symbol)
            context = admitted.get(symbol)
            research_timestamp = (
                existing.research_timestamp
                if reasoning_cached and existing and existing.research_timestamp
                else str(result.get("analysis_started_at") or result.get("timestamp") or now.isoformat())
            )
            context_expiration = (
                existing.context_expiration
                if reasoning_cached and existing and existing.context_expiration
                else (now + timedelta(seconds=config.CANDIDATE_CONTEXT_TTL_SECONDS)).isoformat()
            )
            raw = data_by_symbol.get(symbol, {})
            technical_metrics = _mapping(row.get("deterministic_technical_metrics"))
            coordinator = _mapping(row.get("coordinator_decision"))
            technical_context = _mapping(row.get("technical_context"))
            llm_context = _mapping(row.get("llm_analysis"))
            news_context = _mapping(row.get("news_context"))
            sector_context = _mapping(row.get("sector_context"))
            market_context = _mapping(row.get("market_context"))
            validation = _mapping(technical_metrics.get("technical_validation"))
            true_hard_failures, signal_quality_failures = failed_gates(validation)
            coordinator_vetoes = coordinator.get("vetoes", [])
            if not isinstance(coordinator_vetoes, Sequence) or isinstance(
                coordinator_vetoes, (str, bytes)
            ):
                coordinator_vetoes = []
            coordinator_vetoes = [str(value) for value in coordinator_vetoes]
            candles = raw.get("candles", []) if isinstance(raw, Mapping) else []
            completed = []
            for item in candles:
                if not isinstance(item, Mapping) or item.get("interpolated") or item.get("is_forming") is True:
                    continue
                try:
                    began = datetime.fromisoformat(str(item.get("begins_at", "")).replace("Z", "+00:00"))
                    if began.tzinfo is None or (now - began.astimezone(timezone.utc)).total_seconds() < 300:
                        continue
                except ValueError:
                    continue
                completed.append(dict(item))
            if completed:
                self.emit(BarClosedEvent(now, "FIVE_MINUTE_TECHNICAL_ENGINE", symbol=symbol,
                                         cycle_id=cycle_id, payload={"completed_5m_candles": completed,
                                         "technical_metrics": dict(technical_metrics)}))
            self.emit(NewsUpdatedEvent(now, "NEWS_CONTEXT_ENGINE", symbol=symbol, cycle_id=cycle_id,
                                       payload=dict(news_context)))
            self.emit(ContextUpdatedEvent(now, "LLM_CONTEXT_AGENT", symbol=symbol, cycle_id=cycle_id,
                                          payload={"llm_context": dict(llm_context),
                                                   "sector_context": dict(sector_context),
                                                   "market_context": dict(market_context),
                                                   "research_timestamp": research_timestamp,
                                                   "context_expiration": context_expiration,
                                                   "research_price": _number(technical_metrics.get("current_price")),
                                                   "entry_reference": _number(row.get("entry")),
                                                   "stop_reference": _number(row.get("stop")),
                                                   "target_reference": _number(row.get("target"))}))
            slow_score = _number(coordinator.get("combined_score"))
            if context is not None:
                admission_reason = (
                    "FAST_WATCH_ADMITTED_WITH_SIGNAL_QUALITY_WARNINGS"
                    if signal_quality_failures else "FAST_WATCH_ADMITTED"
                )
            elif true_hard_failures:
                admission_reason = "TRUE_HARD_REJECTION: " + ",".join(true_hard_failures)
            elif coordinator_vetoes:
                admission_reason = "COORDINATOR_VETO: " + ",".join(coordinator_vetoes)
            elif row.get("technical_disposition") in {"QUALIFIED", "MONITORABLE"} and slow_score is not None:
                admission_reason = (
                    f"FAST_WATCH_NOT_ADMITTED: SLOW_SCORE_BELOW_{config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:.2f}"
                )
            else:
                admission_reason = "TRUE_HARD_REJECTION: TECHNICAL_VALIDATION"
                true_hard_failures = ["TECHNICAL_VALIDATION"]
            before_alpha = self.state_store.get(symbol)
            permanently_rejected = bool(true_hard_failures or coordinator_vetoes)
            intended_state = (
                CandidateState.SETUP_FORMING.value
                if context is not None else
                CandidateState.REJECTED.value
                if permanently_rejected else
                CandidateState.WATCHLIST.value
            )
            self.emit(AlphaUpdatedEvent(now, "SLOW_ALPHA", symbol=symbol, cycle_id=cycle_id,
                                        payload={"slow_alpha_score": slow_score,
                                                 "technical_score": _number(coordinator.get("technical_score", technical_context.get("technical_score"))),
                                                 "technical_confidence": _number(validation.get("technical_confidence", validation.get("confidence"))),
                                                 "qualitative_score": _number(coordinator.get("qualitative_score")),
                                                 "news_score": _number(coordinator.get("news_score")),
                                                 "sector_score": _number(coordinator.get("sector_score")),
                                                 "market_score": _number(coordinator.get("market_score")),
                                                 "context_age_seconds": 0.0,
                                                 "context_ttl_seconds": config.CANDIDATE_CONTEXT_TTL_SECONDS,
                                                 "eligible_for_fast_watch": context is not None,
                                                 "admission_reason": admission_reason,
                                                 "true_hard_gate_failures": true_hard_failures,
                                                 "signal_quality_failures": signal_quality_failures,
                                                 "previous_state": before_alpha.state.value if before_alpha else None,
                                                 "new_state": intended_state,
                                                 "research_timestamp": research_timestamp,
                                                 "reason": admission_reason}))
            current = self.state_store.get(symbol)
            if context is not None and current and current.state in {CandidateState.WATCHLIST, CandidateState.INFRASTRUCTURE_BLOCKED}:
                self.transition(symbol, CandidateState.SETUP_FORMING, now=now,
                                event_type="SLOW_ALPHA_READY")
            elif (permanently_rejected and current
                  and current.state in {CandidateState.DISCOVERED, CandidateState.WATCHLIST, CandidateState.SETUP_FORMING}):
                self.transition(symbol, CandidateState.REJECTED, now=now,
                                event_type="SLOW_RESEARCH_REJECTED",
                                reason=admission_reason)

    def ingest_scanner_snapshot(self, snapshot: Mapping[str, Any], *, now: datetime) -> None:
        scanner = snapshot.get("scanner", {})
        market = snapshot.get("market", {})
        scanner_status = scanner.get("status") if isinstance(scanner, Mapping) else None
        if (not isinstance(market, Mapping)
                or market.get("is_regular_session") is not True
                or scanner_status != "OK"):
            # A failed/closed scan never erases a still-valid watchlist. The
            # independent TTL path expires it deterministically.
            self.emit(ScannerEvent(
                now.astimezone(timezone.utc), "ROBINHOOD_SCANNER",
                cycle_id=str(snapshot.get("generated_at") or now.astimezone(timezone.utc).isoformat()),
                payload={"result_count": 0, "status": scanner_status or "UNAVAILABLE",
                         "reason": "SCANNER_NOT_ACTIVE"},
            ))
            return
        rows = scanner.get("candidates", []) if isinstance(scanner, Mapping) else []
        self.ingest_scanner_rows(
            rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else [],
            now=now,
            cycle_id=str(snapshot.get("generated_at") or now.astimezone(timezone.utc).isoformat()),
        )

    def ingest_scanner_rows(self, scanner_rows: Sequence[Any], *, now: datetime,
                            cycle_id: str) -> None:
        """Publish discovery before slower technical/LLM research begins."""
        now = now.astimezone(timezone.utc)
        discovered = {
            str(row.get("symbol", row.get("ticker", ""))).upper()
            for row in scanner_rows if isinstance(row, Mapping) and row.get("symbol", row.get("ticker"))
        }
        self.emit(ScannerEvent(now, "ROBINHOOD_SCANNER", cycle_id=cycle_id,
                               payload={"result_count": len(discovered)}))
        with self._lock:
            for symbol in sorted(discovered):
                existing = self.state_store.get(symbol)
                terminal = existing is not None and existing.state in {
                    CandidateState.CLOSED, CandidateState.EXPIRED, CandidateState.REJECTED
                }
                cls = (
                    CandidateStillActiveEvent
                    if symbol in self._universe and not terminal
                    else CandidateDiscoveredEvent
                )
                self.emit(cls(now, "ROBINHOOD_SCANNER", symbol=symbol, cycle_id=cycle_id))
                current = self.state_store.get(symbol)
                if current and current.state == CandidateState.DISCOVERED:
                    self.transition(symbol, CandidateState.WATCHLIST, now=now,
                                    event_type="SCANNER_ADMISSION")
            for symbol in sorted(self._universe - discovered):
                current = self.state_store.get(symbol)
                if current and current.state in {CandidateState.DISCOVERED, CandidateState.WATCHLIST, CandidateState.SETUP_FORMING}:
                    self.emit(CandidateRemovedEvent(now, "ROBINHOOD_SCANNER", symbol=symbol,
                                                    cycle_id=cycle_id, payload={"reason": "REMOVED_FROM_SCANNER"}))
            self._universe = discovered

    def expire(self, symbol: str, *, now: datetime) -> None:
        self.emit(ContextExpiredEvent(now, "CONTEXT_TTL", symbol=symbol,
                                      payload={"reason": "CONTEXT_TTL"}))

    def quote(self, quote: FastQuote, *, cycle_id: str | None = None,
              position: bool = False) -> None:
        payload = {"bid": quote.bid, "ask": quote.ask, "last_price": quote.last_price,
                   "mark_price": quote.mark_price, "is_market_open": quote.is_market_open,
                   "session_close": quote.session_close.isoformat() if quote.session_close else None}
        priority = None
        if position:
            from event_driven.events import EventPriority
            priority = EventPriority.HIGH
        self.emit(QuoteEvent(quote.timestamp, quote.source, symbol=quote.symbol,
                             cycle_id=cycle_id, payload=payload, priority=priority))

    def alpha(self, symbol: str, *, now: datetime, slow: float, live: float,
              combined: float, slow_weight: float, live_weight: float,
              context_age_seconds: float,
              meaningful: bool = True) -> None:
        current = self.state_store.get(symbol)
        current_state = current.state.value if current else None
        item = AlphaUpdatedEvent(
            now, "FAST_CANDIDATE_SCORER", symbol=symbol,
            payload={"slow_alpha_score": slow, "live_market_score": live,
                     "combined_alpha_score": combined,
                     "slow_weight": slow_weight, "live_weight": live_weight,
                     "context_age_seconds": context_age_seconds,
                     "previous_state": current_state,
                     "new_state": current_state,
                     "meaningful": meaningful},
        )
        # Every fresh quote updates canonical state. Only sampled updates and
        # threshold/state changes enter the durable event queue.
        self.state_store.handle(item)
        if meaningful:
            self.bus.publish(item)

    def trade_ready(self, symbol: str, *, now: datetime, combined: float) -> None:
        current = self.state_store.get(symbol)
        self.emit(TradeCandidateEvent(now, "CANDIDATE_STATE_MACHINE", symbol=symbol,
                                      payload={"combined_alpha_score": combined,
                                               "reason": "CONFIRMED_THRESHOLD_CROSSING",
                                               "previous_state": current.state.value if current else None,
                                               "new_state": CandidateState.TRADE_READY.value}))

    def candidate_blocked(self, symbol: str, *, now: datetime, reason: str,
                          permanent: bool) -> None:
        """Project a fast-path true-hard recheck without touching a broker."""

        current = self.state_store.get(symbol)
        if current is None:
            return
        if permanent:
            if current.state != CandidateState.REJECTED:
                self.risk(symbol, now=now, approved=False,
                          reason=f"TRUE_HARD_REJECTION: {reason}")
            return
        if current.state in {
            CandidateState.DISCOVERED, CandidateState.WATCHLIST,
            CandidateState.SETUP_FORMING,
        }:
            self.transition(
                symbol, CandidateState.INFRASTRUCTURE_BLOCKED, now=now,
                event_type="FAST_TRUE_HARD_BLOCK",
                reason=f"TEMPORARILY_BLOCKED: {reason}",
            )

    def candidate_recovered(self, symbol: str, *, now: datetime) -> None:
        current = self.state_store.get(symbol)
        if current and current.state == CandidateState.INFRASTRUCTURE_BLOCKED:
            self.transition(
                symbol, CandidateState.SETUP_FORMING, now=now,
                event_type="FAST_BLOCK_CLEARED", reason="FRESH_TRUE_HARD_RECHECK_PASSED",
            )

    def risk(self, symbol: str, *, now: datetime, approved: bool, reason: str | None = None) -> None:
        cls = RiskApprovedEvent if approved else RiskRejectedEvent
        current = self.state_store.get(symbol)
        new_state = CandidateState.RISK_APPROVED if approved else CandidateState.REJECTED
        self.emit(cls(now, "DETERMINISTIC_RISK_MANAGER", symbol=symbol,
                      payload={"reason": reason,
                               "previous_state": current.state.value if current else None,
                               "new_state": new_state.value}))

    def position_opened(self, symbol: str, *, now: datetime, trade_id: str) -> None:
        current = self.state_store.get(symbol)
        self.emit(ShadowEntryEvent(now, "SHADOW_EXECUTOR", symbol=symbol,
                                   payload={"trade_id": trade_id}))
        self.emit(PositionOpenedEvent(now, "SHADOW_EXECUTOR", symbol=symbol,
                                      payload={"trade_id": trade_id,
                                               "previous_state": current.state.value if current else None,
                                               "new_state": CandidateState.POSITION_OPEN.value}))

    def position_updated(self, symbol: str, *, now: datetime, price: float) -> None:
        self.emit(PositionUpdatedEvent(now, "FAST_POSITION_WATCHER", symbol=symbol,
                                       payload={"current_price": price}))

    def position_closed(self, symbol: str, *, now: datetime, reason: str) -> None:
        current = self.state_store.get(symbol)
        cls = StopHitEvent if reason == "STOP_HIT" else TargetHitEvent if reason == "TARGET_HIT" else PositionClosedEvent
        if cls is not PositionClosedEvent:
            self.emit(cls(now, "FAST_POSITION_WATCHER", symbol=symbol, payload={"reason": reason}))
        self.emit(ExitRequestedEvent(now, "FAST_POSITION_WATCHER", symbol=symbol,
                                     payload={"reason": reason,
                                              "previous_state": current.state.value if current else None,
                                              "new_state": CandidateState.EXIT_PENDING.value}))
        self.emit(PositionClosedEvent(now, "SHADOW_EXECUTOR", symbol=symbol,
                                      payload={"reason": reason,
                                               "previous_state": current.state.value if current else None,
                                               "new_state": CandidateState.CLOSED.value}))
