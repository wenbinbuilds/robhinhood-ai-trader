"""Compatibility-safe identities for the only two trading strategies.

Persisted V1 records used several lineage names.  They remain readable, while
all human-facing surfaces use exactly POSITION and SCALP.
"""

from __future__ import annotations

from typing import Any


POSITION = "POSITION"
SCALP = "SCALP"

# Keep the proven persistence/event identifiers stable.  A display-name
# migration must not make old journals or episode IDs unreadable.
POSITION_INTERNAL_ID = "MOMENTUM"
SCALP_INTERNAL_ID = "SCALP"

POSITION_ALIASES = frozenset({
    POSITION, POSITION_INTERNAL_ID, "MOMENTUM_V1", "INTRADAY_MOMENTUM_V1",
})
SCALP_ALIASES = frozenset({SCALP, SCALP_INTERNAL_ID, "SCALP_V1"})


def strategy_display_name(value: Any) -> str:
    """Map every supported legacy/internal value to a user-facing name."""

    normalized = str(value or "").strip().upper()
    if normalized in SCALP_ALIASES:
        return SCALP
    # Unknown/empty old records belong to the original POSITION lineage.  The
    # model constructors call this only after exhausting strategy_id/strategy.
    return POSITION


def is_scalp_strategy(value: Any) -> bool:
    return str(value or "").strip().upper() in SCALP_ALIASES


def is_position_strategy(value: Any) -> bool:
    return not is_scalp_strategy(value)


def strategy_of(record: Any) -> str:
    """Return POSITION/SCALP for a model object or mapping."""

    if isinstance(record, dict):
        raw = record.get("strategy_id") or record.get("strategy")
    else:
        raw = getattr(record, "strategy_id", None) or getattr(record, "strategy", None)
    return strategy_display_name(raw)

