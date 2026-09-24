"""Explain infrastructure failures separately from investment decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
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


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def position_watch_candidate_traces(result) -> list[dict[str, Any]]:
    """Extract post-score POSITION gates without changing cycle decisions."""

    traces = []
    for row in _rows(_mapping(result).get('analyzed_candidates')):
        coordinator = _mapping(row.get('coordinator_decision'))
        combined = coordinator.get('combined_score')
        if not isinstance(combined, (int, float)) or combined < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:
            continue
        metrics = _mapping(row.get('deterministic_technical_metrics'))
        validation = _mapping(metrics.get('technical_validation'))
        rules = {
            item.get('rule_name'): item for item in _rows(validation.get('rules'))
            if item.get('rule_name')
        }
        resistance_rule = _mapping(rules.get('RESISTANCE_ABOVE_ENTRY'))
        resistance_actual = _mapping(resistance_rule.get('actual'))
        entry = _score(resistance_actual.get('entry')) or None
        resistance = _score(resistance_actual.get('resistance')) or None
        stop = _mapping(rules.get('STOP_REFERENCE_AVAILABLE')).get('actual')
        rr = _mapping(rules.get('MINIMUM_RISK_REWARD')).get('actual')
        indicators = _mapping(row.get('supporting_indicators'))
        support = indicators.get('intraday_support_reference')
        target = row.get('target')
        if target is None:
            target = resistance
        hard_results = {
            str(name): _mapping(rule).get('status', 'UNAVAILABLE')
            for name, rule in rules.items()
            if _mapping(rule).get('type') == 'HARD'
        }
        hard_failures = _strings(validation.get(
            'true_hard_gate_failures', coordinator.get('true_hard_gate_failures')
        ))
        signal_warnings = _strings(validation.get(
            'signal_quality_failures', coordinator.get('signal_quality_failures')
        ))
        quote_at = _timestamp(metrics.get('quote_as_of'))
        candle_at = _timestamp(metrics.get('latest_completed_bar_timestamp'))
        candle_close = candle_at + timedelta(minutes=5) if candle_at else None
        geometry_at = _timestamp(metrics.get('analysis_started_at'))
        coherent = bool(
            quote_at and candle_close and geometry_at
            and quote_at >= candle_close and geometry_at >= quote_at
        )
        quote_age = metrics.get('quote_age_at_analysis_seconds')
        quote_fresh = (
            isinstance(quote_age, (int, float))
            and 0 <= quote_age <= config.SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS
        )
        distance = (
            resistance-entry
            if isinstance(resistance, (int, float)) and isinstance(entry, (int, float))
            else None
        )
        risk_distance = (
            entry-float(stop)
            if isinstance(entry, (int, float)) and isinstance(stop, (int, float))
            and entry > float(stop) else None
        )
        minimum_rr_distance = (
            config.MIN_RISK_REWARD_RATIO*risk_distance
            if risk_distance is not None else None
        )
        final_state = (
            'HARD_GATE_REJECTED' if hard_failures
            else str(coordinator.get('decision', row.get('decision', 'UNKNOWN')))
        )
        traces.append({
            'symbol': row.get('symbol'),
            'technical_score': coordinator.get('technical_score'),
            'combined_score': combined,
            'watch_threshold': config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE,
            'trade_threshold': config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
            'quote_age': quote_age,
            'quote_fresh': quote_fresh,
            'entry_reference_price': entry,
            'support': support,
            'resistance': resistance,
            'resistance_distance_from_entry': distance,
            'resistance_distance_percent': (
                distance/entry if distance is not None and entry else None
            ),
            'minimum_resistance_above_entry_distance': 0.0,
            'minimum_required_resistance_distance_for_RR': minimum_rr_distance,
            'minimum_resistance_price_for_RR': (
                entry+minimum_rr_distance
                if entry is not None and minimum_rr_distance is not None else None
            ),
            'stop': stop,
            'target': target,
            'gross_RR': rr,
            'net_RR': None,
            'hard_gate_results': hard_results,
            'final_state': final_state,
            'primary_block': hard_failures[0] if hard_failures else (
                'BELOW_TRADE_THRESHOLD'
                if combined < config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD else 'NONE'
            ),
            'secondary_blocks': [*hard_failures[1:], *signal_warnings],
            'quote_timestamp': metrics.get('quote_as_of'),
            'source_candle_timestamp': metrics.get('latest_completed_bar_timestamp'),
            'source_candle_close_timestamp': candle_close.isoformat() if candle_close else None,
            'geometry_timestamp': metrics.get('analysis_started_at'),
            'coherent_geometry_timestamps': coherent,
        })
    return traces


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
        for position_trace in position_watch_candidate_traces(root):
            yield "[POSITION WATCH CANDIDATE] " + " ".join(
                f"{name}={value}" for name, value in position_trace.items()
            )
        if malformed:
            yield "DIAGNOSTICS STATUS: DEGRADED (malformed candidate rows ignored)"
    except Exception as exc:  # Diagnostics must never terminate the trading loop.
        yield f"DIAGNOSTICS STATUS: DEGRADED ({type(exc).__name__}; safe fallback rendered)"
