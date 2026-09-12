"""Read-only projection of cycle evidence for a dashboard."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from watcher.status import shadow_dashboard_projection  # read-only two-speed dashboard API


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def reasoning_dashboard_projection(cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Separate factual Python evidence from model interpretation."""

    raw = cycle.get("analyzed_candidates", [])
    candidates = (
        [item for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else []
    )
    return {
        "mode": cycle.get("mode"),
        "reasoning_trace": cycle.get("llm_reasoning"),
        "candidates": [
            {
                "symbol": item.get("symbol"),
                "factual_deterministic_data": {
                    "scanner_rank": item.get("preliminary_scanner_rank"),
                    "scanner_score": item.get("preliminary_scanner_score"),
                    "technical_metrics": item.get("deterministic_technical_metrics"),
                    "technical_context": item.get("technical_context"),
                    "news_event_clusters": item.get("news_event_clusters"),
                    "sector_context": item.get("sector_context"),
                    "coordinator_inputs": item.get("deterministic_coordinator_inputs"),
                    "coordinator_weights": item.get("coordinator_weights"),
                    "coordinator_vetoes": item.get("coordinator_vetoes"),
                },
                "llm_interpretation": {
                    "news": item.get("llm_news_analysis"),
                    "sector": item.get("llm_sector_analysis"),
                    "macro": item.get("llm_macro_analysis"),
                    "qualitative": item.get("llm_qualitative_analysis"),
                },
                "deterministic_coordinator_score": _mapping(
                    item.get("coordinator_decision")
                ).get("combined_score"),
                "final_decision": item.get("decision"),
            }
            for item in candidates
        ],
        "controls": [],
    }
