"""Explain infrastructure failures separately from investment decisions."""
import config


def classify_decision(decision, candidates, reasoning_status):
    if decision["type"] not in {"NO_TRADE", "WATCH"}:
        return
    infrastructure = []
    quote_vetoes = {
        "QUOTE_TIMESTAMP_MISSING", "QUOTE_TIMESTAMP_INVALID",
        "QUOTE_TIMESTAMP_FUTURE", "QUOTE_STALE",
    }
    if decision.get("reason") in {"STALE_DATA", *quote_vetoes}:
        infrastructure.append(str(decision["reason"]))
    if reasoning_status != "AVAILABLE" and any("LLM_REASONING_UNAVAILABLE" in row.get("coordinator_vetoes", []) for row in candidates):
        infrastructure.append("LLM_REASONING_UNAVAILABLE")
    for row in candidates:
        infrastructure.extend(
            reason for reason in row.get("reasons", [])
            if reason in quote_vetoes
            or reason == "required market data is missing or insufficient"
        )
    if infrastructure and decision["type"] == "NO_TRADE":
        decision.update(type="INFRASTRUCTURE_BLOCKED", decision_reason="INFRASTRUCTURE_VETO",
                        infrastructure_vetoes=list(dict.fromkeys(infrastructure)))
    else:
        decision["decision_reason"] = "STRATEGY_REJECTION" if decision["type"] == "NO_TRADE" else "STRATEGY_WATCH"


def cycle_diagnostic_lines(result):
    reasoning = result.get("llm_reasoning", {})
    trace = reasoning.get("trace", {})
    rows = result.get("analyzed_candidates", [])
    yield f"LLM REASONING: {reasoning.get('status', 'UNAVAILABLE')}"
    yield f"LLM DURATION: {trace.get('reasoning_duration_seconds', 'UNAVAILABLE')}s"
    yield f"CANDIDATES ANALYZED: {len(rows)}"
    yield f"VALID TECHNICAL SETUPS: {sum(r.get('technical_disposition') in {'QUALIFIED', 'MONITORABLE'} for r in rows)}"
    yield f"TRADE THRESHOLD: {config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD}"
    if rows:
        top = max(rows, key=lambda r: r.get('coordinator_decision', {}).get('combined_score', 0))
        scores = top.get('coordinator_decision', {})
        yield f"TOP CANDIDATE: {top.get('symbol')}"
        metrics = top.get('deterministic_technical_metrics', {})
        yield f"QUOTE FRESHNESS SOURCE: {metrics.get('quote_freshness_source', 'unavailable')}"
        age = metrics.get('quote_age_at_analysis_seconds')
        yield f"QUOTE AGE: {round(age, 3) if isinstance(age, (int, float)) else 'UNAVAILABLE'} seconds"
        yield f"CANDLES: {metrics.get('analysis_candle_count', 0)}"
        yield f"TECHNICAL DATA VALID: {'YES' if metrics.get('technical_data_valid') else 'NO'}"
        validation = metrics.get('technical_validation', {})
        rules = {
            rule.get('rule_name'): rule
            for rule in validation.get('rules', [])
            if isinstance(rule, dict)
        }
        yield "TECHNICAL VALIDATION:"
        for name in ("PRICE_VS_VWAP", "EMA_STRUCTURE", "RSI", "MACD", "RELATIVE_VOLUME", "CANDLE_STRUCTURE", "SPREAD"):
            rule = rules.get(name, {})
            yield f"{name}: {rule.get('status', 'UNAVAILABLE')} ({rule.get('type', 'UNKNOWN')})"
        yield f"TRUE HARD GATES: {validation.get('hard_gates_passed', 0)}/{validation.get('hard_gates_evaluated', 0)} PASS"
        failed = validation.get('true_hard_gate_failures', validation.get('failed_hard_gates', []))
        warnings = validation.get('signal_quality_failures', [])
        yield f"TRUE HARD FAILURES: {', '.join(failed) if failed else 'NONE'}"
        yield f"SIGNAL QUALITY WARNINGS: {', '.join(warnings) if warnings else 'NONE'}"
        yield f"TECHNICAL CONFIDENCE: {validation.get('technical_confidence', validation.get('confidence'))}"
        yield f"TECHNICAL SCORE: {top.get('technical_context', {}).get('technical_score')}"
        yield f"TECHNICAL SETUP: {top.get('technical_disposition', 'UNKNOWN')}"
        yield f"LLM QUALITATIVE SCORE: {scores.get('qualitative_score')}"
        yield f"COMBINED SCORE: {scores.get('combined_score')}"
        vetoes = result['decision'].get('infrastructure_vetoes') or top.get('coordinator_vetoes', [])
        yield f"PRIMARY REJECTION: {vetoes[0] if vetoes else 'BELOW_THRESHOLD' if result['decision']['type'] in {'NO_TRADE', 'WATCH'} else 'NONE'}"
    for row in rows:
        m = row.get('deterministic_technical_metrics', {})
        yield f"CANDIDATE DATA: {row.get('symbol')} quote_age={m.get('quote_age_at_analysis_seconds')}s candles={m.get('analysis_candle_count')}"
