"""Pure multi-timescale alpha combination; risk is deliberately absent."""

from __future__ import annotations

from dataclasses import dataclass

import config


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True, slots=True)
class AlphaResult:
    slow_alpha_score: float
    live_market_score: float
    combined_alpha_score: float
    slow_weight: float
    live_weight: float
    context_age_seconds: float
    expired: bool


class AlphaCombiner:
    """Linearly decay slow authority over the existing context TTL.

    Elapsed time changes authority, never alpha itself. Fresh factual quote
    evidence supplies the live component. Entries are forbidden at/after TTL.
    """

    def __init__(self, *, ttl_seconds: float | None = None,
                 slow_weight_fresh: float | None = None,
                 slow_weight_expiring: float | None = None) -> None:
        self.ttl_seconds = float(config.CANDIDATE_CONTEXT_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
        self.slow_weight_fresh = float(config.SLOW_WEIGHT_AT_RESEARCH if slow_weight_fresh is None else slow_weight_fresh)
        self.slow_weight_expiring = float(config.SLOW_WEIGHT_AT_EXPIRATION if slow_weight_expiring is None else slow_weight_expiring)
        if self.ttl_seconds <= 0:
            raise ValueError("context TTL must be positive")
        if not 0 <= self.slow_weight_fresh <= 1 or not 0 <= self.slow_weight_expiring <= 1:
            raise ValueError("alpha weights must be normalized")

    def weights(self, context_age_seconds: float) -> tuple[float, float]:
        fraction = _clamp(max(0.0, context_age_seconds) / self.ttl_seconds)
        slow = self.slow_weight_fresh + (
            self.slow_weight_expiring - self.slow_weight_fresh
        ) * fraction
        slow = _clamp(slow)
        return slow, 1.0 - slow

    def combine(self, slow_alpha_score: float, live_market_score: float,
                *, context_age_seconds: float) -> AlphaResult:
        slow, live = self.weights(context_age_seconds)
        score = slow * _clamp(slow_alpha_score) + live * _clamp(live_market_score)
        return AlphaResult(
            slow_alpha_score=round(_clamp(slow_alpha_score), 6),
            live_market_score=round(_clamp(live_market_score), 6),
            combined_alpha_score=round(_clamp(score), 6),
            slow_weight=round(slow, 6),
            live_weight=round(live, 6),
            context_age_seconds=round(max(0.0, context_age_seconds), 3),
            expired=context_age_seconds >= self.ttl_seconds,
        )
