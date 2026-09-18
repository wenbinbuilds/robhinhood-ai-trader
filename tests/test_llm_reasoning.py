import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import config
from agent.dashboard_projection import reasoning_dashboard_projection
from agent.llm_reasoning_bridge import LlmReasoningBridge

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def candidate(symbol: str, *, news_available: bool = True) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "news_analysis": {
            "status": "AVAILABLE" if news_available else "UNAVAILABLE",
            "catalyst_found": news_available,
            "catalyst_type": "EARNINGS" if news_available else "NONE",
            "sentiment": "POSITIVE" if news_available else "UNKNOWN",
            "importance": 0.8 if news_available else 0.0,
            "freshness_score": 0.9 if news_available else 0.0,
            "source_quality_score": 0.9 if news_available else 0.0,
            "explains_price_move": True if news_available else None,
            "event_summary": "earnings beat" if news_available else "NEWS_UNAVAILABLE",
            "supporting_events": (
                [
                    {
                        "event_id": f"{symbol}-earnings",
                        "summary": "earnings beat",
                        "published_at": NOW.isoformat(),
                        "age_minutes": 0,
                        "source_quality": "PRIMARY",
                        "sources": [
                            {"name": "Issuer", "url": "https://example.com/release"}
                        ],
                    }
                ]
                if news_available else []
            ),
            "conflicting_events": [],
        },
        "sector_analysis": {
            "sector": "TECHNOLOGY_SEMICONDUCTORS",
            "sector_bias": "SUPPORTS",
            "reasoning": "sector evidence supports the setup",
            "important_sector_drivers": ["semiconductor demand"],
        },
        "macro_analysis": {
            "market_bias": "NEUTRAL",
            "reasoning": "broad market evidence is mixed",
        },
        "qualitative_analysis": {
            "proposed_direction": "LONG",
            "setup_quality": 0.8,
            "catalyst_quality": 0.8 if news_available else 0.0,
            "continuation_probability_score": 0.7,
            "conflicting_evidence_severity": 0.2,
            "reasons_for": ["price holds above VWAP"],
            "reasons_against": ["broad market is mixed"],
            "key_uncertainties": [],
            "summary": "constructive but not certain",
        },
    }


def response(symbols=("ACME",), *, news_available: bool = True) -> dict[str, Any]:
    return {
        "schema_version": config.LLM_REASONING_SCHEMA_VERSION,
        "prompt_version": config.LLM_REASONING_PROMPT_VERSION,
        "analysis_timestamp": NOW.isoformat(),
        "candidates": [
            candidate(symbol, news_available=news_available) for symbol in symbols
        ],
    }


def payload(symbols=("ACME",)) -> dict[str, Any]:
    return {
        "schema_version": config.LLM_REASONING_SCHEMA_VERSION,
        "prompt_version": config.LLM_REASONING_PROMPT_VERSION,
        "analysis_timestamp": NOW.isoformat(),
        "broad_market_context": {},
        "current_real_positions": [],
        "current_shadow_positions": [],
        "candidates": [{"symbol": symbol} for symbol in symbols],
    }


class FakeRunner:
    def __init__(
        self,
        value: Any,
        *,
        returncode: int = 0,
        timeout: bool = False,
        catalog=None,
    ) -> None:
        self.value = value
        self.returncode = returncode
        self.timeout = timeout
        self.catalog = catalog or (config.CODEX_REASONING_MODEL or "model-a",)
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        if command[1:3] == ["debug", "models"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"models": [{"slug": item} for item in self.catalog]}
                ),
                stderr="",
            )
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        output = Path(command[command.index("--output-last-message") + 1])
        if isinstance(self.value, str):
            output.write_text(self.value, encoding="utf-8")
        else:
            output.write_text(json.dumps(self.value), encoding="utf-8")
        return subprocess.CompletedProcess(
            command, self.returncode, stdout="", stderr=""
        )


def bridge(tmp_path: Path, runner: FakeRunner) -> LlmReasoningBridge:
    project = Path(__file__).resolve().parents[1]
    return LlmReasoningBridge(
        project_dir=project,
        command_runner=runner,
        which=lambda _: "/usr/local/bin/codex",
    )


def test_valid_structured_response_and_one_call_for_multiple_candidates(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(response(("ACME", "BETA")))
    result = bridge(tmp_path, runner).reason(
        payload(("ACME", "BETA")), expected_symbols=("ACME", "BETA"), now=NOW
    )

    assert result.status == "AVAILABLE"
    assert set(result.by_symbol()) == {"ACME", "BETA"}
    assert len([call for call in runner.calls if call[1] == "exec"]) == 1
    command = next(call for call in runner.calls if call[1] == "exec")
    assert "--output-schema" in command
    assert "--search" not in command
    assert f'web_search="{config.CODEX_REASONING_WEB_SEARCH}"' in command
    assert f'model_reasoning_effort="{config.CODEX_REASONING_EFFORT}"' in command
    assert "read-only" in command
    assert "mcp_servers.robinhood-trading.enabled=false" in command
    if config.CODEX_REASONING_MODEL:
        assert command[command.index("--model") + 1] == config.CODEX_REASONING_MODEL
    else:
        assert "--model" not in command
    assert result.trace.prompt_version == "v1"
    assert result.trace.token_usage is None
    profile = result.trace.diagnostics["profile"]
    assert profile["model"] == config.CODEX_REASONING_MODEL
    assert profile["reasoning_effort"] == "low"
    assert profile["input_chars"] > 0
    assert profile["input_tokens_estimate"] > 0
    assert profile["startup_ms"] >= 0
    assert profile["inference_ms"] >= 0
    assert profile["parse_ms"] >= 0
    assert profile["total_ms"] >= 0
    assert profile["output_tokens_estimate"] > 0


def test_model_catalog_preflight_is_cached_per_bridge(tmp_path: Path) -> None:
    runner = FakeRunner(response())
    value = bridge(tmp_path, runner)
    value.reason(payload(), expected_symbols=("ACME",), now=NOW)
    value.reason(payload(), expected_symbols=("ACME",), now=NOW)
    assert len([call for call in runner.calls if call[1:3] == ["debug", "models"]]) == 1
    assert len([call for call in runner.calls if call[1] == "exec"]) == 2


@pytest.mark.parametrize(
    ("value", "failure"),
    [
        ("not json", "LLM_REASONING_INVALID_JSON"),
        ({"schema_version": "1.0"}, "LLM_REASONING_SCHEMA_VIOLATION"),
    ],
)
def test_invalid_output_fails_closed(
    tmp_path: Path, value: Any, failure: str
) -> None:
    result = bridge(tmp_path, FakeRunner(value)).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.status == "UNAVAILABLE"
    assert result.failure_reason == failure
    assert result.candidates == ()


def test_timeout_fails_closed(tmp_path: Path) -> None:
    result = bridge(tmp_path, FakeRunner({}, timeout=True)).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_TIMEOUT"
    assert result.trace.status == "TIMEOUT"


def test_missing_candidate_is_rejected(tmp_path: Path) -> None:
    result = bridge(tmp_path, FakeRunner(response(()))).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_MISSING_CANDIDATE"


def test_extra_unauthorized_ticker_is_rejected(tmp_path: Path) -> None:
    result = bridge(tmp_path, FakeRunner(response(("ACME", "MEME")))).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_UNAUTHORIZED_SYMBOL"


def test_missing_news_is_explicit_and_valid(tmp_path: Path) -> None:
    result = bridge(tmp_path, FakeRunner(response(news_available=False))).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    news = result.candidates[0].news_analysis
    assert result.status == "AVAILABLE"
    assert news.status == "UNAVAILABLE"
    assert news.catalyst_found is False


def test_inconsistent_unavailable_news_is_rejected(tmp_path: Path) -> None:
    value = response(news_available=False)
    value["candidates"][0]["news_analysis"]["catalyst_found"] = True
    result = bridge(tmp_path, FakeRunner(value)).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_INCONSISTENT_NEWS_UNAVAILABLE"


def test_duplicate_semantic_news_event_is_rejected(tmp_path: Path) -> None:
    value = response()
    event = value["candidates"][0]["news_analysis"]["supporting_events"][0]
    value["candidates"][0]["news_analysis"]["conflicting_events"] = [event]
    result = bridge(tmp_path, FakeRunner(value)).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_DUPLICATE_NEWS_EVENT"


def test_explicit_supported_model_is_validated_and_used(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "CODEX_REASONING_MODEL", "model-a")
    runner = FakeRunner(response())
    result = bridge(tmp_path, runner).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.status == "AVAILABLE"
    assert runner.calls[0][1:4] == ["debug", "models", "--bundled"]
    assert runner.calls[1][runner.calls[1].index("--model") + 1] == "model-a"
    assert result.trace.model_identifier == "model-a"


def test_explicit_unsupported_model_does_not_silently_fallback(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "CODEX_REASONING_MODEL", "not-supported")
    runner = FakeRunner(response(), catalog=("model-a",))
    result = bridge(tmp_path, runner).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "UNSUPPORTED_CODEX_REASONING_MODEL"
    assert not any(call[1] == "exec" for call in runner.calls)


def test_nonzero_exit_fails_closed(tmp_path: Path) -> None:
    result = bridge(tmp_path, FakeRunner(response(), returncode=9)).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "LLM_REASONING_NONZERO_EXIT:9"


def test_oversized_payload_is_rejected_without_model_call(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "LLM_REASONING_MAX_PAYLOAD_BYTES", 10)
    runner = FakeRunner(response())
    result = bridge(tmp_path, runner).reason(
        payload(), expected_symbols=("ACME",), now=NOW
    )
    assert result.failure_reason == "REASONING_PAYLOAD_TOO_LARGE"
    assert runner.calls == []


def test_dashboard_projection_is_read_only_and_separates_evidence() -> None:
    cycle = {
        "mode": "SHADOW_TRADING",
        "llm_reasoning": {"status": "AVAILABLE"},
        "analyzed_candidates": [
            {
                "symbol": "ACME",
                "preliminary_scanner_rank": 1,
                "deterministic_technical_metrics": {"rsi14": 61},
                "llm_news_analysis": {"catalyst_type": "EARNINGS"},
                "llm_qualitative_analysis": {"reasons_for": ["beat"]},
                "coordinator_decision": {"combined_score": 0.8},
                "decision": "WATCH",
            }
        ],
    }
    projection = reasoning_dashboard_projection(cycle)
    row = projection["candidates"][0]
    assert row["factual_deterministic_data"]["technical_metrics"]["rsi14"] == 61
    assert row["llm_interpretation"]["news"]["catalyst_type"] == "EARNINGS"
    assert projection["controls"] == []
