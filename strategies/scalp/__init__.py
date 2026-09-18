"""Deterministic, local-only short-hold strategy."""

from strategies.scalp.models import ScalpEntryDecision, ScalpEpisode, ScalpFeatures

STRATEGY_ID = 'SCALP'
__all__ = ['ScalpEntryDecision', 'ScalpEpisode', 'ScalpFeatures', 'STRATEGY_ID']
