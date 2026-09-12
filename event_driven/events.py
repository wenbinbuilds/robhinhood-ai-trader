"""Small typed event vocabulary; payloads contain facts, never hidden reasoning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping
from uuid import uuid4


class EventPriority(IntEnum):
    CRITICAL = 0       # stops, hard risk, position lifecycle
    HIGH = 10          # open-position quotes and risk decisions
    MEDIUM = 20        # watchlist quotes and alpha changes
    LOW = 30           # scanner/bar/context work
    BACKGROUND = 40    # news and LLM research work


class EventType(str, Enum):
    QUOTE = "QUOTE"
    BAR_CLOSED = "BAR_CLOSED"
    SCANNER = "SCANNER"
    CANDIDATE_DISCOVERED = "CANDIDATE_DISCOVERED"
    CANDIDATE_STILL_ACTIVE = "CANDIDATE_STILL_ACTIVE"
    CANDIDATE_REMOVED = "CANDIDATE_REMOVED"
    CANDIDATE_STATE_CHANGED = "CANDIDATE_STATE_CHANGED"
    CONTEXT_UPDATED = "CONTEXT_UPDATED"
    NEWS_UPDATED = "NEWS_UPDATED"
    ALPHA_UPDATED = "ALPHA_UPDATED"
    TRADE_CANDIDATE = "TRADE_CANDIDATE"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    SHADOW_ENTRY = "SHADOW_ENTRY"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    STOP_HIT = "STOP_HIT"
    TARGET_HIT = "TARGET_HIT"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    POSITION_CLOSED = "POSITION_CLOSED"
    CONTEXT_EXPIRED = "CONTEXT_EXPIRED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    timestamp: datetime
    source: str
    symbol: str | None = None
    cycle_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    priority: EventPriority | None = None

    event_type: ClassVar[EventType] = EventType.INFRASTRUCTURE_ERROR
    default_priority: ClassVar[EventPriority] = EventPriority.LOW

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        if self.symbol:
            object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if self.priority is None:
            object.__setattr__(self, "priority", self.default_priority)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "symbol": self.symbol,
            "source": self.source,
            "cycle_id": self.cycle_id,
            "event_id": self.event_id,
            "priority": self.priority.name if self.priority is not None else None,
            "payload": dict(self.payload),
        }


class QuoteEvent(MarketEvent):
    event_type = EventType.QUOTE
    default_priority = EventPriority.MEDIUM


class BarClosedEvent(MarketEvent):
    event_type = EventType.BAR_CLOSED


class ScannerEvent(MarketEvent):
    event_type = EventType.SCANNER


class CandidateDiscoveredEvent(MarketEvent):
    event_type = EventType.CANDIDATE_DISCOVERED


class CandidateStillActiveEvent(MarketEvent):
    event_type = EventType.CANDIDATE_STILL_ACTIVE


class CandidateRemovedEvent(MarketEvent):
    event_type = EventType.CANDIDATE_REMOVED


class CandidateStateChangedEvent(MarketEvent):
    event_type = EventType.CANDIDATE_STATE_CHANGED
    default_priority = EventPriority.MEDIUM


class ContextUpdatedEvent(MarketEvent):
    event_type = EventType.CONTEXT_UPDATED
    default_priority = EventPriority.BACKGROUND


class NewsUpdatedEvent(MarketEvent):
    event_type = EventType.NEWS_UPDATED
    default_priority = EventPriority.BACKGROUND


class AlphaUpdatedEvent(MarketEvent):
    event_type = EventType.ALPHA_UPDATED
    default_priority = EventPriority.MEDIUM


class TradeCandidateEvent(MarketEvent):
    event_type = EventType.TRADE_CANDIDATE
    default_priority = EventPriority.HIGH


class RiskApprovedEvent(MarketEvent):
    event_type = EventType.RISK_APPROVED
    default_priority = EventPriority.HIGH


class RiskRejectedEvent(MarketEvent):
    event_type = EventType.RISK_REJECTED
    default_priority = EventPriority.CRITICAL


class ShadowEntryEvent(MarketEvent):
    event_type = EventType.SHADOW_ENTRY
    default_priority = EventPriority.HIGH


class PositionOpenedEvent(MarketEvent):
    event_type = EventType.POSITION_OPENED
    default_priority = EventPriority.CRITICAL


class PositionUpdatedEvent(MarketEvent):
    event_type = EventType.POSITION_UPDATED
    default_priority = EventPriority.HIGH


class StopHitEvent(MarketEvent):
    event_type = EventType.STOP_HIT
    default_priority = EventPriority.CRITICAL


class TargetHitEvent(MarketEvent):
    event_type = EventType.TARGET_HIT
    default_priority = EventPriority.CRITICAL


class ExitRequestedEvent(MarketEvent):
    event_type = EventType.EXIT_REQUESTED
    default_priority = EventPriority.CRITICAL


class PositionClosedEvent(MarketEvent):
    event_type = EventType.POSITION_CLOSED
    default_priority = EventPriority.CRITICAL


class ContextExpiredEvent(MarketEvent):
    event_type = EventType.CONTEXT_EXPIRED
    default_priority = EventPriority.HIGH


class InfrastructureErrorEvent(MarketEvent):
    event_type = EventType.INFRASTRUCTURE_ERROR
    default_priority = EventPriority.CRITICAL


EVENT_CLASSES = {
    cls.event_type: cls
    for cls in (
        QuoteEvent, BarClosedEvent, ScannerEvent, CandidateDiscoveredEvent,
        CandidateStillActiveEvent, CandidateRemovedEvent, CandidateStateChangedEvent, ContextUpdatedEvent,
        NewsUpdatedEvent, AlphaUpdatedEvent, TradeCandidateEvent,
        RiskApprovedEvent, RiskRejectedEvent, ShadowEntryEvent,
        PositionOpenedEvent, PositionUpdatedEvent, StopHitEvent, TargetHitEvent,
        ExitRequestedEvent, PositionClosedEvent, ContextExpiredEvent,
        InfrastructureErrorEvent,
    )
}
