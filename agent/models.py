"""Typed, JSON-serializable evidence exchanged by analysis-only agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


class StructuredModel:
    """Small dataclass serialization mixin without runtime dependencies."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]


@dataclass(frozen=True)
class MarketContext(StructuredModel):
    regime: str
    spy_bias: str
    qqq_bias: str
    volatility_context: str | None
    confidence: float
    score: float
    evidence: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsEventCluster(StructuredModel):
    event_id: str
    catalyst_type: str
    sentiment: str
    importance: float
    freshness_minutes: float | None
    source_count: int
    source_quality: str
    summary: str
    sources: tuple[Mapping[str, Any], ...] = ()
    deduplicated_article_count: int = 0


@dataclass(frozen=True)
class NewsContext(StructuredModel):
    symbol: str
    catalyst_found: bool
    catalyst_type: str
    sentiment: str
    importance: float
    freshness_minutes: float | None
    source_count: int
    event_id: str | None
    event_cluster: str | None
    summary: str
    confidence: float
    score: float
    sources: tuple[Mapping[str, Any], ...] = ()
    event_clusters: tuple[NewsEventCluster, ...] = ()
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TechnicalContext(StructuredModel):
    symbol: str
    trend: str
    price_vs_vwap: str
    ema_structure: str
    rsi_state: str
    macd_state: str
    volume_confirmation: str
    price_action_state: str
    technical_score: float
    confidence: float
    evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TechnicalAssessment(StructuredModel):
    context: TechnicalContext
    candidate_plan: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SectorContext(StructuredModel):
    symbol: str
    sector: str
    sector_bias: str
    sector_driver: str | None
    confidence: float
    score: float
    evidence: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoordinatorDecision(StructuredModel):
    symbol: str
    decision: str
    combined_score: float
    market_score: float
    news_score: float
    technical_score: float
    sector_score: float
    qualitative_score: float
    weights: Mapping[str, float]
    entry: float | None
    stop: float | None
    target: float | None
    risk_per_share: float | None
    risk_reward_ratio: float | None
    thesis: str | None
    invalidation_condition: str | None
    confidence: float
    reasons_for: tuple[str, ...] = ()
    reasons_against: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    llm_reasoning_available: bool = False
    risk_manager_approved: bool | None = None
    analysis_only: bool = True
    executed: bool = False
    technical_confidence: float | None = None
    true_hard_gate_failures: tuple[str, ...] = ()
    signal_quality_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class LlmNewsEvent(StructuredModel):
    event_id: str
    summary: str
    published_at: str | None
    age_minutes: float | None
    source_quality: str
    sources: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class LlmNewsAnalysis(StructuredModel):
    status: str
    catalyst_found: bool
    catalyst_type: str
    sentiment: str
    importance: float
    freshness_score: float
    source_quality_score: float
    explains_price_move: bool | None
    event_summary: str
    supporting_events: tuple[LlmNewsEvent, ...] = ()
    conflicting_events: tuple[LlmNewsEvent, ...] = ()


@dataclass(frozen=True)
class LlmSectorAnalysis(StructuredModel):
    sector: str
    sector_bias: str
    reasoning: str
    important_sector_drivers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LlmMacroAnalysis(StructuredModel):
    market_bias: str
    reasoning: str


@dataclass(frozen=True)
class LlmQualitativeAnalysis(StructuredModel):
    proposed_direction: str
    setup_quality: float
    catalyst_quality: float
    continuation_probability_score: float
    conflicting_evidence_severity: float
    reasons_for: tuple[str, ...] = ()
    reasons_against: tuple[str, ...] = ()
    key_uncertainties: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class LlmCandidateAnalysis(StructuredModel):
    symbol: str
    news_analysis: LlmNewsAnalysis
    sector_analysis: LlmSectorAnalysis
    macro_analysis: LlmMacroAnalysis
    qualitative_analysis: LlmQualitativeAnalysis

    @staticmethod
    def _event(value: Mapping[str, Any]) -> LlmNewsEvent:
        return LlmNewsEvent(
            event_id=str(value["event_id"]),
            summary=str(value["summary"]),
            published_at=(
                str(value["published_at"])
                if value.get("published_at") is not None
                else None
            ),
            age_minutes=(
                float(value["age_minutes"])
                if value.get("age_minutes") is not None
                else None
            ),
            source_quality=str(value["source_quality"]),
            sources=tuple(value.get("sources", ())),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LlmCandidateAnalysis:
        news = value["news_analysis"]
        sector = value["sector_analysis"]
        macro = value["macro_analysis"]
        qualitative = value["qualitative_analysis"]
        return cls(
            symbol=str(value["symbol"]),
            news_analysis=LlmNewsAnalysis(
                status=str(news["status"]),
                catalyst_found=bool(news["catalyst_found"]),
                catalyst_type=str(news["catalyst_type"]),
                sentiment=str(news["sentiment"]),
                importance=float(news["importance"]),
                freshness_score=float(news["freshness_score"]),
                source_quality_score=float(news["source_quality_score"]),
                explains_price_move=news.get("explains_price_move"),
                event_summary=str(news["event_summary"]),
                supporting_events=tuple(
                    cls._event(item) for item in news["supporting_events"]
                ),
                conflicting_events=tuple(
                    cls._event(item) for item in news["conflicting_events"]
                ),
            ),
            sector_analysis=LlmSectorAnalysis(
                sector=str(sector["sector"]),
                sector_bias=str(sector["sector_bias"]),
                reasoning=str(sector["reasoning"]),
                important_sector_drivers=tuple(sector["important_sector_drivers"]),
            ),
            macro_analysis=LlmMacroAnalysis(
                market_bias=str(macro["market_bias"]),
                reasoning=str(macro["reasoning"]),
            ),
            qualitative_analysis=LlmQualitativeAnalysis(
                proposed_direction=str(qualitative["proposed_direction"]),
                setup_quality=float(qualitative["setup_quality"]),
                catalyst_quality=float(qualitative["catalyst_quality"]),
                continuation_probability_score=float(
                    qualitative["continuation_probability_score"]
                ),
                conflicting_evidence_severity=float(
                    qualitative["conflicting_evidence_severity"]
                ),
                reasons_for=tuple(qualitative["reasons_for"]),
                reasons_against=tuple(qualitative["reasons_against"]),
                key_uncertainties=tuple(qualitative["key_uncertainties"]),
                summary=str(qualitative["summary"]),
            ),
        )


@dataclass(frozen=True)
class ReasoningTrace(StructuredModel):
    reasoning_provider: str
    model_identifier: str | None
    reasoning_invocation_timestamp: str
    reasoning_duration_seconds: float
    candidate_count: int
    schema_version: str
    prompt_version: str
    status: str
    failure_reason: str | None
    token_usage: Mapping[str, Any] | None = None
    diagnostics: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class LlmReasoningResult(StructuredModel):
    status: str
    candidates: tuple[LlmCandidateAnalysis, ...]
    trace: ReasoningTrace
    failure_reason: str | None = None

    def by_symbol(self) -> dict[str, LlmCandidateAnalysis]:
        return {item.symbol: item for item in self.candidates}
