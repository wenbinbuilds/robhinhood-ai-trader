"""Mock-only pipeline regressions; no broker or order calls."""
import subprocess
import json
from pathlib import Path
from datetime import timedelta

import config
import runner
from agent.candidate_analyzer import CandidateData
from agent.technical_agent import TechnicalAgent
from agent.llm_reasoning_bridge import sanitized_diagnostics
from agent.cycle_diagnostics import classify_decision, cycle_diagnostic_lines
from agent.codex_mcp_bridge import CodexMcpBridge
from test_market_cycle import bullish_candidate, NOW
from test_llm_reasoning import bridge, FakeRunner, payload, response


def assessment(age=150, **changes):
    row = {"symbol": "ACME", **bullish_candidate(),
           "quote_as_of": (NOW - timedelta(seconds=age)).isoformat(), **changes}
    return TechnicalAgent().analyze(CandidateData.from_mapping(row), now=NOW)


def test_slow_latency_accepted_without_changing_fast_limits():
    assert config.FAST_QUOTE_MAX_AGE_SECONDS == 5
    assert config.MAX_QUOTE_AGE_SECONDS == 120  # execution gate unchanged
    for age in (150, 241, 300):
        result = assessment(age)
        assert result.context.technical_score > 0
        assert "STALE_QUOTE" not in result.candidate_plan["reasons"]
        assert result.metrics["quote_age_at_analysis_seconds"] == age


def test_slow_stale_future_invalid_reasons_are_distinct():
    assert "QUOTE_STALE" in assessment(301).candidate_plan["reasons"]
    assert "QUOTE_TIMESTAMP_FUTURE" in assessment(-1).candidate_plan["reasons"]
    for value in ("not-a-date", "2026-09-09T15:00:00"):
        result = assessment(quote_as_of=value)
        assert "QUOTE_TIMESTAMP_INVALID" in result.candidate_plan["reasons"]
        assert "QUOTE_TIMESTAMP_FUTURE" not in result.candidate_plan["reasons"]
    missing = assessment(quote_as_of=None)
    assert "QUOTE_TIMESTAMP_MISSING" in missing.candidate_plan["reasons"]


def test_slow_cycle_uses_explicit_retrieval_freshness_when_exchange_time_absent():
    result = assessment(
        quote_as_of=None,
        candidate_quote_retrieved_at=(NOW - timedelta(seconds=1)).isoformat(),
        quote_freshness_source="retrieval_timestamp",
    )
    assert result.context.technical_score > 0
    assert result.metrics["quote_freshness_source"] == "retrieval_timestamp"
    assert result.metrics["quote_age_at_analysis_seconds"] == 1
    assert result.metrics["technical_data_valid"] is True


def test_adequate_and_insufficient_history_still_enforced():
    result = assessment()
    assert result.metrics["analysis_candle_count"] == 6
    assert result.context.technical_score > 0
    short = assessment(candles=bullish_candidate()["candles"][-3:])
    assert "sufficient_5_minute_candles" in short.context.unavailable_fields
    assert short.context.technical_score == 0


def test_fifty_real_five_minute_candles_are_valid_and_score_nonzero():
    seed = bullish_candidate()["candles"]
    candles = [dict(seed[index % len(seed)], begins_at=f"2026-09-09T{10 + index // 12:02d}:{(index % 12) * 5:02d}:00Z") for index in range(50)]
    result = assessment(candles=candles)
    assert result.metrics["analysis_candle_count"] == 50
    assert result.metrics["technical_data_valid"] is True
    assert result.context.technical_score > 0


def test_bad_but_structurally_valid_setup_is_monitorable_without_changing_thresholds():
    bad = assessment(current_price=97, ema9=98, ema20=100, rsi14=28,
                     relative_volume=.4, macd=-.5, macd_signal=.1)
    assert bad.candidate_plan["technical_disposition"] == "MONITORABLE"
    assert bad.metrics["technical_validation"]["true_hard_gate_failures"] == []


def test_normalization_does_not_discard_analysis_bars():
    data = {"market": {"benchmarks": []}, "candidate_data": [
        {"symbol": "ACME", "candles": bullish_candidate()["candles"]}]}
    normalized = CodexMcpBridge._normalize_snapshot(data)
    assert len(normalized["candidate_data"][0]["candles"]) == 6


def test_nonzero_diagnostic_captures_parser_error_without_secrets(tmp_path):
    class Failing(FakeRunner):
        def __call__(self, command, **kwargs):
            if command[1:3] == ["debug", "models"]:
                return super().__call__(command, **kwargs)
            return subprocess.CompletedProcess(command, 2, "", "error: unexpected argument '--search' found\nAuthorization: Bearer secret123\nerror: access_token=hide-this\nUsage: codex exec [OPTIONS]")
    result = bridge(tmp_path, Failing(response())).reason(payload(), expected_symbols=["ACME"], now=NOW)
    diagnostic = result.trace.diagnostics
    assert diagnostic["returncode"] == 2
    assert "unexpected argument" in diagnostic["stderr"]
    assert "hide-this" not in str(diagnostic)
    assert "secret123" not in str(diagnostic)
    assert diagnostic["timeout"] is False


def test_timeout_and_schema_diagnostics(tmp_path):
    timeout = bridge(tmp_path, FakeRunner(response(), timeout=True)).reason(payload(), expected_symbols=["ACME"], now=NOW)
    assert timeout.trace.diagnostics["timeout"] is True
    invalid = bridge(tmp_path, FakeRunner({})).reason(payload(), expected_symbols=["ACME"], now=NOW)
    assert invalid.trace.diagnostics["schema_errors"]
    assert invalid.status == "UNAVAILABLE"


def test_infrastructure_and_strategy_are_not_conflated():
    failed = {"type": "NO_TRADE"}
    classify_decision(failed, [{"coordinator_vetoes": ["LLM_REASONING_UNAVAILABLE"]}], "UNAVAILABLE")
    assert failed["type"] == "INFRASTRUCTURE_BLOCKED"
    assert failed["decision_reason"] == "INFRASTRUCTURE_VETO"
    rejected = {"type": "NO_TRADE"}
    classify_decision(rejected, [{"coordinator_vetoes": ["bearish setup"]}], "AVAILABLE")
    assert rejected["type"] == "NO_TRADE"
    assert rejected["decision_reason"] == "STRATEGY_REJECTION"


def test_terminal_includes_concise_technical_validation_audit():
    technical = assessment()
    row = {
        "symbol": "ACME",
        "technical_disposition": technical.candidate_plan["technical_disposition"],
        "technical_context": technical.context.to_dict(),
        "deterministic_technical_metrics": dict(technical.metrics),
        "coordinator_decision": {"combined_score": 0.5, "qualitative_score": 0.4},
        "coordinator_vetoes": ["technical setup failed deterministic validation"],
    }
    result = {
        "llm_reasoning": {"status": "AVAILABLE", "trace": {"reasoning_duration_seconds": 1}},
        "analyzed_candidates": [row],
        "decision": {"type": "NO_TRADE"},
    }
    output = "\n".join(cycle_diagnostic_lines(result))
    assert "TECHNICAL VALIDATION:" in output
    assert "PRICE_VS_VWAP: PASS (SOFT)" in output
    assert "SPREAD: PASS (HARD)" in output
    assert "TRUE HARD GATES:" in output
    assert "TECHNICAL SETUP: QUALIFIED" in output


def test_diagnostics_tolerate_missing_none_and_partial_nested_objects():
    base = {
        "llm_reasoning": None,
        "decision": {"type": "NO_TRADE"},
    }
    cases = [
        [],
        [{"symbol": "MISSING"}],
        [{"symbol": "NONE", "coordinator_decision": None}],
        [{"symbol": "NO_SCORE", "coordinator_decision": {}}],
        [{
            "symbol": "OPEN",
            "decision": "POSITION_CONTEXT_UPDATED",
            "coordinator_decision": None,
            "deterministic_technical_metrics": None,
            "technical_context": None,
        }],
        [None, "malformed", {"symbol": "PARTIAL"}],
    ]
    for rows in cases:
        output = "\n".join(cycle_diagnostic_lines({**base, "analyzed_candidates": rows}))
        assert "CANDIDATES ANALYZED:" in output


def test_runner_isolates_diagnostic_renderer_failure(monkeypatch, capsys):
    def broken(_result):
        yield "BEFORE FAILURE"
        raise AttributeError("fixture")

    monkeypatch.setattr(runner, "cycle_diagnostic_lines", broken)
    runner.print_cycle_diagnostics({"decision": {"type": "NO_TRADE"}})
    output = capsys.readouterr().out
    assert "BEFORE FAILURE" in output
    assert "DIAGNOSTICS STATUS: DEGRADED (AttributeError; cycle continued)" in output


def test_safe_diagnostics_do_not_persist_echoed_prompts():
    assert sanitized_diagnostics('INPUT_JSON:\n{"private":"anything"}\nerror: failed') == 'error: failed'


def test_reasoning_schema_has_api_required_explicit_scalar_types():
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/llm_market_reasoning.schema.json").read_text())
    def walk(value):
        if isinstance(value, dict):
            if "enum" in value or "const" in value:
                assert "type" in value
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value["properties"]) == set(value["required"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(schema)
