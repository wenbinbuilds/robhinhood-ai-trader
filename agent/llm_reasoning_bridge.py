"""One schema-constrained Codex reasoning invocation per market cycle.

This bridge receives normalized, credential-free evidence. It has no Robinhood
client and explicitly disables the Robinhood MCP server for its subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

import config
from agent.models import (
    LlmCandidateAnalysis,
    LlmReasoningResult,
    ReasoningTrace,
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

FORBIDDEN_PAYLOAD_KEYS = {
    "account_number",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session_cookie",
    "api_key",
    "secret",
    "password",
    "two_factor_code",
}


def sanitized_diagnostics(value: Any) -> str:
    """Allow only diagnostic lines, not echoed prompts, transcripts or secrets."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    lines = []
    for line in str(value or "").splitlines():
        if re.search(r"(?i)token|cookie|authorization|credential|secret|password|api[_ -]?key|oauth|bearer|account[_ -]?number", line):
            continue
        if not re.match(r'(?i)^\s*(error|warning|usage:|tip:|caused by:|invalid|model:|reasoning effort:|"(?:message|type|param|code)"\s*:)', line):
            continue
        line = re.sub(r"/Users/[^/\s]+", "/Users/<redacted>", line)
        line = re.sub(r"[\w.+-]+@[\w.-]+", "<redacted>", line)
        line = re.sub(r"[A-Za-z0-9_\-]{40,}", "<redacted>", line)
        lines.append(line[:600])
    return "\n".join(lines)[:3000]


class LlmReasoningProvider(Protocol):
    def reason(
        self,
        payload: Mapping[str, Any],
        *,
        expected_symbols: Sequence[str],
        now: datetime,
    ) -> LlmReasoningResult: ...


class UnavailableLlmReasoningBridge:
    """Fail-closed default for direct MarketCycle construction."""

    def reason(
        self,
        payload: Mapping[str, Any],
        *,
        expected_symbols: Sequence[str],
        now: datetime,
    ) -> LlmReasoningResult:
        current = _utc(now)
        return _failure_result(
            current,
            len(expected_symbols),
            "LLM_REASONING_NOT_CONFIGURED",
            duration=0.0,
            model_identifier=None,
            status="NOT_CONFIGURED",
        )


class LlmReasoningBridge:
    """Run one non-interactive, read-only Codex analysis for all candidates."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        prompt_path: str | Path = "prompts/llm_market_reasoning_v1.md",
        schema_path: str | Path = "schemas/llm_market_reasoning.schema.json",
        command_runner: CommandRunner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.prompt_path = self._resolve(prompt_path)
        self.schema_path = self._resolve(schema_path)
        self.command_runner = command_runner
        self.which = which
        self.monotonic = monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_dir / path

    def reason(
        self,
        payload: Mapping[str, Any],
        *,
        expected_symbols: Sequence[str],
        now: datetime,
    ) -> LlmReasoningResult:
        analysis_time = _utc(now)
        current = _utc(self.clock())
        started = self.monotonic()
        self._diagnostics = {"returncode": None, "timeout": False, "stderr": "", "stdout": "", "schema_errors": []}
        symbols = tuple(str(item).upper() for item in expected_symbols)
        explicit_model = config.CODEX_REASONING_MODEL
        model_identifier = (
            str(explicit_model).strip()
            if isinstance(explicit_model, str) and explicit_model.strip()
            else None
        )

        try:
            _reject_sensitive_keys(payload)
            serialized = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        except (TypeError, ValueError) as exc:
            return self._failure(
                current, symbols, f"INVALID_REASONING_PAYLOAD:{type(exc).__name__}",
                started, model_identifier,
            )
        if len(serialized.encode("utf-8")) > config.LLM_REASONING_MAX_PAYLOAD_BYTES:
            return self._failure(
                current, symbols, "REASONING_PAYLOAD_TOO_LARGE", started,
                model_identifier,
            )

        codex = self.which("codex")
        if not codex:
            return self._failure(
                current, symbols, "CODEX_NOT_INSTALLED", started, model_identifier
            )
        if model_identifier is not None:
            model_failure = self._validate_model(codex, model_identifier)
            if model_failure is not None:
                return self._failure(
                    current, symbols, model_failure, started, model_identifier
                )

        try:
            prompt = self.prompt_path.read_text(encoding="utf-8")
            schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except (OSError, ValueError, SchemaError) as exc:
            return self._failure(
                current,
                symbols,
                f"REASONING_CONFIGURATION_ERROR:{type(exc).__name__}",
                started,
                model_identifier,
            )

        output_path = self._temporary_output_path()
        command = self.build_exec_command(codex, output_path, model_identifier)
        self._diagnostics["command_structure"] = [
            "<output.json>" if item == str(output_path) else item.replace(str(self.project_dir), "<project>")
            for item in command
        ]
        try:
            try:
                completed = self.command_runner(
                    command,
                    cwd=self.project_dir,
                    input=f"{prompt}\n\nINPUT_JSON:\n{serialized}\n",
                    text=True,
                    capture_output=True,
                    timeout=config.CODEX_LLM_REASONING_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self._diagnostics.update(timeout=True, stderr=sanitized_diagnostics(exc.stderr), stdout=sanitized_diagnostics(exc.stdout))
                return self._failure(
                    current, symbols, "LLM_REASONING_TIMEOUT", started,
                    model_identifier,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return self._failure(
                    current,
                    symbols,
                    f"LLM_REASONING_START_FAILED:{type(exc).__name__}",
                    started,
                    model_identifier,
                )
            self._diagnostics.update(returncode=completed.returncode,
                                     stderr=sanitized_diagnostics(completed.stderr),
                                     stdout=sanitized_diagnostics(completed.stdout))
            observed_model = re.search(r"(?m)^model:\s*([A-Za-z0-9_.-]+)\s*$", self._diagnostics["stderr"])
            if model_identifier is None and observed_model:
                model_identifier = observed_model.group(1)
            if completed.returncode != 0:
                return self._failure(
                    current,
                    symbols,
                    f"LLM_REASONING_NONZERO_EXIT:{completed.returncode}",
                    started,
                    model_identifier,
                )
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._failure(
                    current, symbols, "LLM_REASONING_INVALID_JSON", started,
                    model_identifier,
                )
            if not isinstance(raw, Mapping):
                return self._failure(
                    current, symbols, "LLM_REASONING_INVALID_ROOT", started,
                    model_identifier,
                )
            validation_error = self._validate_response(
                raw, schema, symbols, payload
            )
            self._diagnostics["schema_errors"] = [
                {"path": list(error.absolute_schema_path), "validator": error.validator}
                for error in list(Draft202012Validator(schema).iter_errors(raw))[:10]
            ]
            if validation_error is not None:
                return self._failure(
                    current, symbols, validation_error, started, model_identifier
                )
            self._normalize_event_ages(raw, analysis_time)
            try:
                candidates = tuple(
                    LlmCandidateAnalysis.from_mapping(item)
                    for item in raw["candidates"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                return self._failure(
                    current,
                    symbols,
                    f"LLM_REASONING_TYPED_PARSE_FAILED:{type(exc).__name__}",
                    started,
                    model_identifier,
                )
            duration = max(0.0, self.monotonic() - started)
            trace = ReasoningTrace(
                reasoning_provider="CODEX_CLI",
                model_identifier=model_identifier,
                reasoning_invocation_timestamp=current.isoformat(),
                reasoning_duration_seconds=round(duration, 3),
                candidate_count=len(candidates),
                schema_version=config.LLM_REASONING_SCHEMA_VERSION,
                prompt_version=config.LLM_REASONING_PROMPT_VERSION,
                status="SUCCESS",
                failure_reason=None,
                token_usage=None,
                diagnostics=dict(self._diagnostics),
            )
            return LlmReasoningResult(
                status="AVAILABLE", candidates=candidates, trace=trace
            )
        finally:
            output_path.unlink(missing_ok=True)

    def build_exec_command(
        self,
        codex: str,
        output_path: Path,
        model_identifier: str | None = None,
    ) -> list[str]:
        """Return safe argv; Robinhood tools are unavailable to this subprocess."""

        command = [
            codex,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--config",
            'web_search="live"',
            "--config",
            f'model_reasoning_effort="{config.CODEX_REASONING_EFFORT}"',
            "--sandbox",
            "read-only",
            "--config",
            "mcp_servers.robinhood-trading.enabled=false",
            "-C",
            str(self.project_dir),
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if model_identifier is not None:
            command.extend(["--model", model_identifier])
        command.append("-")
        return command

    def _validate_model(self, codex: str, requested: str) -> str | None:
        try:
            completed = self.command_runner(
                [codex, "debug", "models", "--bundled"],
                cwd=self.project_dir,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "CODEX_MODEL_CATALOG_UNAVAILABLE"
        if completed.returncode != 0:
            return "CODEX_MODEL_CATALOG_UNAVAILABLE"
        try:
            catalog = json.loads(completed.stdout)
            models = catalog["models"]
            supported = {
                str(item["slug"])
                for item in models
                if isinstance(item, Mapping) and item.get("slug")
            }
        except (KeyError, TypeError, json.JSONDecodeError):
            return "CODEX_MODEL_CATALOG_INVALID"
        return None if requested in supported else "UNSUPPORTED_CODEX_REASONING_MODEL"

    def _temporary_output_path(self) -> Path:
        output_dir = self.project_dir / "state"
        output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".llm-reasoning-", suffix=".json", dir=output_dir
        )
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _validate_response(
        raw: Mapping[str, Any],
        schema: Mapping[str, Any],
        expected_symbols: Sequence[str],
        payload: Mapping[str, Any],
    ) -> str | None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(raw),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        if errors:
            return "LLM_REASONING_SCHEMA_VIOLATION"
        if raw.get("schema_version") != config.LLM_REASONING_SCHEMA_VERSION:
            return "LLM_REASONING_SCHEMA_VERSION_MISMATCH"
        if raw.get("prompt_version") != config.LLM_REASONING_PROMPT_VERSION:
            return "LLM_REASONING_PROMPT_VERSION_MISMATCH"
        if raw.get("analysis_timestamp") != payload.get("analysis_timestamp"):
            return "LLM_REASONING_TIMESTAMP_MISMATCH"
        returned = [str(item["symbol"]) for item in raw["candidates"]]
        if len(returned) != len(set(returned)):
            return "LLM_REASONING_DUPLICATE_SYMBOL"
        if set(returned) - set(expected_symbols):
            return "LLM_REASONING_UNAUTHORIZED_SYMBOL"
        if set(expected_symbols) - set(returned):
            return "LLM_REASONING_MISSING_CANDIDATE"
        for item in raw["candidates"]:
            news = item["news_analysis"]
            if news["status"] == "UNAVAILABLE" and (
                news["catalyst_found"]
                or news["catalyst_type"] != "NONE"
                or news["sentiment"] != "UNKNOWN"
                or news["importance"] != 0
                or news["freshness_score"] != 0
                or news["source_quality_score"] != 0
                or news["explains_price_move"] is not None
                or news["supporting_events"]
                or news["conflicting_events"]
            ):
                return "LLM_REASONING_INCONSISTENT_NEWS_UNAVAILABLE"
            events = [*news["supporting_events"], *news["conflicting_events"]]
            event_ids = [str(event["event_id"]) for event in events]
            if len(event_ids) != len(set(event_ids)):
                return "LLM_REASONING_DUPLICATE_NEWS_EVENT"
        return None

    @staticmethod
    def _normalize_event_ages(raw: Mapping[str, Any], now: datetime) -> None:
        """Make event age a Python-derived fact, never an LLM calculation."""

        for candidate in raw["candidates"]:
            news = candidate["news_analysis"]
            for event in [*news["supporting_events"], *news["conflicting_events"]]:
                published = event.get("published_at")
                if not isinstance(published, str) or not published.strip():
                    event["age_minutes"] = None
                    continue
                try:
                    parsed = datetime.fromisoformat(
                        published.replace("Z", "+00:00")
                    )
                except ValueError:
                    event["published_at"] = None
                    event["age_minutes"] = None
                    continue
                if parsed.tzinfo is None:
                    event["published_at"] = None
                    event["age_minutes"] = None
                    continue
                event["age_minutes"] = round(
                    max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 60),
                    1,
                )

    def _failure(
        self,
        now: datetime,
        symbols: Sequence[str],
        reason: str,
        started: float,
        model_identifier: str | None,
    ) -> LlmReasoningResult:
        result = _failure_result(
            now,
            len(symbols),
            reason,
            duration=max(0.0, self.monotonic() - started),
            model_identifier=model_identifier,
        )
        return replace(result, trace=replace(result.trace, diagnostics=dict(self._diagnostics)))


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _reject_sensitive_keys(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(f"sensitive key {path}.{key} is forbidden")
            _reject_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")


def _failure_result(
    now: datetime,
    candidate_count: int,
    reason: str,
    *,
    duration: float,
    model_identifier: str | None,
    status: str = "FAILED",
) -> LlmReasoningResult:
    trace = ReasoningTrace(
        reasoning_provider="CODEX_CLI",
        model_identifier=model_identifier,
        reasoning_invocation_timestamp=_utc(now).isoformat(),
        reasoning_duration_seconds=round(max(0.0, duration), 3),
        candidate_count=candidate_count,
        schema_version=config.LLM_REASONING_SCHEMA_VERSION,
        prompt_version=config.LLM_REASONING_PROMPT_VERSION,
        status=status,
        failure_reason=reason,
        token_usage=None,
    )
    return LlmReasoningResult(
        status="UNAVAILABLE", candidates=(), trace=trace,
        failure_reason=reason,
    )
