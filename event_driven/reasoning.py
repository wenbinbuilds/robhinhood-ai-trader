"""Per-candidate semantic-evidence cache for slow qualitative research."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping, Sequence
import json

import config
from agent.llm_reasoning_bridge import LlmReasoningProvider
from agent.models import LlmCandidateAnalysis, LlmReasoningResult, ReasoningTrace


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class _CacheEntry:
    fingerprint: str
    evidence: Mapping[str, Any]
    candidate: LlmCandidateAnalysis
    generated_at: datetime
    trace: ReasoningTrace


class EventDrivenReasoningProvider:
    """Batch semantic cache misses; quote and numeric indicator churn is ignored."""

    def __init__(self, provider: LlmReasoningProvider, *, ttl_seconds: float | None = None) -> None:
        self.provider = provider
        self.ttl_seconds = float(
            config.LLM_QUALITATIVE_CONTEXT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        )
        if self.ttl_seconds <= 0:
            raise ValueError("qualitative cache TTL must be positive")
        self._lock = RLock()
        self._cache: dict[str, _CacheEntry] = {}
        self.invocations = 0
        self.cache_hits = 0

    @staticmethod
    def _semantic_evidence(row: Mapping[str, Any], broad_market: Mapping[str, Any]) -> dict[str, Any]:
        technical = _mapping(row.get("technical_summary", row.get("deterministic_proposed_setup")))
        news = row.get("news_events", row.get("deterministic_news_event_clusters", []))
        news_context = _mapping(row.get("news_context", row.get("deterministic_news_context")))
        sector = _mapping(row.get("sector_context", row.get("deterministic_sector_classification")))
        macro = _mapping(broad_market.get("deterministic_interpretation"))
        events = []
        if isinstance(news, Sequence) and not isinstance(news, (str, bytes)):
            for item in news:
                if not isinstance(item, Mapping):
                    continue
                sources = item.get("sources", [])
                urls = sorted(
                    str(source.get("url"))
                    for source in sources
                    if isinstance(source, Mapping) and source.get("url")
                ) if isinstance(sources, Sequence) else []
                events.append({
                    "event_id": item.get("event_id"),
                    "catalyst_type": item.get("catalyst_type"),
                    "published_at": item.get("published_at"),
                    "source_urls": urls,
                })
        return {
            "symbol": str(row.get("symbol", "")).upper(),
            "news_events": sorted(events, key=lambda item: str(item.get("event_id"))),
            "catalyst_type": news_context.get("catalyst_type"),
            "catalyst_found": news_context.get("catalyst_found"),
            "sector": sector.get("sector"),
            "sector_regime": sector.get("sector_bias", sector.get("bias")),
            "sector_driver": sector.get("sector_driver"),
            "market_regime": macro.get("regime"),
            "spy_bias": macro.get("spy_bias"),
            "qqq_bias": macro.get("qqq_bias"),
            "volatility_context": macro.get("volatility_context"),
            "technical_disposition": technical.get("technical_disposition"),
        }

    @staticmethod
    def _fingerprint(evidence: Mapping[str, Any]) -> str:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _fingerprint_payload(cls, payload: Mapping[str, Any], expected_symbols: Sequence[str]) -> str:
        """Stable debug fingerprint that excludes candles and numeric indicators."""
        broad = _mapping(payload.get("broad_market_context"))
        rows = {
            str(row.get("symbol", "")).upper(): row
            for row in payload.get("candidates", []) if isinstance(row, Mapping)
        }
        evidence = [
            cls._semantic_evidence(rows.get(symbol, {}), broad)
            for symbol in sorted(str(item).upper() for item in expected_symbols)
        ]
        return cls._fingerprint({"candidates": evidence})

    @staticmethod
    def _trigger_reason(previous: _CacheEntry | None, evidence: Mapping[str, Any], *, expired: bool) -> str:
        if previous is None:
            return "NEW_CANDIDATE"
        if expired:
            return "QUALITATIVE_CONTEXT_EXPIRED"
        old = previous.evidence
        if old.get("news_events") != evidence.get("news_events") or any(
            old.get(key) != evidence.get(key) for key in ("catalyst_type", "catalyst_found")
        ):
            return "NEW_MATERIAL_NEWS"
        if any(old.get(key) != evidence.get(key) for key in ("sector", "sector_regime", "sector_driver")):
            return "SECTOR_REGIME_CHANGE"
        if any(old.get(key) != evidence.get(key) for key in (
            "market_regime", "spy_bias", "qqq_bias", "volatility_context",
        )):
            return "MARKET_REGIME_CHANGE"
        if old.get("technical_disposition") != evidence.get("technical_disposition"):
            return "TECHNICAL_DISPOSITION_CHANGE"
        return "SEMANTIC_EVIDENCE_CHANGED"

    def reason(self, payload: Mapping[str, Any], *, expected_symbols: Sequence[str], now) -> LlmReasoningResult:
        current = _utc(now)
        symbols = tuple(str(item).upper() for item in expected_symbols)
        broad = _mapping(payload.get("broad_market_context"))
        rows = {
            str(row.get("symbol", "")).upper(): row
            for row in payload.get("candidates", []) if isinstance(row, Mapping)
        }
        evidence = {symbol: self._semantic_evidence(rows.get(symbol, {}), broad) for symbol in symbols}
        fingerprints = {symbol: self._fingerprint(evidence[symbol]) for symbol in symbols}
        hits: dict[str, _CacheEntry] = {}
        prior_entries: dict[str, _CacheEntry] = {}
        miss_reasons: dict[str, str] = {}
        with self._lock:
            for symbol in symbols:
                prior = self._cache.get(symbol)
                if prior is not None:
                    prior_entries[symbol] = prior
                age = (current - prior.generated_at).total_seconds() if prior is not None else None
                expired = age is not None and age > self.ttl_seconds
                if prior is not None and not expired and prior.fingerprint == fingerprints[symbol]:
                    hits[symbol] = prior
                else:
                    miss_reasons[symbol] = self._trigger_reason(prior, evidence[symbol], expired=bool(expired))

        misses = tuple(symbol for symbol in symbols if symbol not in hits)
        self.cache_hits += len(hits)
        per_candidate = {
            symbol: {
                "cache_hit": symbol in hits,
                "status": "CACHE_HIT" if symbol in hits else "REFRESH_REQUIRED",
                "reason": "QUALITATIVE_CONTEXT_UNCHANGED" if symbol in hits else miss_reasons[symbol],
                "generated_at": hits[symbol].generated_at.isoformat() if symbol in hits else None,
                "cache_age_seconds": (
                    round((current - hits[symbol].generated_at).total_seconds(), 3)
                    if symbol in hits else None
                ),
            }
            for symbol in symbols
        }

        if not misses:
            first = hits[symbols[0]]
            trace = replace(
                first.trace,
                reasoning_invocation_timestamp=first.generated_at.isoformat(),
                reasoning_duration_seconds=0.0, candidate_count=len(symbols), status="CACHED",
                diagnostics={
                    "cache_hit": True, "cache_hits": len(hits), "cache_misses": 0,
                    "qualitative_cache_ttl_seconds": self.ttl_seconds,
                    "per_candidate": per_candidate,
                },
            )
            return LlmReasoningResult(
                status="AVAILABLE",
                candidates=tuple(hits[symbol].candidate for symbol in symbols), trace=trace,
            )

        miss_payload = dict(payload)
        miss_payload["candidates"] = [rows[symbol] for symbol in misses if symbol in rows]
        result = self.provider.reason(miss_payload, expected_symbols=misses, now=current)
        self.invocations += 1
        isolation_failures = (
            "LLM_REASONING_SCHEMA_VIOLATION",
            "LLM_REASONING_TYPED_PARSE_FAILED",
            "LLM_REASONING_MISSING_CANDIDATE",
            "LLM_REASONING_DUPLICATE_SYMBOL",
        )
        if (
            result.status != "AVAILABLE" and len(misses) > 1
            and any(str(result.failure_reason or "").startswith(code) for code in isolation_failures)
        ):
            isolated = []
            failed_symbols = {}
            last_success = None
            for symbol in misses:
                single_payload = dict(payload)
                single_payload["candidates"] = [rows[symbol]] if symbol in rows else []
                single = self.provider.reason(
                    single_payload, expected_symbols=(symbol,), now=current
                )
                self.invocations += 1
                if single.status == "AVAILABLE" and single.by_symbol().get(symbol) is not None:
                    isolated.extend(single.candidates)
                    last_success = single
                else:
                    failed_symbols[symbol] = single.failure_reason
            if isolated and last_success is not None:
                partial_diagnostics = dict(last_success.trace.diagnostics or {})
                partial_diagnostics.update({
                    "failure_isolation": True,
                    "failed_symbols": failed_symbols,
                    "batch_failure_reason": result.failure_reason,
                })
                result = LlmReasoningResult(
                    status="AVAILABLE", candidates=tuple(isolated),
                    trace=replace(
                        last_success.trace, status="PARTIAL_SUCCESS",
                        candidate_count=len(isolated), diagnostics=partial_diagnostics,
                    ),
                    failure_reason=(
                        "PARTIAL_LLM_FAILURE:" + ",".join(sorted(failed_symbols))
                        if failed_symbols else None
                    ),
                )
        if result.status != "AVAILABLE":
            if not config.LLM_QUALITATIVE_CACHE_ALLOW_FAILURE_FALLBACK:
                return result
            fallback = {
                symbol: prior_entries[symbol]
                for symbol in misses if symbol in prior_entries
                and (current - prior_entries[symbol].generated_at).total_seconds() <= self.ttl_seconds
            }
            if len(fallback) != len(misses):
                return result
            hits.update(fallback)
            for symbol in misses:
                per_candidate[symbol] = {
                    "cache_hit": True, "status": "CACHE_FALLBACK",
                    "reason": miss_reasons[symbol], "fallback_reason": result.failure_reason,
                    "generated_at": fallback[symbol].generated_at.isoformat(),
                    "cache_age_seconds": round((current - fallback[symbol].generated_at).total_seconds(), 3),
                }
            trace = replace(
                result.trace, status="CACHED_FALLBACK", failure_reason=None,
                candidate_count=len(symbols), diagnostics={
                    "cache_hit": True, "cache_hits": len(symbols), "cache_misses": 0,
                    "fallback_reason": result.failure_reason, "per_candidate": per_candidate,
                },
            )
            return LlmReasoningResult(
                status="AVAILABLE", candidates=tuple(hits[symbol].candidate for symbol in symbols), trace=trace,
            )

        generated_at = current
        try:
            generated_at = _utc(datetime.fromisoformat(
                result.trace.reasoning_invocation_timestamp.replace("Z", "+00:00")
            ))
        except (ValueError, AttributeError):
            pass
        fresh = result.by_symbol()
        with self._lock:
            for symbol in misses:
                candidate = fresh.get(symbol)
                if candidate is None:
                    continue
                entry = _CacheEntry(fingerprints[symbol], evidence[symbol], candidate, generated_at, result.trace)
                self._cache[symbol] = entry
                hits[symbol] = entry
                per_candidate[symbol].update(
                    status="REFRESHED", generated_at=generated_at.isoformat(), cache_age_seconds=0.0,
                )
        if any(symbol not in hits for symbol in symbols):
            return result
        diagnostics = dict(result.trace.diagnostics or {})
        diagnostics.update({
            "cache_hit": len(misses) < len(symbols),
            "cache_hits": len(symbols) - len(misses), "cache_misses": len(misses),
            "qualitative_cache_ttl_seconds": self.ttl_seconds,
            "per_candidate": per_candidate,
        })
        trace = replace(result.trace, candidate_count=len(symbols), diagnostics=diagnostics)
        return LlmReasoningResult(
            status="AVAILABLE", candidates=tuple(hits[symbol].candidate for symbol in symbols), trace=trace,
        )
