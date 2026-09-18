"""Explain infrastructure failures separately from investment decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import config


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _score(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and result not in (float("inf"), float("-inf")) else 0.0


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def classify_decision(decision, candidates, reasoning_status):
    if not isinstance(decision, dict) or decision.get("type") not in {"NO_TRADE", "WATCH"}:
        return
    candidate_rows = _rows(candidates)
    infrastructure = []
    quote_vetoes = {
        "QUOTE_TIMESTAMP_MISSING", "QUOTE_TIMESTAMP_INVALID",
        "QUOTE_TIMESTAMP_FUTURE", "QUOTE_STALE",
    }
    if decision.get("reason") in {"STALE_DATA", *quote_vetoes}:
        infrastructure.append(str(decision["reason"]))
    if reasoning_status != "AVAILABLE" and any(
        "LLM_REASONING_UNAVAILABLE" in _strings(row.get("coordinator_vetoes"))
        for row in candidate_rows
    ):
        infrastructure.append("LLM_REASONING_UNAVAILABLE")
    for row in candidate_rows:
        infrastructure.extend(
            reason for reason in _strings(row.get("reasons"))
            if reason in quote_vetoes
            or reason == "required market data is missing or insufficient"
        )
    if infrastructure and decision["type"] == "NO_TRADE":
        decision.update(type="INFRASTRUCTURE_BLOCKED", decision_reason="INFRASTRUCTURE_VETO",
                        infrastructure_vetoes=list(dict.fromkeys(infrastructure)))
    else:
        decision["decision_reason"] = "STRATEGY_REJECTION" if decision["type"] == "NO_TRADE" else "STRATEGY_WATCH"


def cycle_diagnostic_lines(result):
    """Render best-effort diagnostics; malformed optional data is never fatal."""

    try:
        root = _mapping(result)
        reasoning = _mapping(root.get("llm_reasoning"))
        trace = _mapping(reasoning.get("trace"))
        raw_rows = root.get("analyzed_candidates")
        rows = _rows(raw_rows)
        malformed = raw_rows is not None and (
            not isinstance(raw_rows, Sequence)
            or isinstance(raw_rows, (str, bytes))
            or len(rows) != len(raw_rows)
        )
        yield f"LLM REASONING: {reasoning.get('status', 'UNAVAILABLE')}"
        yield f"LLM DURATION: {trace.get('reasoning_duration_seconds', 'UNAVAILABLE')}s"
        diagnostics = _mapping(trace.get("diagnostics"))
        profile = _mapping(diagnostics.get("profile"))
        yield (
            "LLM RESEARCH: "
            f"model={profile.get('model', trace.get('model_identifier', 'UNAVAILABLE'))} "
            f"effort={profile.get('reasoning_effort', config.CODEX_REASONING_EFFORT)} "
            f"candidates={trace.get('candidate_count', 0)} "
            f"cache_hits={diagnostics.get('cache_hits', 0)} "
            f"cache_misses={diagnostics.get('cache_misses', trace.get('candidate_count', 0))} "
            f"prompt_tokens_est={profile.get('input_tokens_estimate', 0)} "
            f"duration={trace.get('reasoning_duration_seconds', 0)}s"
        )
        if profile:
            yield (
                "LLM PROFILE: "
                f"input_chars={profile.get('input_chars')} "
                f"startup_ms={profile.get('startup_ms')} "
                f"inference_ms={profile.get('inference_ms')} "
                f"parse_ms={profile.get('parse_ms')} "
                f"total_ms={profile.get('total_ms')} "
                f"output_tokens_est={profile.get('output_tokens_estimate')}"
            )
        per_candidate = _mapping(diagnostics.get("per_candidate"))
        for symbol, cache in per_candidate.items():
            detail = _mapping(cache)
            yield (
                f"{symbol} QUALITATIVE CONTEXT: {detail.get('status')} "
                f"reason={detail.get('reason')} "
                f"age_seconds={detail.get('cache_age_seconds')}"
            )
        yield f"CANDIDATES ANALYZED: {len(rows)}"
        yield f"VALID TECHNICAL SETUPS: {sum(row.get('technical_disposition') in {'QUALIFIED', 'MONITORABLE'} for row in rows)}"
        yield f"TRADE THRESHOLD: {config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD}"
        if rows:
            top = max(
                rows,
                key=lambda row: _score(_mapping(row.get("coordinator_decision")).get("combined_score")),
            )
            scores = _mapping(top.get("coordinator_decision"))
            yield f"TOP CANDIDATE: {top.get('symbol', 'UNAVAILABLE')}"
            metrics = _mapping(top.get("deterministic_technical_metrics"))
            yield f"QUOTE FRESHNESS SOURCE: {metrics.get('quote_freshness_source', 'unavailable')}"
            age = metrics.get("quote_age_at_analysis_seconds")
            yield f"QUOTE AGE: {round(age, 3) if isinstance(age, (int, float)) and not isinstance(age, bool) else 'UNAVAILABLE'} seconds"
            yield f"CANDLES: {metrics.get('analysis_candle_count', 0)}"
            yield f"TECHNICAL DATA VALID: {'YES' if metrics.get('technical_data_valid') else 'NO'}"
            validation = _mapping(metrics.get("technical_validation"))
            rules = {
                rule.get("rule_name"): rule
                for rule in _rows(validation.get("rules"))
                if rule.get("rule_name")
            }
            yield "TECHNICAL VALIDATION:"
            for name in ("PRICE_VS_VWAP", "EMA_STRUCTURE", "RSI", "MACD", "RELATIVE_VOLUME", "CANDLE_STRUCTURE", "SPREAD"):
                rule = _mapping(rules.get(name))
                yield f"{name}: {rule.get('status', 'UNAVAILABLE')} ({rule.get('type', 'UNKNOWN')})"
            yield f"TRUE HARD GATES: {validation.get('hard_gates_passed', 0)}/{validation.get('hard_gates_evaluated', 0)} PASS"
            failed = _strings(validation.get("true_hard_gate_failures", validation.get("failed_hard_gates")))
            warnings = _strings(validation.get("signal_quality_failures"))
            yield f"TRUE HARD FAILURES: {', '.join(failed) if failed else 'NONE'}"
            yield f"SIGNAL QUALITY WARNINGS: {', '.join(warnings) if warnings else 'NONE'}"
            yield f"TECHNICAL CONFIDENCE: {validation.get('technical_confidence', validation.get('confidence'))}"
            yield f"TECHNICAL SCORE: {_mapping(top.get('technical_context')).get('technical_score')}"
            yield f"TECHNICAL SETUP: {top.get('technical_disposition', 'UNKNOWN')}"
            yield f"LLM QUALITATIVE SCORE: {scores.get('qualitative_score')}"
            yield f"COMBINED SCORE: {scores.get('combined_score')}"
            decision = _mapping(root.get("decision"))
            vetoes = _strings(decision.get("infrastructure_vetoes")) or _strings(top.get("coordinator_vetoes"))
            decision_type = decision.get("type")
            primary = vetoes[0] if vetoes else "BELOW_THRESHOLD" if decision_type in {"NO_TRADE", "WATCH"} else "NONE"
            yield f"PRIMARY REJECTION: {primary}"
        for row in rows:
            metrics = _mapping(row.get("deterministic_technical_metrics"))
            yield f"CANDIDATE DATA: {row.get('symbol', 'UNAVAILABLE')} quote_age={metrics.get('quote_age_at_analysis_seconds')}s candles={metrics.get('analysis_candle_count')}"
        if malformed:
            yield "DIAGNOSTICS STATUS: DEGRADED (malformed candidate rows ignored)"
    except Exception as exc:  # Diagnostics must never terminate the trading loop.
        yield f"DIAGNOSTICS STATUS: DEGRADED ({type(exc).__name__}; safe fallback rendered)"
