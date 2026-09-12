"""Meaningful-event cache in front of the slow Codex reasoning provider."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping, Sequence
import json

from agent.llm_reasoning_bridge import LlmReasoningProvider
from agent.models import LlmReasoningResult


class EventDrivenReasoningProvider:
    """Invoke the LLM only when bar/news/sector/macro evidence changed.

    Fast quotes never reach this class. Cached context is reused only for the
    same candidate set and meaningful slow-evidence fingerprint.
    """

    def __init__(self, provider: LlmReasoningProvider) -> None:
        self.provider = provider
        self._lock = RLock()
        self._fingerprint: str | None = None
        self._result: LlmReasoningResult | None = None
        self.invocations = 0
        self.cache_hits = 0

    @staticmethod
    def _fingerprint_payload(payload: Mapping[str, Any], expected_symbols: Sequence[str]) -> str:
        rows = []
        for row in payload.get("candidates", []):
            if not isinstance(row, Mapping):
                continue
            technical = row.get("deterministic_technical_metrics", {})
            news = row.get("deterministic_news_event_clusters", [])
            sector = row.get("deterministic_sector_classification", {})
            rows.append({
                "symbol": row.get("symbol"),
                "latest_completed_bar_timestamp": technical.get("latest_completed_bar_timestamp") if isinstance(technical, Mapping) else None,
                "technical_disposition": row.get("deterministic_proposed_setup", {}).get("technical_disposition") if isinstance(row.get("deterministic_proposed_setup"), Mapping) else None,
                "technical_score": technical.get("technical_validation", {}).get("technical_score") if isinstance(technical, Mapping) and isinstance(technical.get("technical_validation"), Mapping) else None,
                "ema_relationship": technical.get("ema_relationship") if isinstance(technical, Mapping) else None,
                "recent_candle_structure": technical.get("recent_5_minute_candle_structure") if isinstance(technical, Mapping) else None,
                "news_event_ids": sorted(
                    str(item.get("event_id")) for item in news
                    if isinstance(item, Mapping) and item.get("event_id")
                ) if isinstance(news, Sequence) else [],
                "sector": sector.get("sector") if isinstance(sector, Mapping) else None,
                "sector_bias": sector.get("bias") if isinstance(sector, Mapping) else None,
            })
        broad = payload.get("broad_market_context", {})
        macro = broad.get("deterministic_interpretation", {}) if isinstance(broad, Mapping) else {}
        value = {
            "symbols": sorted(str(symbol).upper() for symbol in expected_symbols),
            "market_regime": macro.get("regime") if isinstance(macro, Mapping) else None,
            "candidates": sorted(rows, key=lambda item: str(item.get("symbol"))),
        }
        return sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def reason(self, payload: Mapping[str, Any], *, expected_symbols: Sequence[str], now) -> LlmReasoningResult:
        fingerprint = self._fingerprint_payload(payload, expected_symbols)
        with self._lock:
            if self._fingerprint == fingerprint and self._result is not None and self._result.status == "AVAILABLE":
                self.cache_hits += 1
                trace = replace(
                    self._result.trace,
                    reasoning_duration_seconds=0.0,
                    status="CACHED",
                    diagnostics={"trigger": "NO_MEANINGFUL_SLOW_EVENT", "cache_hit": True},
                )
                return replace(self._result, trace=trace)
        result = self.provider.reason(payload, expected_symbols=expected_symbols, now=now)
        with self._lock:
            self.invocations += 1
            if result.status == "AVAILABLE":
                self._fingerprint = fingerprint
                self._result = result
        return result
