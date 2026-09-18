"""Repair display projections from canonical local position records."""
from datetime import datetime


class ReconciliationService:
    def __init__(self, portfolio, candidate_store, emit):
        self.portfolio, self.candidate_store, self.emit = portfolio, candidate_store, emit

    def reconcile(self, symbol: str, *, now: datetime):
        from event_driven.state import CandidateState
        from event_driven.events import StateReconciledEvent
        symbol = symbol.upper()
        canonical = self.portfolio.snapshot()
        position = next((p for p in canonical.open_positions if p.symbol == symbol), None)
        closed = next((p for p in reversed(canonical.closed_positions) if p.symbol == symbol), None)
        projection = self.candidate_store.get(symbol)
        old = projection.state.value if projection else None
        if position:
            desired = 'POSITION_OPEN'
            episode_id = getattr(position, 'episode_id', '')
        elif old in {'POSITION_OPEN', 'EXIT_PENDING'}:
            desired = 'CLOSED' if closed else 'EXPIRED'
            episode_id = getattr(closed, 'episode_id', '') if closed else ''
        else:
            return False
        if old != desired or (position and getattr(projection, 'episode_id', '') != episode_id):
            self.candidate_store.project_position(
                symbol, CandidateState(desired), now=now, episode_id=episode_id,
                reason='CANONICAL_PORTFOLIO_RECONCILIATION',
            )
            self.emit(StateReconciledEvent(now, 'POSITION_RECONCILER', symbol=symbol,
                payload=dict(old_candidate_state=old, portfolio_state=desired,
                             reason=('PORTFOLIO_OPEN_STATE_NOT_OPEN' if position else 'STATE_OPEN_PORTFOLIO_NOT_OPEN'), episode_id=episode_id)))
        return position is not None
