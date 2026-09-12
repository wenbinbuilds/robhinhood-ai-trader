"""Orchestration for one non-trading market evaluation cycle.

The module intentionally has no Robinhood SDK or order methods. A configured
collector provides a normalized JSON snapshot through the ``MarketDataProvider``
boundary; the production factual collector is direct MCP and model-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar
from zoneinfo import ZoneInfo

import config
from agent.candidate_analyzer import CandidateAnalyzer, CandidateData
from agent.coordinator import CoordinatorAgent
from agent.llm_reasoning_bridge import (
    LlmReasoningProvider,
    UnavailableLlmReasoningBridge,
)
from agent.macro_agent import MacroAgent
from agent.models import LlmReasoningResult, ReasoningTrace
from agent.news_agent import NewsAgent
from agent.sector_agent import SectorAgent
from agent.technical_agent import TechnicalAgent
from execution.base import ExecutionRouter
from execution.models import TradePlanError, build_trade_plan
from execution.order_state import ExecutionAuditLog
from execution.shadow_executor import ShadowExecutor
from risk.risk_manager import RiskManager, RiskRequest
from shadow.execution import ShadowExecutionEngine
from shadow.performance import ShadowPerformance
from shadow.portfolio import ShadowPortfolio
from agent.cycle_diagnostics import classify_decision

T = TypeVar("T")

SENSITIVE_KEYS = {
    "account_number",
    "rhs_account_number",
    "rhc_account_number",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session_cookie",
    "api_key",
    "secret",
    "two_factor_code",
    "email",
    "phone",
    "phone_number",
    "address",
    "legal_name",
    "first_name",
    "last_name",
    "ssn",
    "tax_id",
    "user_id",
    "person_id",
    "order_id",
}


class MarketDataProvider(Protocol):
    """Read-only normalized market-data interface.

    A future integration can implement this protocol using read-only MCP calls,
    but the local analysis process must never gain order methods.
    """

    def get_account(self) -> Mapping[str, Any]: ...

    def get_portfolio(self) -> Mapping[str, Any]: ...

    def get_positions(self) -> Sequence[Mapping[str, Any]]: ...

    def get_open_orders(self) -> Sequence[Mapping[str, Any]]: ...

    def get_daily_realized_pnl(self) -> float | None: ...

    def get_market_context(self) -> Mapping[str, Any]: ...

    def run_equity_scanner(self) -> Mapping[str, Any]: ...

    def get_candidate_data(self, symbol: str) -> Mapping[str, Any]: ...

    def get_shadow_position_data(self, symbol: str) -> Mapping[str, Any]: ...

    def get_warnings(self) -> Sequence[str]: ...

    def get_errors(self) -> Sequence[str]: ...


class SnapshotValidationError(ValueError):
    """Raised when a normalized snapshot violates its safety contract."""

    status = "INVALID_SNAPSHOT"


class MissingSnapshotError(SnapshotValidationError):
    status = "MISSING_SNAPSHOT"


class StaleSnapshotError(SnapshotValidationError):
    status = "STALE_SNAPSHOT"


class McpUnavailableError(SnapshotValidationError):
    status = "MCP_UNAVAILABLE"


class McpNotAuthenticatedError(SnapshotValidationError):
    status = "MCP_NOT_AUTHENTICATED"


class RobinhoodDataError(SnapshotValidationError):
    status = "ROBINHOOD_ERROR"


class AccountNotIdentifiedError(SnapshotValidationError):
    status = "ACCOUNT_NOT_IDENTIFIED"


@dataclass(frozen=True)
class JsonSnapshotProvider:
    """Read a normalized, credential-free snapshot created from read-only MCP data."""

    data: Mapping[str, Any]
    source: str = "in-memory"

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        now: datetime | None = None,
        max_age_seconds: int = config.SNAPSHOT_MAX_AGE_SECONDS,
    ) -> JsonSnapshotProvider:
        source_path = Path(path)
        try:
            with source_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise MissingSnapshotError(f"snapshot not found at {source_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError(
                f"snapshot could not be read: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, Mapping):
            raise SnapshotValidationError("snapshot root must be a JSON object")
        cls._reject_sensitive_keys(data)
        cls._validate_for_analysis(data, now=now, max_age_seconds=max_age_seconds)
        return cls(data=data, source=str(source_path))

    @staticmethod
    def _reject_sensitive_keys(value: Any, path: str = "snapshot") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower()
                if normalized in SENSITIVE_KEYS:
                    raise SnapshotValidationError(
                        f"sensitive field {path}.{key} is forbidden"
                    )
                JsonSnapshotProvider._reject_sensitive_keys(child, f"{path}.{key}")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, child in enumerate(value):
                JsonSnapshotProvider._reject_sensitive_keys(child, f"{path}[{index}]")

    @staticmethod
    def _parse_timestamp(value: Any, field_name: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise SnapshotValidationError(f"{field_name} is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotValidationError(f"{field_name} is invalid") from exc
        if parsed.tzinfo is None:
            raise SnapshotValidationError(f"{field_name} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _validate_for_analysis(
        cls,
        data: Mapping[str, Any],
        *,
        now: datetime | None,
        max_age_seconds: int,
    ) -> None:
        if data.get("data_source") != "ROBINHOOD_MCP":
            raise SnapshotValidationError("data_source must be ROBINHOOD_MCP")

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        generated_at = cls._parse_timestamp(data.get("generated_at"), "generated_at")
        age = (current - generated_at).total_seconds()
        if age < -30 or age > max_age_seconds:
            raise StaleSnapshotError(
                f"snapshot age {age:.0f}s exceeds the {max_age_seconds}s freshness limit"
            )

        mcp_status = data.get("mcp_status")
        if mcp_status == "MCP_UNAVAILABLE":
            raise McpUnavailableError("Robinhood MCP is unavailable to Codex CLI")
        if mcp_status == "MCP_NOT_AUTHENTICATED":
            raise McpNotAuthenticatedError("Robinhood MCP is not authenticated")
        if mcp_status == "ROBINHOOD_ERROR":
            raise RobinhoodDataError("Robinhood returned an error during snapshot refresh")
        if mcp_status != "CONNECTED":
            raise SnapshotValidationError(f"unsupported mcp_status: {mcp_status!r}")

        account = data.get("account")
        if not isinstance(account, Mapping) or account.get("is_agentic_account") is not True:
            raise AccountNotIdentifiedError(
                "the Robinhood Agentic account was not positively identified"
            )

        portfolio = data.get("portfolio")
        if not isinstance(portfolio, Mapping):
            raise RobinhoodDataError("portfolio data is missing")
        if portfolio.get("portfolio_value", portfolio.get("total_value")) is None:
            raise RobinhoodDataError("portfolio value is missing")
        if portfolio.get("buying_power") is None:
            raise RobinhoodDataError("buying power is missing")
        if portfolio.get("unleveraged_buying_power") is None:
            raise RobinhoodDataError("unleveraged buying power is missing")

        for key in ("positions", "open_orders"):
            value = data.get(key)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise RobinhoodDataError(f"{key} data is missing or malformed")

        market = data.get("market")
        if not isinstance(market, Mapping) or not isinstance(
            market.get("is_regular_session"), bool
        ):
            raise RobinhoodDataError("market-session status is missing")

        scanner = data.get("scanner")
        if not isinstance(scanner, Mapping):
            raise RobinhoodDataError("scanner status is missing")
        scanner_status = scanner.get("status")
        if market["is_regular_session"] is False:
            if scanner_status not in {"OK", "SKIPPED_MARKET_CLOSED"}:
                raise RobinhoodDataError(
                    f"closed-market scanner retrieval failed: {scanner_status}"
                )
        elif scanner_status != "OK":
            raise RobinhoodDataError(
                f"open-market scanner retrieval failed: {scanner_status}"
            )
        if scanner_status == "OK":
            if scanner.get("scan_name") != config.PROJECT_SCANNER_NAME:
                raise RobinhoodDataError("project scanner identity is missing")
            candidates = scanner.get("candidates")
            if not isinstance(candidates, Sequence) or isinstance(
                candidates, (str, bytes)
            ):
                raise RobinhoodDataError("scanner candidates are malformed")
            if len(candidates) > config.MAX_CANDIDATES_TO_ANALYZE:
                raise RobinhoodDataError("scanner candidate limit was exceeded")

        checks = data.get("connectivity_checks")
        if not isinstance(checks, Mapping):
            raise RobinhoodDataError("connectivity checks are missing")
        candidate_data = data.get("candidate_data")
        if not isinstance(candidate_data, Sequence) or isinstance(
            candidate_data, (str, bytes)
        ):
            raise RobinhoodDataError("candidate data is missing or malformed")
        scanner_candidates = scanner.get("candidates", [])
        scanner_symbols = {
            str(item.get("symbol", "")).upper()
            for item in scanner_candidates
            if isinstance(item, Mapping)
        }
        candidate_symbols = {
            str(item.get("symbol", "")).upper()
            for item in candidate_data
            if isinstance(item, Mapping)
        }
        if not candidate_symbols.issubset(scanner_symbols):
            raise RobinhoodDataError(
                "candidate data contains a symbol not returned by the scanner"
            )
        if market["is_regular_session"] and candidate_symbols != scanner_symbols:
            raise RobinhoodDataError(
                "open-market scanner candidates are missing deeper analysis data"
            )
        shadow_data = data.get("shadow_position_data")
        if not isinstance(shadow_data, Sequence) or isinstance(shadow_data, (str, bytes)):
            raise RobinhoodDataError("shadow-position data is missing or malformed")

    def get_snapshot_metadata(self) -> Mapping[str, Any]:
        return {
            "data_source": self.data.get("data_source"),
            "generated_at": self.data.get("generated_at"),
            "mcp_status": self.data.get("mcp_status"),
        }

    def _mapping(self, key: str) -> Mapping[str, Any]:
        value = self.data.get(key, {})
        if not isinstance(value, Mapping):
            raise SnapshotValidationError(f"snapshot.{key} must be an object")
        return value

    def _sequence(self, key: str) -> Sequence[Mapping[str, Any]]:
        value = self.data.get(key, [])
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise SnapshotValidationError(f"snapshot.{key} must be an array")
        if any(not isinstance(item, Mapping) for item in value):
            raise SnapshotValidationError(f"snapshot.{key} entries must be objects")
        return value  # type: ignore[return-value]

    def get_account(self) -> Mapping[str, Any]:
        return self._mapping("account")

    def get_portfolio(self) -> Mapping[str, Any]:
        return self._mapping("portfolio")

    def get_positions(self) -> Sequence[Mapping[str, Any]]:
        return self._sequence("positions")

    def get_open_orders(self) -> Sequence[Mapping[str, Any]]:
        return self._sequence("open_orders")

    def get_daily_realized_pnl(self) -> float | None:
        value = self.data.get("daily_realized_pnl")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                "snapshot.daily_realized_pnl must be numeric or null"
            ) from exc

    def get_market_context(self) -> Mapping[str, Any]:
        return self._mapping("market")

    def run_equity_scanner(self) -> Mapping[str, Any]:
        return self._mapping("scanner")

    def get_candidate_data(self, symbol: str) -> Mapping[str, Any]:
        all_data = self.data.get("candidate_data", {})
        if isinstance(all_data, Mapping):
            value = all_data.get(symbol.upper(), {})
        elif isinstance(all_data, Sequence) and not isinstance(all_data, (str, bytes)):
            value = next(
                (
                    item
                    for item in all_data
                    if isinstance(item, Mapping)
                    and str(item.get("symbol", "")).upper() == symbol.upper()
                ),
                {},
            )
        else:
            raise SnapshotValidationError("snapshot.candidate_data must be an array")
        if not isinstance(value, Mapping):
            raise SnapshotValidationError(
                f"snapshot.candidate_data.{symbol.upper()} must be an object"
            )
        return value

    def get_shadow_position_data(self, symbol: str) -> Mapping[str, Any]:
        value = self.data.get("shadow_position_data", [])
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise SnapshotValidationError("snapshot.shadow_position_data must be an array")
        result = next(
            (
                item for item in value
                if isinstance(item, Mapping)
                and str(item.get("symbol", "")).upper() == symbol.upper()
            ),
            {},
        )
        return result if isinstance(result, Mapping) else {}

    def get_warnings(self) -> Sequence[str]:
        return self._messages("warnings")

    def get_errors(self) -> Sequence[str]:
        return self._messages("errors")

    def _messages(self, key: str) -> Sequence[str]:
        value = self.data.get(key, [])
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise SnapshotValidationError(f"snapshot.{key} must be an array")
        return [str(item) for item in value]


def is_regular_market_hours(now: datetime | None = None) -> bool:
    """Return whether local wall time is within a normal weekday NYSE session.

    This intentionally does not guess exchange holidays. A read-only snapshot
    should provide ``market.is_regular_session`` when holiday-aware status is
    available; otherwise the agent remains conservative elsewhere when data is
    missing or stale.
    """

    current = now or datetime.now(timezone.utc)
    eastern = current.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
    if eastern.weekday() >= 5:
        return False
    opened = time(*config.REGULAR_MARKET_OPEN)
    closed = time(*config.REGULAR_MARKET_CLOSE)
    return opened <= eastern.time().replace(tzinfo=None) < closed


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_redact(item) for item in value]
    return value


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


class MarketCycle:
    """Perform exactly one logged analysis or local-shadow cycle."""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        analyzer: CandidateAnalyzer | None = None,
        risk_manager: RiskManager | None = None,
        macro_agent: MacroAgent | None = None,
        news_agent: NewsAgent | None = None,
        technical_agent: TechnicalAgent | None = None,
        sector_agent: SectorAgent | None = None,
        coordinator: CoordinatorAgent | None = None,
        reasoning_bridge: LlmReasoningProvider | None = None,
        shadow_portfolio: ShadowPortfolio | None = None,
        state_path: str | Path = "state/session.json",
        logs_dir: str | Path = "logs",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.analyzer = analyzer or CandidateAnalyzer()
        self.risk_manager = risk_manager or RiskManager()
        self.macro_agent = macro_agent or MacroAgent()
        self.news_agent = news_agent or NewsAgent()
        self.technical_agent = technical_agent or TechnicalAgent(self.analyzer)
        self.sector_agent = sector_agent or SectorAgent()
        self.coordinator = coordinator or CoordinatorAgent()
        self.reasoning_bridge = reasoning_bridge or UnavailableLlmReasoningBridge()
        self.state_path = Path(state_path)
        self.logs_dir = Path(logs_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.shadow_portfolio = shadow_portfolio
        if config.MODE == "SHADOW_TRADING" and self.shadow_portfolio is None:
            self.shadow_portfolio = ShadowPortfolio(
                self.state_path.parent / "shadow_portfolio.json",
                self.state_path.parent / "shadow_trades.jsonl",
            )
        self.shadow_engine = (
            ShadowExecutionEngine(self.shadow_portfolio, self.risk_manager, self.news_agent)
            if self.shadow_portfolio is not None
            else None
        )
        self.execution_router = ExecutionRouter(
            shadow_executor=(
                ShadowExecutor(
                    self.shadow_engine,
                    ExecutionAuditLog(self.logs_dir / "execution_audit.jsonl"),
                )
                if self.shadow_engine is not None else None
            ),
            # A RobinhoodExecutor is intentionally never constructed here.
            robinhood_executor=None,
        )

    def run(self) -> dict[str, Any]:
        if config.MODE not in {"ANALYSIS_ONLY", "SHADOW_TRADING"}:
            raise RuntimeError(
                "Only ANALYSIS_ONLY and SHADOW_TRADING are implemented"
            )

        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        session = self._load_session(now)
        errors: list[str] = []
        warnings: list[str] = []

        metadata_getter = getattr(self.provider, "get_snapshot_metadata", None)
        snapshot_metadata: Mapping[str, Any] = (
            metadata_getter() if callable(metadata_getter) else {}
        )

        warnings.extend(self._safe_call(self.provider.get_warnings, [], errors, "warnings"))
        errors.extend(self._safe_call(self.provider.get_errors, [], errors, "errors"))

        account = self._safe_call(self.provider.get_account, {}, errors, "account")
        portfolio = self._safe_call(self.provider.get_portfolio, {}, errors, "portfolio")
        positions = list(
            self._safe_call(self.provider.get_positions, [], errors, "positions")
        )
        open_orders = list(
            self._safe_call(self.provider.get_open_orders, [], errors, "open orders")
        )
        daily_pnl = self._safe_call(
            self.provider.get_daily_realized_pnl,
            None,
            errors,
            "today's realized P&L",
        )

        is_agentic = account.get("is_agentic_account") is True
        if not is_agentic:
            errors.append("Agentic account was not positively identified")

        position_reviews = self._analyze_positions_first(positions)

        market = self._safe_call(
            self.provider.get_market_context, {}, errors, "market context"
        )
        macro_context = self.macro_agent.analyze(market)
        regular_session_value = market.get("is_regular_session")
        wall_clock_open = is_regular_market_hours(now)
        if isinstance(regular_session_value, bool):
            market_open = regular_session_value and wall_clock_open
            if regular_session_value and not wall_clock_open:
                warnings.append(
                    "snapshot market status conflicts with local regular-session hours"
                )
        else:
            market_open = wall_clock_open
            warnings.append(
                "holiday-aware market status unavailable; used weekday/session-time check"
            )

        shadow_positions_evaluated: list[dict[str, Any]] = []
        shadow_positions_exited: list[dict[str, Any]] = []
        new_shadow_positions: list[dict[str, Any]] = []
        rejected_shadow_candidates: list[dict[str, Any]] = []
        if self.shadow_portfolio is not None and self.shadow_engine is not None:
            raw_benchmarks = market.get("benchmarks", [])
            benchmarks = [
                item for item in raw_benchmarks if isinstance(item, Mapping)
            ] if isinstance(raw_benchmarks, Sequence) and not isinstance(raw_benchmarks, (str, bytes)) else []
            self.shadow_portfolio.begin_cycle(
                now, benchmarks, is_regular_session=market_open
            )
            shadow_getter = getattr(self.provider, "get_shadow_position_data", None)
            lookup = (
                shadow_getter
                if callable(shadow_getter)
                else self.provider.get_candidate_data
            )
            (
                shadow_positions_evaluated,
                shadow_positions_exited,
                shadow_warnings,
            ) = self.shadow_engine.monitor_positions(
                lookup, now=now, market_context=macro_context.to_dict()
            )
            warnings.extend(shadow_warnings)

        scanner: Mapping[str, Any] = {"criteria": [], "candidates": []}
        if config.REGULAR_MARKET_ONLY and not market_open:
            warnings.append(
                "regular U.S. market session is closed; scanner results were "
                "excluded from strategy analysis"
            )
        elif not is_agentic:
            warnings.append("scanner was not run because the Agentic account is unverified")
        else:
            scanner = self._safe_call(
                self.provider.run_equity_scanner, scanner, errors, "equity scanner"
            )

        criteria = scanner.get("criteria", [])
        if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)):
            errors.append("scanner criteria are malformed")
            criteria = []
        raw_candidates = scanner.get("candidates", [])
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, (str, bytes)
        ):
            errors.append("scanner candidates are malformed")
            raw_candidates = []

        scanner_is_usable = self._is_supported_momentum_scan(criteria)
        if market_open and is_agentic and not scanner_is_usable:
            warnings.append(
                "no suitable read-only saved momentum scan result is available"
            )
            raw_candidates = []

        scanner_candidates: list[Mapping[str, Any]] = []
        symbols: list[str] = []
        for item in raw_candidates:
            if not isinstance(item, Mapping):
                warnings.append("ignored malformed scanner candidate")
                continue
            symbol = str(item.get("symbol", item.get("ticker", ""))).upper().strip()
            instrument_type = str(item.get("instrument_type", "EQUITY")).upper()
            if not symbol or instrument_type != "EQUITY":
                warnings.append("ignored scanner row that was not a named equity")
                continue
            if symbol not in symbols:
                symbols.append(symbol)
                scanner_candidates.append(item)
        if len(symbols) > config.MAX_CANDIDATES_TO_ANALYZE:
            warnings.append(
                f"scanner returned more than {config.MAX_CANDIDATES_TO_ANALYZE} "
                "candidates; excess rows were not analyzed"
            )
        ranked_rows = sorted(
            enumerate(scanner_candidates),
            key=lambda pair: (
                -self._preliminary_scanner_score(pair[1], pair[0]),
                pair[0],
            ),
        )
        scanner_candidates = [
            row for _, row in ranked_rows[: config.MAX_CANDIDATES_TO_ANALYZE]
        ]
        symbols = [
            str(row.get("symbol", row.get("ticker", ""))).upper()
            for row in scanner_candidates
        ]

        analyzed: list[dict[str, Any]] = []
        risk_results: list[dict[str, Any]] = []
        approved_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        watch_candidates: list[dict[str, Any]] = []
        market_direction = macro_context.regime
        scanner_rows = {
            str(item.get("symbol", item.get("ticker", ""))).upper(): item
            for item in scanner_candidates
        }
        held_symbols = {
            str(position.get("symbol", "")).upper()
            for position in positions
            if (_as_float(position.get("quantity")) or 0) != 0
        }
        if self.shadow_portfolio is not None:
            held_symbols.update(
                position.symbol for position in self.shadow_portfolio.snapshot().open_positions
            )
        candidate_payloads: dict[str, Mapping[str, Any]] = {}
        candidate_records: list[dict[str, Any]] = []

        for scanner_index, symbol in enumerate(symbols):
            if symbol in held_symbols:
                if (
                    self.shadow_portfolio is not None
                    and self.shadow_portfolio.has_symbol(symbol)
                ):
                    rejected_shadow_candidates.append(
                        {"symbol": symbol, "reason": "DUPLICATE_POSITION"}
                    )
                analyzed.append(
                    {
                        "decision": "NO_TRADE",
                        "symbol": symbol,
                        "direction": None,
                        "entry": None,
                        "stop": None,
                        "target": None,
                        "risk_per_share": None,
                        "risk_reward_ratio": None,
                        "confidence": 0.0,
                        "setup_name": config.STRATEGY_NAME,
                        "thesis": None,
                        "invalidation_condition": None,
                        "supporting_indicators": {},
                        "reasons": [
                            "symbol already has an open position; no additional entry "
                            "is considered"
                        ],
                        "unavailable_values": [],
                        "preliminary_scanner_score": self._preliminary_scanner_score(
                            scanner_rows.get(symbol, {}), scanner_index
                        ),
                        "preliminary_scanner_rank": scanner_index + 1,
                        "scanner_evidence": dict(scanner_rows.get(symbol, {})),
                        "market_context": macro_context.to_dict(),
                        "news_context": None,
                        "technical_context": None,
                        "sector_context": None,
                        "llm_analysis": None,
                        "coordinator_decision": None,
                        "risk_manager_result": None,
                        "warnings": [],
                        "errors": [],
                    }
                )
                continue
            payload = self._safe_call(
                lambda symbol=symbol: self.provider.get_candidate_data(symbol),
                {},
                errors,
                f"candidate data for {symbol}",
            )
            candidate_payloads[symbol] = payload
            normalized = dict(payload)
            normalized["symbol"] = symbol
            if normalized.get("market_direction") is None and market_direction is not None:
                normalized["market_direction"] = market_direction
            candidate = CandidateData.from_mapping(normalized)
            technical = self.technical_agent.analyze(candidate, now=now)
            if payload.get("collection_status") == "DATA_UNAVAILABLE":
                plan = dict(technical.candidate_plan)
                plan.update(
                    decision="NO_TRADE",
                    preliminary_scanner_score=self._preliminary_scanner_score(
                        scanner_rows.get(symbol, {}), scanner_index
                    ),
                    preliminary_scanner_rank=scanner_index + 1,
                    scanner_evidence=dict(scanner_rows.get(symbol, {})),
                    market_context=macro_context.to_dict(),
                    news_context=None,
                    technical_context=technical.context.to_dict(),
                    sector_context=None,
                    llm_analysis=None,
                    coordinator_decision=None,
                    risk_manager_result=None,
                    warnings=[],
                    errors=[str(payload.get("collection_error") or "candidate market data unavailable")],
                )
                analyzed.append(plan)
                continue
            raw_news = payload.get("news_items", [])
            if not isinstance(raw_news, Sequence) or isinstance(raw_news, (str, bytes)):
                raw_news = []
            news = self.news_agent.analyze(
                symbol,
                [item for item in raw_news if isinstance(item, Mapping)],
                now=now,
            )
            raw_sector_benchmark = payload.get("sector_benchmark")
            sector = self.sector_agent.analyze(
                symbol,
                sector=str(payload.get("sector")) if payload.get("sector") else None,
                industry=str(payload.get("industry")) if payload.get("industry") else None,
                benchmark=(
                    raw_sector_benchmark
                    if isinstance(raw_sector_benchmark, Mapping)
                    else None
                ),
                market=macro_context,
                news=news,
            )
            candidate_records.append(
                {
                    "symbol": symbol,
                    "scanner_index": scanner_index,
                    "scanner_score": self._preliminary_scanner_score(
                        scanner_rows.get(symbol, {}), scanner_index
                    ),
                    "scanner_row": scanner_rows.get(symbol, {}),
                    "payload": payload,
                    "technical": technical,
                    "news": news,
                    "sector": sector,
                }
            )

        reasoning_result = self._reason_about_candidates(
            candidate_records,
            market=market,
            macro_context=macro_context.to_dict(),
            positions=positions,
            now=now,
        )
        llm_by_symbol = reasoning_result.by_symbol()
        if reasoning_result.status != "AVAILABLE" and candidate_records:
            warnings.append(
                "LLM reasoning unavailable; all new candidates fail closed: "
                f"{reasoning_result.failure_reason}"
            )

        for candidate_record in candidate_records:
            symbol = candidate_record["symbol"]
            technical = candidate_record["technical"]
            news = candidate_record["news"]
            sector = candidate_record["sector"]
            llm_analysis = llm_by_symbol.get(symbol)
            coordinated = self.coordinator.decide(
                symbol,
                market=macro_context,
                news=news,
                technical=technical,
                sector=sector,
                llm_analysis=llm_analysis,
                llm_failure_reason=reasoning_result.failure_reason,
            )
            risk_record: dict[str, Any] | None = None
            if coordinated.decision == "TRADE_CANDIDATE":
                plan = technical.candidate_plan
                if self.shadow_portfolio is not None:
                    shadow_state = self.shadow_portfolio.snapshot()
                    equity = shadow_state.equity
                    buying_power = shadow_state.cash
                    risk_positions = len(shadow_state.open_positions)
                    risk_trades_today = shadow_state.trades_today
                    risk_daily_pnl = shadow_state.daily_pnl
                else:
                    equity = _as_float(
                        portfolio.get("portfolio_value", portfolio.get("total_value"))
                    )
                    buying_power = _as_float(
                        portfolio.get("unleveraged_buying_power")
                    )
                    risk_positions = self._open_position_count(positions)
                    risk_trades_today = int(session.get("trades_today", 0))
                    risk_daily_pnl = daily_pnl
                if equity is None:
                    risk_record = {
                        "symbol": symbol,
                        "approved": False,
                        "reasons": ["portfolio value is unavailable"],
                        "analysis_only": True,
                    }
                else:
                    risk_record = self.risk_manager.evaluate(
                        RiskRequest(
                            account_equity=equity,
                            entry_price=plan["entry"],
                            stop_price=plan["stop"],
                            daily_realized_pnl=risk_daily_pnl,
                            open_positions=risk_positions,
                            trades_today=risk_trades_today,
                            available_buying_power=buying_power,
                        )
                    ).to_dict()
                    risk_record["symbol"] = symbol
                risk_results.append(risk_record)
                if (
                    self.shadow_portfolio is not None
                    and risk_record.get("approved") is not True
                ):
                    rejection_text = " ".join(
                        str(item) for item in risk_record.get("reasons", [])
                    ).lower()
                    rejection_code = (
                        "MAX_POSITIONS" if "simultaneous" in rejection_text
                        else "MAX_TRADES" if "trades per day" in rejection_text
                        else "DAILY_LOSS_LIMIT" if "daily loss" in rejection_text
                        else "INVALID_STOP" if "stop price" in rejection_text
                        else "RISK_REJECTED"
                    )
                    rejected_shadow_candidates.append(
                        {
                            "symbol": symbol,
                            "reason": rejection_code,
                            "details": list(risk_record.get("reasons", [])),
                        }
                    )
                coordinated = self.coordinator.decide(
                    symbol,
                    market=macro_context,
                    news=news,
                    technical=technical,
                    sector=sector,
                    llm_analysis=llm_analysis,
                    llm_failure_reason=reasoning_result.failure_reason,
                    risk_result=risk_record,
                )

            llm_record = (
                llm_analysis.to_dict() if llm_analysis is not None else None
            )
            analyzed.append(
                {
                    **dict(technical.candidate_plan),
                    "decision": coordinated.decision,
                    "preliminary_scanner_score": candidate_record["scanner_score"],
                    "preliminary_scanner_rank": candidate_record["scanner_index"] + 1,
                    "scanner_evidence": dict(candidate_record["scanner_row"]),
                    "market_context": macro_context.to_dict(),
                    "news_context": news.to_dict(),
                    "news_event_clusters": [
                        item.to_dict() for item in news.event_clusters
                    ],
                    "deterministic_technical_metrics": dict(technical.metrics),
                    "technical_context": technical.context.to_dict(),
                    "sector_context": sector.to_dict(),
                    "llm_analysis": llm_record,
                    "llm_news_analysis": (
                        llm_record["news_analysis"] if llm_record else None
                    ),
                    "llm_sector_analysis": (
                        llm_record["sector_analysis"] if llm_record else None
                    ),
                    "llm_macro_analysis": (
                        llm_record["macro_analysis"] if llm_record else None
                    ),
                    "llm_qualitative_analysis": (
                        llm_record["qualitative_analysis"] if llm_record else None
                    ),
                    "deterministic_coordinator_inputs": {
                        "technical_score": technical.context.technical_score,
                        "news_score": coordinated.news_score,
                        "sector_score": coordinated.sector_score,
                        "market_score": coordinated.market_score,
                        "qualitative_score": coordinated.qualitative_score,
                    },
                    "coordinator_weights": dict(coordinated.weights),
                    "coordinator_vetoes": list(coordinated.vetoes),
                    "coordinator_decision": coordinated.to_dict(),
                    "risk_manager_result": risk_record,
                    "warnings": [],
                    "errors": [],
                }
            )
            if coordinated.decision == "TRADE_CANDIDATE" and risk_record:
                approved_candidates.append((coordinated.to_dict(), risk_record))
            elif coordinated.decision == "WATCH":
                watch_candidates.append(coordinated.to_dict())

        if errors or not is_agentic:
            decision = {
                "type": "DATA_ERROR",
                "reason": "required Robinhood account data is unavailable or invalid",
                "executed": False,
                "analysis_only": True,
            }
        elif config.REGULAR_MARKET_ONLY and not market_open:
            decision = {
                "type": "ANALYSIS_SKIPPED",
                "reason": "regular U.S. market session is closed",
                "executed": False,
                "analysis_only": True,
            }
        elif not scanner_is_usable:
            decision = {
                "type": "DATA_ERROR",
                "reason": "a successful compatible read-only scanner result is unavailable",
                "executed": False,
                "analysis_only": True,
            }
        elif approved_candidates:
            selected_analysis, selected_risk = max(
                approved_candidates,
                key=lambda pair: float(pair[0].get("confidence", 0.0)),
            )
            decision: dict[str, Any] = {
                "type": "TRADE_CANDIDATE",
                "candidate": selected_analysis,
                "theoretical_risk": selected_risk,
                "executed": False,
                "analysis_only": True,
            }
        elif watch_candidates:
            selected_watch = max(
                watch_candidates,
                key=lambda item: float(item.get("combined_score", 0.0)),
            )
            decision = {
                "type": "WATCH",
                "candidate": selected_watch,
                "executed": False,
                "analysis_only": True,
            }
        else:
            reason = "no candidate passed both analysis and deterministic risk checks"
            if not symbols:
                reason = "no eligible scanner candidates were available"
            decision = {
                "type": "NO_TRADE",
                "reason": reason,
                "executed": False,
                "analysis_only": True,
            }

        if (
            decision.get("type") == "TRADE_CANDIDATE"
            and isinstance(decision.get("candidate"), Mapping)
        ):
            coordinated = dict(decision["candidate"])
            symbol = str(coordinated.get("symbol", "")).upper()
            analyzed_row = next(
                (item for item in analyzed if item.get("symbol") == symbol), {}
            )
            simulation_input = {
                **coordinated,
                "news_context": analyzed_row.get("news_context", {}),
                "technical_context": analyzed_row.get("technical_context", {}),
                "sector_context": analyzed_row.get("sector_context", {}),
                "market_context": analyzed_row.get("market_context", {}),
            }
            try:
                trade_plan = build_trade_plan(
                    simulation_input,
                    decision.get("theoretical_risk", {}),
                    candidate_payloads.get(symbol, {}),
                    now=now,
                )
            except TradePlanError as exc:
                rejection = {
                    "symbol": symbol,
                    "reason": "INVALID_TRADE_PLAN",
                    "details": [str(exc)],
                }
                rejected_shadow_candidates.append(rejection)
                vetoed = self.coordinator.veto(
                    coordinated, "INVALID_TRADE_PLAN"
                )
                analyzed_row["coordinator_decision"] = vetoed
                analyzed_row["decision"] = "NO_TRADE"
                decision = {
                    "type": "NO_TRADE",
                    "reason": "INVALID_TRADE_PLAN",
                    "candidate": vetoed,
                    "executed": False,
                    "analysis_only": True,
                }
            else:
                decision["trade_plan"] = trade_plan.to_dict()
                if (
                    config.MODE == "SHADOW_TRADING"
                    and not config.SHADOW_ENTRY_VIA_FAST_WATCHLIST
                ):
                    execution_result = self.execution_router.route(
                        config.MODE,
                        trade_plan,
                        now=self.clock(),  # Revalidate quote age and cutoff AFTER slow reasoning.
                        market_data=candidate_payloads.get(symbol, {}),
                    )
                    decision["execution_result"] = execution_result.to_dict()
                    if execution_result.status != "FILLED":
                        reason = (
                            execution_result.errors[0]
                            if execution_result.errors else "SHADOW_REJECTED"
                        )
                        rejection = {"symbol": symbol, "reason": reason}
                        rejected_shadow_candidates.append(rejection)
                        vetoed = self.coordinator.veto(coordinated, reason)
                        analyzed_row["coordinator_decision"] = vetoed
                        analyzed_row["decision"] = "NO_TRADE"
                        decision = {
                            "type": "NO_TRADE",
                            "reason": reason,
                            "candidate": vetoed,
                            "trade_plan": trade_plan.to_dict(),
                            "execution_result": execution_result.to_dict(),
                            "executed": False,
                            "analysis_only": True,
                        }
                    elif self.shadow_portfolio is not None:
                        opened = next(
                            (
                                item for item in self.shadow_portfolio.snapshot().open_positions
                                if item.trade_id == trade_plan.trade_id
                            ),
                            None,
                        )
                        if opened is not None:
                            new_shadow_positions.append(opened.to_dict())
                        decision["shadow_executed"] = True
                        decision["shadow_trade_id"] = trade_plan.trade_id

        shadow_summary: Mapping[str, Any] | None = None
        if self.shadow_portfolio is not None:
            with self.shadow_portfolio.lock:
                self.shadow_portfolio.revalue()
                self.shadow_portfolio.save(self.clock())
                shadow_summary = ShadowPerformance().summarize(self.shadow_portfolio.snapshot())

        account_summary = {
            "account": account,
            "portfolio": portfolio,
            "daily_realized_pnl": daily_pnl,
        }
        record = {
            "timestamp": now.isoformat(),
            "analysis_started_at": now.isoformat(),
            "mode": config.MODE,
            **snapshot_metadata,
            "account_summary": account_summary,
            "market_context": {
                **market,
                "effective_regular_session": market_open,
                "research_context": macro_context.to_dict(),
            },
            "coordinator_configuration": {
                "weights": dict(self.coordinator.weights),
                "no_trade_threshold": config.COORDINATOR_NO_TRADE_THRESHOLD,
                "trade_candidate_threshold": config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
            },
            "llm_reasoning": {
                "status": reasoning_result.status,
                "failure_reason": reasoning_result.failure_reason,
                "trace": reasoning_result.trace.to_dict(),
            },
            "positions": positions,
            "open_orders": open_orders,
            "position_reviews": position_reviews,
            "scanner_criteria": criteria,
            "scanner_candidates": scanner_candidates,
            "analyzed_candidates": analyzed,
            "decision": decision,
            "risk_manager_result": risk_results,
            "shadow_portfolio": shadow_summary,
            "shadow_positions_evaluated": shadow_positions_evaluated,
            "shadow_positions_exited": shadow_positions_exited,
            "new_shadow_positions_opened": new_shadow_positions,
            "rejected_shadow_candidates": rejected_shadow_candidates,
            "errors": list(dict.fromkeys(errors)),
            "warnings": list(dict.fromkeys(warnings)),
        }
        classify_decision(decision, analyzed, reasoning_result.status)
        safe_record = _redact(record)
        self._append_log(safe_record, now)
        self._save_session(session, now, daily_pnl)
        return safe_record

    def _reason_about_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        market: Mapping[str, Any],
        macro_context: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> LlmReasoningResult:
        if not candidates:
            trace = ReasoningTrace(
                reasoning_provider="CODEX_CLI",
                model_identifier=(
                    str(config.CODEX_REASONING_MODEL)
                    if config.CODEX_REASONING_MODEL else None
                ),
                reasoning_invocation_timestamp=now.isoformat(),
                reasoning_duration_seconds=0.0,
                candidate_count=0,
                schema_version=config.LLM_REASONING_SCHEMA_VERSION,
                prompt_version=config.LLM_REASONING_PROMPT_VERSION,
                status="NOT_INVOKED",
                failure_reason="NO_ELIGIBLE_CANDIDATES",
                token_usage=None,
            )
            return LlmReasoningResult(
                status="UNAVAILABLE",
                candidates=(),
                trace=trace,
                failure_reason="NO_ELIGIBLE_CANDIDATES",
            )

        shadow_positions: list[dict[str, Any]] = []
        if self.shadow_portfolio is not None:
            shadow_positions = [
                {
                    "symbol": item.symbol,
                    "quantity": item.quantity,
                    "entry_price": item.entry_price,
                    "stop_price": item.stop,
                    "target_price": item.target,
                }
                for item in self.shadow_portfolio.snapshot().open_positions
            ]
        reasoning_candidates: list[dict[str, Any]] = []
        for item in candidates:
            technical = item["technical"]
            news = item["news"]
            sector = item["sector"]
            source_payload = item["payload"]
            reasoning_candidates.append(
                {
                    "symbol": item["symbol"],
                    "preliminary_scanner_rank": int(item["scanner_index"]) + 1,
                    "preliminary_scanner_score": item["scanner_score"],
                    "scanner_evidence": dict(item["scanner_row"]),
                    "deterministic_technical_metrics": dict(technical.metrics),
                    "deterministic_technical_assessment": technical.context.to_dict(),
                    "deterministic_proposed_setup": dict(technical.candidate_plan),
                    "deterministic_news_event_clusters": self._bounded_news_clusters(news),
                    "deterministic_news_context": {
                        "status": (
                            "UNAVAILABLE"
                            if "news" in news.unavailable_fields else "AVAILABLE"
                        ),
                        "catalyst_found": news.catalyst_found,
                        "catalyst_type": news.catalyst_type,
                        "sentiment": news.sentiment,
                    },
                    "deterministic_sector_classification": sector.to_dict(),
                    "sector_benchmark": self._bounded_benchmark(
                        source_payload.get("sector_benchmark")
                    ),
                }
            )
        benchmarks = market.get("benchmarks", [])
        bounded_benchmarks = [
            self._bounded_benchmark(item)
            for item in benchmarks
            if isinstance(item, Mapping)
        ] if isinstance(benchmarks, Sequence) and not isinstance(
            benchmarks, (str, bytes)
        ) else []
        payload = {
            "schema_version": config.LLM_REASONING_SCHEMA_VERSION,
            "prompt_version": config.LLM_REASONING_PROMPT_VERSION,
            "analysis_timestamp": now.isoformat(),
            "broad_market_context": {
                "deterministic_interpretation": dict(macro_context),
                "benchmarks": bounded_benchmarks,
            },
            "current_real_positions": [
                {
                    "symbol": str(item.get("symbol", "")).upper(),
                    "quantity": _as_float(item.get("quantity")),
                }
                for item in positions
                if item.get("symbol")
            ],
            "current_shadow_positions": shadow_positions,
            "candidates": reasoning_candidates,
        }
        expected_symbols = [str(item["symbol"]) for item in candidates]
        try:
            return self.reasoning_bridge.reason(
                payload, expected_symbols=expected_symbols, now=now
            )
        except Exception as exc:
            failure = f"LLM_REASONING_BRIDGE_ERROR:{type(exc).__name__}"
            trace = ReasoningTrace(
                reasoning_provider="CODEX_CLI",
                model_identifier=(
                    str(config.CODEX_REASONING_MODEL)
                    if config.CODEX_REASONING_MODEL else None
                ),
                reasoning_invocation_timestamp=now.isoformat(),
                reasoning_duration_seconds=0.0,
                candidate_count=len(expected_symbols),
                schema_version=config.LLM_REASONING_SCHEMA_VERSION,
                prompt_version=config.LLM_REASONING_PROMPT_VERSION,
                status="FAILED",
                failure_reason=failure,
                token_usage=None,
            )
            return LlmReasoningResult(
                status="UNAVAILABLE", candidates=(), trace=trace,
                failure_reason=failure,
            )

    @staticmethod
    def _bounded_news_clusters(news: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for cluster in news.event_clusters[: config.LLM_REASONING_MAX_NEWS_CLUSTERS]:
            value = cluster.to_dict()
            value["sources"] = list(value.get("sources", []))[
                : config.LLM_REASONING_MAX_SOURCES_PER_CLUSTER
            ]
            result.append(value)
        return result

    @staticmethod
    def _bounded_benchmark(value: Any) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        fields = (
            "symbol", "current_price", "vwap", "ema9", "ema20",
            "intraday_change_percent", "quote_as_of",
        )
        result = {key: value.get(key) for key in fields}
        candles = value.get("candles", [])
        result["candles"] = (
            [dict(item) for item in candles if isinstance(item, Mapping)][
                -config.LLM_REASONING_MAX_CANDLES :
            ]
            if isinstance(candles, Sequence)
            and not isinstance(candles, (str, bytes))
            else []
        )
        return result

    @staticmethod
    def _safe_call(
        operation: Callable[[], T], default: T, errors: list[str], label: str
    ) -> T:
        try:
            return operation()
        except Exception as exc:  # fail closed and preserve the complete cycle log
            errors.append(f"unable to retrieve {label}: {type(exc).__name__}: {exc}")
            return default

    @staticmethod
    def _analyze_positions_first(
        positions: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for position in positions:
            symbol = str(position.get("symbol", "UNKNOWN")).upper()
            unavailable = [
                field
                for field in ("quantity", "average_buy_price", "current_price")
                if position.get(field) is None
            ]
            reviews.append(
                {
                    "symbol": symbol,
                    "status": "ANALYSIS_ONLY_NO_ACTION",
                    "summary": "Existing position observed before scanning; no changes made.",
                    "unavailable_values": unavailable,
                }
            )
        return reviews

    @staticmethod
    def _open_position_count(positions: Sequence[Mapping[str, Any]]) -> int:
        count = 0
        for position in positions:
            quantity = _as_float(position.get("quantity"))
            if quantity is not None and quantity != 0:
                count += 1
        return count

    @staticmethod
    def _preliminary_scanner_score(
        row: Mapping[str, Any], rank_index: int
    ) -> float:
        """Score retained rows deterministically without additional data calls."""

        columns = row.get("columns", [])
        values: dict[str, float] = {}
        if isinstance(columns, Mapping):
            iterable = columns.items()
        elif isinstance(columns, Sequence) and not isinstance(columns, (str, bytes)):
            iterable = (
                (item.get("name"), item.get("value"))
                for item in columns
                if isinstance(item, Mapping)
            )
        else:
            iterable = ()
        for name, value in iterable:
            number = _as_float(str(value).replace("%", "").replace(",", ""))
            if number is not None:
                values[str(name).lower()] = number
        change = values.get("% change")
        relative = values.get("relative volume")
        volume = values.get("volume")
        if change is None and relative is None and volume is None:
            return round(max(0.0, 1.0 - rank_index / config.MAX_CANDIDATES_TO_ANALYZE), 3)
        if change is not None and abs(change) > 1:
            change /= 100
        change_score = min(max((change or 0.0) / 0.10, 0.0), 1.0)
        relative_score = min(max((relative or 0.0) / 3.0, 0.0), 1.0)
        volume_score = min(max((volume or 0.0) / 5_000_000, 0.0), 1.0)
        return round(0.5 * change_score + 0.3 * relative_score + 0.2 * volume_score, 3)

    @staticmethod
    def _is_supported_momentum_scan(criteria: Sequence[Any]) -> bool:
        """Require all verified stock/liquidity/momentum filter types.

        The read-only scan response may call the enum field ``filter_type_enum``
        while normalized snapshots may use ``filter_type``. Requiring the full
        verified set prevents unrelated saved scans from becoming candidates.
        """

        actual: set[str] = set()
        stock_filter = False
        for item in criteria:
            if not isinstance(item, Mapping):
                continue
            filter_type = str(
                item.get("filter_type_enum", item.get("filter_type", ""))
            )
            actual.add(filter_type)
            if filter_type == "FILTER_TYPE_INSTRUMENT_TYPE":
                values = item.get("values", [])
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    stock_filter = "STOCK" in values

        required = {str(item["filter_type"]) for item in config.SCANNER_CRITERIA}
        return stock_filter and required.issubset(actual)

    def _load_session(self, now: datetime) -> dict[str, Any]:
        eastern_date = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()
        default = {
            "date": eastern_date,
            "cycle_count": 0,
            "trades_today": 0,
            "daily_realized_pnl": None,
            "last_cycle_at": None,
        }
        if not self.state_path.exists():
            return default
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(value, dict) or value.get("date") != eastern_date:
            return default
        default.update(value)
        return default

    def _save_session(
        self, session: dict[str, Any], now: datetime, daily_pnl: float | None
    ) -> None:
        eastern_date = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()
        updated = {
            "date": eastern_date,
            "cycle_count": int(session.get("cycle_count", 0)) + 1,
            # Trading is disabled: never increment this in an analysis cycle.
            "trades_today": int(session.get("trades_today", 0)),
            "daily_realized_pnl": daily_pnl,
            "last_cycle_at": now.isoformat(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def _append_log(self, record: Mapping[str, Any], now: datetime) -> None:
        eastern_date = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{eastern_date}.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
