"""Bounded meaningful-event logging and concise terminal projection."""

from __future__ import annotations

from pathlib import Path

from event_driven.events import AlphaUpdatedEvent, MarketEvent, QuoteEvent
from event_driven.state import CandidateStateStore
from watcher.storage import event as append_event


class StructuredEventLogger:
    def __init__(self, path: str | Path, state_store: CandidateStateStore | None = None) -> None:
        self.path = Path(path)
        self.state_store = state_store

    def __call__(self, item: MarketEvent) -> None:
        # Raw 2-second quotes are intentionally omitted. Alpha is logged only
        # when the producer marks it meaningful (sample/crossing/transition).
        if isinstance(item, QuoteEvent):
            return
        payload = dict(item.payload)
        state = self.state_store.get(item.symbol) if self.state_store and item.symbol else None
        append_event(
            self.path,
            item.event_type.value,
            item.timestamp,
            symbol=item.symbol,
            event_id=item.event_id,
            cycle_id=item.cycle_id,
            previous_state=payload.get("previous_state"),
            new_state=(payload.get("new_state") or (state.state.value if state else None)),
            slow_score=payload.get("slow_alpha_score", state.slow_alpha_score if state else None),
            live_score=payload.get("live_market_score", state.live_market_score if state else None),
            combined_score=payload.get("combined_alpha_score", state.combined_alpha_score if state else None),
            technical_score=payload.get("technical_score", state.technical_score if state else None),
            technical_confidence=payload.get("technical_confidence", state.technical_confidence if state else None),
            qualitative_score=payload.get("qualitative_score", state.qualitative_score if state else None),
            news_score=payload.get("news_score", state.news_score if state else None),
            sector_score=payload.get("sector_score", state.sector_score if state else None),
            market_score=payload.get("market_score", state.market_score if state else None),
            context_age_seconds=payload.get("context_age_seconds", state.context_age_seconds if state else None),
            true_hard_gate_failures=payload.get("true_hard_gate_failures", state.true_hard_gate_failures if state else []),
            signal_quality_failures=payload.get("signal_quality_failures", state.signal_quality_failures if state else []),
            candidate_state=(payload.get("new_state") or (state.state.value if state else None)),
            admission_reason=payload.get("admission_reason", state.admission_reason if state else None),
            reason=payload.get("reason"),
        )


def concise_event_line(item: MarketEvent) -> str | None:
    if isinstance(item, QuoteEvent):
        return None
    stamp = item.timestamp.strftime("%H:%M:%S")
    symbol = f" {item.symbol}" if item.symbol else ""
    payload = dict(item.payload)
    if isinstance(item, AlphaUpdatedEvent):
        transition = ""
        if payload.get("previous_state") or payload.get("new_state"):
            transition = (
                f" {payload.get('previous_state')} → {payload.get('new_state')}"
            )
        if item.source == "SLOW_ALPHA":
            return (
                f"[{stamp}]{symbol}{transition} slow={payload.get('slow_alpha_score')} "
                f"context_ttl={payload.get('context_ttl_seconds')}s "
                f"admission={payload.get('reason')}"
            )
        return (
            f"[{stamp}]{symbol}{transition} live={payload.get('live_market_score')} "
            f"slow_weight={payload.get('slow_weight')} live_weight={payload.get('live_weight')} "
            f"combined={payload.get('combined_alpha_score')}"
        )
    reason = f" ({payload.get('reason')})" if payload.get("reason") else ""
    return f"[{stamp}]{symbol} {item.event_type.value}{reason}"
