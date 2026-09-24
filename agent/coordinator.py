"""Single transparent coordinator for analysis evidence; no execution APIs."""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

import config
from agent.gate_policy import failed_gates
from agent.models import (
    CoordinatorDecision,
    LlmCandidateAnalysis,
    MarketContext,
    NewsContext,
    SectorContext,
    TechnicalAssessment,
)


class CoordinatorAgent:
    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        self.weights = dict(weights or config.COORDINATOR_WEIGHTS)
        required = {"technical", "news", "sector", "market", "qualitative"}
        if set(self.weights) != required:
            raise ValueError(
                "coordinator weights must cover technical/news/sector/market/qualitative"
            )
        if abs(sum(self.weights.values()) - 1.0) > 1e-9 or any(
            value < 0 for value in self.weights.values()
        ):
            raise ValueError("coordinator weights must be nonnegative and sum to 1")

    def decide(
        self,
        symbol: str,
        *,
        market: MarketContext,
        news: NewsContext,
        technical: TechnicalAssessment,
        sector: SectorContext,
        llm_analysis: LlmCandidateAnalysis | None = None,
        llm_failure_reason: str | None = None,
        risk_result: Mapping[str, Any] | None = None,
    ) -> CoordinatorDecision:
        plan = technical.candidate_plan
        llm_available = llm_analysis is not None
        if llm_analysis is None:
            news_score = news.score
            sector_score = sector.score
            market_score = market.score
            # Missing qualitative evidence is not negative evidence.  The
            # separate deterministic veto below still fails closed, while the
            # diagnostic score remains semantically neutral instead of being
            # silently converted to zero quality.
            qualitative_score = 0.5
        else:
            news_score = self._llm_news_score(llm_analysis)
            sector_score = self._bias_score(
                llm_analysis.sector_analysis.sector_bias
            )
            market_score = self._bias_score(
                llm_analysis.macro_analysis.market_bias
            )
            qualitative = llm_analysis.qualitative_analysis
            qualitative_score = (
                0.30 * qualitative.setup_quality
                + 0.25 * qualitative.catalyst_quality
                + 0.30 * qualitative.continuation_probability_score
                + 0.15 * (1.0 - qualitative.conflicting_evidence_severity)
            )
        scores = {
            "technical": self._bounded_score(
                technical.context.technical_score, "technical"
            ),
            "news": self._bounded_score(news_score, "news"),
            "sector": self._bounded_score(sector_score, "sector"),
            "market": self._bounded_score(market_score, "market"),
            "qualitative": self._bounded_score(
                qualitative_score, "qualitative"
            ),
        }
        combined = sum(scores[key] * self.weights[key] for key in self.weights)
        score_breakdown = {
            key: {
                "value": scores[key],
                "semantic": (
                    "0=negative, 0.5=neutral, 1=positive"
                    if key != "technical"
                    else "0=weak, 0.5=monitorable, 1=strong"
                ),
                "weight": self.weights[key],
                "contribution": scores[key] * self.weights[key],
            }
            for key in self.weights
        }
        reasons_for = [
            *technical.context.evidence,
            *(news.event_clusters[0].summary.splitlines()[:1] if news.event_clusters else []),
            *sector.evidence,
            *market.evidence,
        ]
        reasons_against = list(technical.context.conflicts)
        uncertainties: list[str] = []
        vetoes: list[str] = []
        validation = (
            technical.metrics.get("technical_validation", {})
            if isinstance(technical.metrics, Mapping) else {}
        )
        true_hard_failures, signal_quality_failures = (
            failed_gates(validation) if isinstance(validation, Mapping) else
            (["TECHNICAL_VALIDATION_MALFORMED"], [])
        )

        if llm_analysis is None:
            vetoes.append("LLM_REASONING_UNAVAILABLE")
            uncertainties.append(llm_failure_reason or "qualitative reasoning unavailable")
        else:
            qualitative = llm_analysis.qualitative_analysis
            reasons_for.extend(qualitative.reasons_for)
            reasons_against.extend(qualitative.reasons_against)
            uncertainties.extend(qualitative.key_uncertainties)
            if llm_analysis.news_analysis.status == "UNAVAILABLE":
                uncertainties.append("NEWS_UNAVAILABLE")
            if qualitative.proposed_direction == "SHORT":
                vetoes.append("short-only LLM proposal while shorting is disabled")
            llm_news = llm_analysis.news_analysis
            if (
                llm_news.sentiment in {"NEGATIVE", "VERY_NEGATIVE"}
                and llm_news.importance >= config.NEWS_NEGATIVE_VETO_IMPORTANCE
            ):
                vetoes.append("major negative catalyst conflicts with a long setup")

        required_missing = {
            "current_price", "bid", "ask", "volume",
            "relative_volume", "vwap", "ema9", "ema20", "rsi14", "macd",
            "macd_signal", "quote_freshness", "sufficient_5_minute_candles",
        } & set(technical.context.unavailable_fields)
        if required_missing and not true_hard_failures:
            # Backward-compatible fail-closed fallback for an assessment that
            # predates the structured gate audit. Structured results use the
            # exact TRUE_HARD_REJECTION rule below instead of a second veto.
            vetoes.append("stale or insufficient technical market data")
        if news.sentiment in {"NEGATIVE", "VERY_NEGATIVE"} and news.importance >= config.NEWS_NEGATIVE_VETO_IMPORTANCE:
            vetoes.append("major negative catalyst conflicts with a long setup")
        if plan.get("direction") == "SHORT" and not config.ALLOW_SHORTING:
            vetoes.append("bearish-only technical setup while shorting is disabled")
        vetoes.extend(
            f"TRUE_HARD_REJECTION: {name}" for name in true_hard_failures
        )
        disposition = plan.get("technical_disposition")
        monitorable = disposition in {"QUALIFIED", "MONITORABLE"}
        if disposition == "REJECTED" and not true_hard_failures:
            # Legacy/unstructured assessments remain fail-closed.
            vetoes.append("TRUE_HARD_REJECTION: TECHNICAL_VALIDATION")
        entry = self._number(plan.get("entry"))
        stop = self._number(plan.get("stop"))
        target = self._number(plan.get("target"))
        reward_ratio = self._number(plan.get("risk_reward_ratio"))
        if monitorable and (
            entry is None
            or stop is None
            or target is None
            or not (0 < stop < entry < target)
        ):
            vetoes.append("invalid deterministic entry/stop/target")
        if (
            monitorable
            and (reward_ratio is None or reward_ratio < config.MIN_RISK_REWARD_RATIO)
        ):
            vetoes.append("risk/reward below deterministic minimum")
        if risk_result is not None and risk_result.get("approved") is not True:
            vetoes.append("deterministic risk manager rejected the candidate")
            reasons_against.extend(str(item) for item in risk_result.get("reasons", []))

        if vetoes:
            decision = "NO_TRADE"
        elif combined < config.COORDINATOR_NO_TRADE_THRESHOLD:
            decision = "NO_TRADE"
        elif combined < config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:
            decision = "WATCH"
        elif monitorable:
            decision = "TRADE_CANDIDATE"
        else:
            decision = "WATCH" if technical.context.technical_score >= 0.5 else "NO_TRADE"

        reasons_against.extend(vetoes)
        include_plan = decision == "TRADE_CANDIDATE"
        return CoordinatorDecision(
            symbol=symbol.upper(), decision=decision,
            combined_score=round(combined, 3),
            market_score=round(market_score, 3), news_score=round(news_score, 3),
            technical_score=technical.context.technical_score,
            sector_score=round(sector_score, 3),
            qualitative_score=round(qualitative_score, 3),
            weights=dict(self.weights),
            entry=plan.get("entry") if include_plan else None,
            stop=plan.get("stop") if include_plan else None,
            target=plan.get("target") if include_plan else None,
            risk_per_share=plan.get("risk_per_share") if include_plan else None,
            risk_reward_ratio=plan.get("risk_reward_ratio") if include_plan else None,
            thesis=plan.get("thesis") if include_plan else None,
            invalidation_condition=plan.get("invalidation_condition") if include_plan else None,
            confidence=round(
                min(
                    float(technical.context.confidence),
                    max(0.3, qualitative_score),
                )
                if llm_available
                else 0.0,
                3,
            ),
            reasons_for=tuple(dict.fromkeys(str(item) for item in reasons_for if item)),
            reasons_against=tuple(dict.fromkeys(reasons_against)),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            vetoes=tuple(dict.fromkeys(vetoes)),
            llm_reasoning_available=llm_available,
            risk_manager_approved=(None if risk_result is None else risk_result.get("approved") is True),
            technical_confidence=round(float(technical.context.confidence), 3),
            true_hard_gate_failures=tuple(true_hard_failures),
            signal_quality_failures=tuple(signal_quality_failures),
            score_breakdown=score_breakdown,
        )

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result else None

    @staticmethod
    def _bounded_score(value: Any, name: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} score must be a finite number") from exc
        if not isfinite(result) or not 0.0 <= result <= 1.0:
            raise ValueError(f"{name} score must be within [0, 1]")
        return result

    @staticmethod
    def _bias_score(value: str) -> float:
        return {
            "SUPPORTS": 0.8,
            "NEUTRAL": 0.5,
            "CONFLICTS": 0.2,
            "UNKNOWN": 0.4,
        }.get(value, 0.4)

    @staticmethod
    def _llm_news_score(value: LlmCandidateAnalysis) -> float:
        news = value.news_analysis
        if news.status == "UNAVAILABLE":
            return 0.35
        sentiment = {
            "VERY_POSITIVE": 1.0,
            "POSITIVE": 0.8,
            "NEUTRAL": 0.5,
            "MIXED": 0.5,
            "NEGATIVE": 0.2,
            "VERY_NEGATIVE": 0.0,
            "UNKNOWN": 0.4,
        }.get(news.sentiment, 0.4)
        evidence_strength = (
            0.4 * news.importance
            + 0.3 * news.freshness_score
            + 0.3 * news.source_quality_score
        )
        return round(0.5 + (sentiment - 0.5) * evidence_strength, 3)

    @staticmethod
    def veto(decision: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """Apply a deterministic execution-simulation veto through the coordinator."""

        result = dict(decision)
        result["decision"] = "NO_TRADE"
        result["entry"] = None
        result["stop"] = None
        result["target"] = None
        result["risk_per_share"] = None
        result["risk_reward_ratio"] = None
        result["thesis"] = None
        result["invalidation_condition"] = None
        result["reasons_against"] = list(
            dict.fromkeys([*result.get("reasons_against", []), reason])
        )
        result["executed"] = False
        result["analysis_only"] = True
        return result
