"""Thread-safe factual market state and explicit candidate lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence
import json

from event_driven.events import (
    AlphaUpdatedEvent,
    BarClosedEvent,
    CandidateDiscoveredEvent,
    CandidateRemovedEvent,
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
    TradeCandidateEvent,
)
from watcher.storage import atomic_json


class CandidateState(str, Enum):
    DISCOVERED = "DISCOVERED"
    WATCHLIST = "WATCHLIST"
    SETUP_FORMING = "SETUP_FORMING"
    TRADE_READY = "TRADE_READY"
    RISK_APPROVED = "RISK_APPROVED"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    INFRASTRUCTURE_BLOCKED = "INFRASTRUCTURE_BLOCKED"


LEGAL_TRANSITIONS: Mapping[CandidateState, frozenset[CandidateState]] = {
    CandidateState.DISCOVERED: frozenset({CandidateState.WATCHLIST, CandidateState.EXPIRED, CandidateState.REJECTED, CandidateState.INFRASTRUCTURE_BLOCKED}),
    CandidateState.WATCHLIST: frozenset({CandidateState.SETUP_FORMING, CandidateState.EXPIRED, CandidateState.REJECTED, CandidateState.INFRASTRUCTURE_BLOCKED}),
    CandidateState.SETUP_FORMING: frozenset({CandidateState.WATCHLIST, CandidateState.TRADE_READY, CandidateState.EXPIRED, CandidateState.REJECTED, CandidateState.INFRASTRUCTURE_BLOCKED}),
    CandidateState.TRADE_READY: frozenset({CandidateState.SETUP_FORMING, CandidateState.RISK_APPROVED, CandidateState.REJECTED, CandidateState.EXPIRED, CandidateState.INFRASTRUCTURE_BLOCKED}),
    CandidateState.RISK_APPROVED: frozenset({CandidateState.POSITION_OPEN, CandidateState.REJECTED, CandidateState.INFRASTRUCTURE_BLOCKED}),
    CandidateState.POSITION_OPEN: frozenset({CandidateState.EXIT_PENDING}),
    CandidateState.EXIT_PENDING: frozenset({CandidateState.POSITION_OPEN, CandidateState.CLOSED}),
    CandidateState.INFRASTRUCTURE_BLOCKED: frozenset({CandidateState.WATCHLIST, CandidateState.SETUP_FORMING, CandidateState.EXPIRED, CandidateState.REJECTED}),
    CandidateState.CLOSED: frozenset(),
    CandidateState.EXPIRED: frozenset(),
    CandidateState.REJECTED: frozenset(),
}


@dataclass(slots=True)
class StateTransition:
    timestamp: str
    previous_state: str | None
    new_state: str
    event_type: str
    reason: str | None = None


@dataclass(slots=True)
class MarketState:
    symbol: str
    episode_id: str = ''
    state: CandidateState = CandidateState.DISCOVERED
    latest_quote: dict[str, Any] | None = None
    latest_quote_timestamp: str | None = None
    completed_5m_candles: list[dict[str, Any]] = field(default_factory=list)
    slow_technical_metrics: dict[str, Any] = field(default_factory=dict)
    llm_context: dict[str, Any] = field(default_factory=dict)
    news_context: dict[str, Any] = field(default_factory=dict)
    sector_context: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    live_market_score: float | None = None
    slow_alpha_score: float | None = None
    combined_alpha_score: float | None = None
    technical_score: float | None = None
    technical_confidence: float | None = None
    qualitative_score: float | None = None
    news_score: float | None = None
    sector_score: float | None = None
    market_score: float | None = None
    slow_weight: float | None = None
    live_weight: float | None = None
    context_age_seconds: float | None = None
    research_timestamp: str | None = None
    context_expiration: str | None = None
    research_price: float | None = None
    current_price: float | None = None
    eligible_for_fast_watch: bool = False
    admission_reason: str | None = None
    true_hard_gate_failures: list[str] = field(default_factory=list)
    signal_quality_failures: list[str] = field(default_factory=list)
    open_position_context_status: str | None = None
    position_invalidation_signals: list[str] = field(default_factory=list)
    entry_reference: float | None = None
    stop_reference: float | None = None
    target_reference: float | None = None
    discovery_timestamp: str | None = None
    watchlist_timestamp: str | None = None
    trade_ready_timestamp: str | None = None
    last_event: str | None = None
    last_reason: str | None = None
    transition_history: list[StateTransition] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MarketState":
        transitions = [
            StateTransition(**item) for item in value.get("transition_history", [])
            if isinstance(item, Mapping)
        ]
        fields = {name: value[name] for name in cls.__dataclass_fields__ if name in value}
        fields["state"] = CandidateState(fields.get("state", CandidateState.DISCOVERED))
        fields["transition_history"] = transitions
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


class IllegalStateTransition(ValueError):
    pass


class CandidateStateStore:
    """No execution logic: only current facts and legal state transitions."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.lock = RLock()
        self.states: dict[str, MarketState] = {}
        self.portfolio = None
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            return
        for row in value.get("candidates", []):
            try:
                item = MarketState.from_dict(row)
            except (TypeError, ValueError, KeyError):
                continue
            if item.symbol:
                self.states[item.symbol.upper()] = item

    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            atomic_json(self.path, {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [self.states[key].to_dict() for key in sorted(self.states)],
            })

    def snapshot(self, *, include_terminal: bool = True) -> list[MarketState]:
        with self.lock:
            values = [MarketState.from_dict(item.to_dict()) for item in self.states.values()]
        if include_terminal:
            return values
        terminal = {CandidateState.CLOSED, CandidateState.EXPIRED, CandidateState.REJECTED}
        return [item for item in values if item.state not in terminal]

    def get(self, symbol: str) -> MarketState | None:
        with self.lock:
            item = self.states.get(symbol.upper())
            return MarketState.from_dict(item.to_dict()) if item else None

    def project_position(self, symbol, state, *, now, episode_id, reason):
        """Only reconciliation projects canonical positions; never creates one."""
        with self.lock:
            item = self.states.setdefault(symbol, MarketState(symbol))
            previous = item.state.value
            item.state = state
            item.episode_id = episode_id or item.episode_id
            item.eligible_for_fast_watch = False
            item.transition_history.append(StateTransition(now.isoformat(), previous, state.value, 'STATE_RECONCILED', reason))
            item.last_event, item.last_reason = 'STATE_RECONCILED', reason
            self.save()

    def discover(self, symbol: str, *, timestamp: datetime, event_type: str = "CANDIDATE_DISCOVERED") -> MarketState:
        symbol = symbol.upper()
        with self.lock:
            item = self.states.get(symbol)
            if item is None or item.state in {CandidateState.CLOSED, CandidateState.EXPIRED, CandidateState.REJECTED}:
                item = MarketState(symbol=symbol, discovery_timestamp=timestamp.astimezone(timezone.utc).isoformat())
                item.transition_history.append(StateTransition(timestamp.astimezone(timezone.utc).isoformat(), None, CandidateState.DISCOVERED.value, event_type))
                self.states[symbol] = item
            item.last_event = event_type
            self.save()
            return MarketState.from_dict(item.to_dict())

    def recover_open_position(self, symbol: str, *, entry_timestamp: str,
                              transition_history: Sequence[Mapping[str, Any]] = ()) -> MarketState:
        """Project a persisted local position without inventing market events."""
        symbol = symbol.upper()
        with self.lock:
            current = self.states.get(symbol)
            if current is not None and current.state == CandidateState.POSITION_OPEN:
                return MarketState.from_dict(current.to_dict())
            parsed_history = []
            for row in transition_history:
                try:
                    parsed_history.append(StateTransition(
                        timestamp=str(row["timestamp"]),
                        previous_state=row.get("previous_state"),
                        new_state=str(row["new_state"]),
                        event_type=str(row.get("event_type", "PERSISTED_ATTRIBUTION")),
                        reason=row.get("reason"),
                    ))
                except KeyError:
                    continue
            parsed_history.append(StateTransition(
                timestamp=entry_timestamp, previous_state=None,
                new_state=CandidateState.POSITION_OPEN.value,
                event_type="RECOVERED_OPEN_POSITION",
                reason="LOCAL_SHADOW_STATE_RESTART",
            ))
            if current is None:
                self.states[symbol] = MarketState(
                    symbol=symbol, state=CandidateState.POSITION_OPEN,
                    discovery_timestamp=entry_timestamp,
                    last_event="RECOVERED_OPEN_POSITION",
                    transition_history=parsed_history,
                )
            else:
                current.transition_history.append(StateTransition(
                    timestamp=entry_timestamp,
                    previous_state=current.state.value,
                    new_state=CandidateState.POSITION_OPEN.value,
                    event_type="RECOVERED_OPEN_POSITION",
                    reason="LOCAL_SHADOW_STATE_RECONCILIATION",
                ))
                current.state = CandidateState.POSITION_OPEN
                current.last_event = "RECOVERED_OPEN_POSITION"
                current.last_reason = "LOCAL_SHADOW_STATE_RECONCILIATION"
            self.save()
            return MarketState.from_dict(self.states[symbol].to_dict())

    def transition(self, symbol: str, new_state: CandidateState, *, timestamp: datetime,
                   event_type: str, reason: str | None = None) -> MarketState:
        symbol = symbol.upper()
        canonical_open = self.portfolio.has_symbol(symbol) if self.portfolio is not None else None
        with self.lock:
            if symbol not in self.states:
                raise IllegalStateTransition(f"{symbol} has not been discovered")
            item = self.states[symbol]
            old = item.state
            if self.portfolio is not None:
                if canonical_open and new_state not in {CandidateState.POSITION_OPEN, CandidateState.EXIT_PENDING}:
                    return MarketState.from_dict(item.to_dict())
                if not canonical_open and new_state in {CandidateState.POSITION_OPEN, CandidateState.EXIT_PENDING}:
                    return MarketState.from_dict(item.to_dict())
            if new_state == old:
                item.last_event, item.last_reason = event_type, reason
                return MarketState.from_dict(item.to_dict())
            if new_state not in LEGAL_TRANSITIONS[old]:
                raise IllegalStateTransition(f"illegal candidate transition: {old.value} -> {new_state.value}")
            at = timestamp.astimezone(timezone.utc).isoformat()
            item.state = new_state
            item.last_event, item.last_reason = event_type, reason
            item.transition_history.append(StateTransition(at, old.value, new_state.value, event_type, reason))
            if new_state == CandidateState.WATCHLIST and item.watchlist_timestamp is None:
                item.watchlist_timestamp = at
            if new_state == CandidateState.TRADE_READY and item.trade_ready_timestamp is None:
                item.trade_ready_timestamp = at
            self.save()
            return MarketState.from_dict(item.to_dict())

    def update_facts(self, symbol: str, **fields: Any) -> MarketState:
        with self.lock:
            item = self.states[symbol.upper()]
            for name, value in fields.items():
                if name not in MarketState.__dataclass_fields__ or name in {"symbol", "state", "transition_history"}:
                    raise ValueError(f"unsupported state field: {name}")
                setattr(item, name, value)
            self.save()
            return MarketState.from_dict(item.to_dict())

    def handle(self, item: MarketEvent) -> None:
        """Short deterministic event projection suitable for a bus handler."""
        symbol = item.symbol
        if isinstance(item, CandidateDiscoveredEvent) and symbol:
            self.discover(symbol, timestamp=item.timestamp, event_type=item.event_type.value)
            return
        if not symbol or symbol not in self.states:
            return
        payload = dict(item.payload)
        current_episode = self.states[symbol].episode_id
        if item.episode_id and current_episode and item.episode_id != current_episode:
            return  # A delayed old-episode event cannot mutate a new setup.
        if self.portfolio is not None:
            is_open = self.portfolio.has_symbol(symbol)
            if is_open and isinstance(item, (RiskRejectedEvent, ContextExpiredEvent, TradeCandidateEvent, CandidateRemovedEvent)):
                return
            if isinstance(item, PositionOpenedEvent) and not is_open:
                return
            if isinstance(item, PositionClosedEvent) and is_open:
                return
            if isinstance(item, (PositionOpenedEvent, PositionClosedEvent)):
                self.project_position(symbol, CandidateState.POSITION_OPEN if is_open else CandidateState.CLOSED,
                                      now=item.timestamp, episode_id=item.episode_id,
                                      reason=item.event_type.value)
                return
        if isinstance(item, QuoteEvent):
            self.update_facts(
                symbol, latest_quote=payload,
                latest_quote_timestamp=item.timestamp.isoformat(),
                current_price=payload.get("mark_price"),
                last_event=item.event_type.value,
            )
        elif isinstance(item, BarClosedEvent):
            candles = payload.get("completed_5m_candles", [])
            self.update_facts(symbol, completed_5m_candles=list(candles) if isinstance(candles, Sequence) else [], slow_technical_metrics=dict(payload.get("technical_metrics", {})), last_event=item.event_type.value)
        elif isinstance(item, ContextUpdatedEvent):
            self.update_facts(
                symbol,
                llm_context=dict(payload.get("llm_context", {})),
                sector_context=dict(payload.get("sector_context", {})),
                market_context=dict(payload.get("market_context", {})),
                research_timestamp=payload.get("research_timestamp"),
                context_expiration=payload.get("context_expiration"),
                research_price=payload.get("research_price", payload.get("entry_reference")),
                entry_reference=payload.get("entry_reference"),
                stop_reference=payload.get("stop_reference"),
                target_reference=payload.get("target_reference"),
                last_event=item.event_type.value,
            )
        elif isinstance(item, NewsUpdatedEvent):
            self.update_facts(symbol, news_context=payload, last_event=item.event_type.value)
        elif isinstance(item, AlphaUpdatedEvent):
            updates = {
                name: payload[name]
                for name in (
                    "slow_alpha_score", "live_market_score",
                    "combined_alpha_score", "technical_score",
                    "technical_confidence",
                    "qualitative_score", "news_score", "sector_score",
                    "market_score", "slow_weight", "live_weight",
                    "context_age_seconds", "eligible_for_fast_watch",
                    "admission_reason",
                    "true_hard_gate_failures", "signal_quality_failures",
                    "open_position_context_status", "position_invalidation_signals",
                )
                if name in payload
            }
            self.update_facts(symbol, **updates, last_event=item.event_type.value)
        elif isinstance(item, CandidateRemovedEvent):
            current = self.states[symbol].state
            if current in {CandidateState.DISCOVERED, CandidateState.WATCHLIST, CandidateState.SETUP_FORMING}:
                self.transition(symbol, CandidateState.EXPIRED, timestamp=item.timestamp, event_type=item.event_type.value, reason="REMOVED_FROM_SCANNER")
        elif isinstance(item, ContextExpiredEvent):
            self.transition(symbol, CandidateState.EXPIRED, timestamp=item.timestamp, event_type=item.event_type.value, reason=payload.get("reason", "CONTEXT_TTL"))
        elif isinstance(item, TradeCandidateEvent):
            self.transition(symbol, CandidateState.TRADE_READY, timestamp=item.timestamp, event_type=item.event_type.value, reason=payload.get("reason"))
        elif isinstance(item, RiskApprovedEvent):
            self.transition(symbol, CandidateState.RISK_APPROVED, timestamp=item.timestamp, event_type=item.event_type.value)
        elif isinstance(item, RiskRejectedEvent):
            self.transition(symbol, CandidateState.REJECTED, timestamp=item.timestamp, event_type=item.event_type.value, reason=payload.get("reason"))
        elif isinstance(item, PositionOpenedEvent):
            self.transition(symbol, CandidateState.POSITION_OPEN, timestamp=item.timestamp, event_type=item.event_type.value)
        elif isinstance(item, ExitRequestedEvent):
            self.transition(symbol, CandidateState.EXIT_PENDING, timestamp=item.timestamp,
                            event_type=item.event_type.value, reason=payload.get("reason"))
        elif isinstance(item, PositionUpdatedEvent):
            self.update_facts(symbol, latest_quote=payload, latest_quote_timestamp=item.timestamp.isoformat(), last_event=item.event_type.value)
        elif isinstance(item, PositionClosedEvent):
            current = self.states[symbol].state
            if current == CandidateState.POSITION_OPEN:
                self.transition(symbol, CandidateState.EXIT_PENDING, timestamp=item.timestamp, event_type="EXIT_REQUESTED")
            self.transition(symbol, CandidateState.CLOSED, timestamp=item.timestamp, event_type=item.event_type.value, reason=payload.get("reason"))
