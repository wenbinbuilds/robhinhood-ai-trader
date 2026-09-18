"""Typed slow-research context and bounded local watchlist persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence
import json

import config
from agent.gate_policy import TRUE_HARD_CLASSIFICATIONS, failed_gates
from watcher.models import timestamp
from watcher.storage import atomic_json


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


@dataclass
class CandidateContext:
    symbol: str
    research_cycle_id: str
    research_timestamp: str
    analysis_price: float
    slow_context_score: float
    technical_context_score: float
    qualitative_score: float
    news_score: float
    sector_score: float
    market_score: float
    llm_thesis: str
    llm_conflicts: list[str]
    catalyst_classification: str
    slow_technical_evidence: list[str]
    slow_technical_conflicts: list[str]
    slow_hard_gate_status: dict[str, Any]
    suggested_stop_reference: float
    suggested_target_reference: float
    invalidation_condition: str
    expiration_timestamp: str
    research_vwap: float | None
    research_ema9: float | None
    intraday_support_reference: float | None
    intraday_resistance_reference: float | None
    research_bid: float | None
    research_ask: float | None
    risk_reward_ratio: float
    coordinator_confidence: float
    slow_score_formula: str
    technical_confidence: float | None = None
    true_hard_gate_failures: list[str] = field(default_factory=list)
    signal_quality_failures: list[str] = field(default_factory=list)
    status: str = "WATCH"
    live_market_score: float | None = None
    slow_weight: float | None = None
    live_weight: float | None = None
    dynamic_score: float | None = None
    current_price: float | None = None
    context_age_seconds: float | None = None
    consecutive_qualifying_updates: int = 0
    last_updated_at: str | None = None
    last_history_at: str | None = None
    candidate_state: str = "SETUP_FORMING"
    discovery_timestamp: str | None = None
    watchlist_timestamp: str | None = None
    trade_ready_timestamp: str | None = None
    state_transition_history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Canonical, non-secret technical inputs from the admitting slow cycle.
    # A pre-execution quote refresh overlays this mapping without allowing
    # absent/None partial fields to erase valid slow research.
    canonical_market_data: dict[str, Any] = field(default_factory=dict)
    qualitative_generated_at: str | None = None
    qualitative_cache_hit: bool = False
    qualitative_cache_age_seconds: float | None = None
    episode_id: str = ''

    def __post_init__(self):
        if not self.episode_id:
            from trading_runtime.setup_controller import episode_id
            self.episode_id = episode_id(self.symbol, self.research_cycle_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateContext:
        return cls(**{
            name: value[name]
            for name in cls.__dataclass_fields__
            if name in value
        })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def age_at(self, now: datetime) -> float | None:
        at = timestamp(self.research_timestamp)
        return None if at is None else (now.astimezone(timezone.utc) - at).total_seconds()

    def expired_at(self, now: datetime) -> bool:
        expires = timestamp(self.expiration_timestamp)
        return expires is None or now.astimezone(timezone.utc) >= expires

    def pre_execution_market_data(self) -> dict[str, Any]:
        """Return the single canonical slow context used by entry revalidation."""

        result = dict(self.canonical_market_data)
        fallbacks = {
            "symbol": self.symbol,
            "current_price": self.analysis_price,
            "bid": self.research_bid,
            "ask": self.research_ask,
            "vwap": self.research_vwap,
            "ema9": self.research_ema9,
            "intraday_support_reference": self.intraday_support_reference,
            "intraday_resistance_reference": self.intraday_resistance_reference,
            "market_direction": self.metadata.get("market_regime"),
            "context_timestamp": self.research_timestamp,
            "context_expires_at": self.expiration_timestamp,
        }
        for name, value in fallbacks.items():
            if result.get(name) is None and value is not None:
                result[name] = value
        return result


class CandidateContextStore:
    """One atomic JSON object shared by the slow and fast local threads."""

    def __init__(self, path: str | Path = config.CANDIDATE_WATCHLIST_PATH) -> None:
        self.path = Path(path)
        self.lock = RLock()
        self.contexts: dict[str, CandidateContext] = {}
        self.generated_at: str | None = None
        self._load()
        from trading_runtime.journal import EventJournal
        self.journal = EventJournal(self.path.with_suffix('.episodes.jsonl'))

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            return
        rows = value.get("candidates", [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return
        for row in rows:
            try:
                context = CandidateContext.from_dict(row)
            except (TypeError, KeyError):
                continue
            if context.symbol and context.symbol not in self.contexts:
                self.contexts[context.symbol] = context
        self.generated_at = value.get("generated_at") if isinstance(value.get("generated_at"), str) else None

    def snapshot(self) -> list[CandidateContext]:
        with self.lock:
            return [CandidateContext.from_dict(item.to_dict()) for item in self.contexts.values()]

    def replace(self, contexts: Sequence[CandidateContext], *, now: datetime,
                exclude_symbols: Sequence[str] = (),
                preserve_research: bool = False) -> None:
        excluded = {symbol.upper() for symbol in exclude_symbols}
        with self.lock:
            current = self.contexts
            timestamp_text = now.astimezone(timezone.utc).isoformat()
            for context in contexts:
                prior = current.get(context.symbol)
                if prior is not None and (preserve_research or prior.episode_id == context.episode_id):
                    context.discovery_timestamp = prior.discovery_timestamp
                    context.watchlist_timestamp = prior.watchlist_timestamp
                    context.state_transition_history = list(prior.state_transition_history)
                    if preserve_research:
                        context.episode_id = prior.episode_id
                        context.research_cycle_id = prior.research_cycle_id
                        context.research_timestamp = prior.research_timestamp
                        context.expiration_timestamp = prior.expiration_timestamp
                context.discovery_timestamp = context.discovery_timestamp or timestamp_text
                context.watchlist_timestamp = context.watchlist_timestamp or timestamp_text
                if not context.state_transition_history:
                    context.state_transition_history = [
                        {"timestamp": timestamp_text, "previous_state": None,
                         "new_state": "DISCOVERED", "reason": "SCANNER_DISCOVERY"},
                        {"timestamp": timestamp_text, "previous_state": "DISCOVERED",
                         "new_state": "WATCHLIST", "reason": "SCANNER_ADMISSION"},
                        {"timestamp": timestamp_text, "previous_state": "WATCHLIST",
                         "new_state": "SETUP_FORMING", "reason": "SLOW_ALPHA_READY"},
                    ]
            self.contexts = {
                context.symbol: context
                for context in contexts
                if context.symbol not in excluded
            }
            self.generated_at = now.astimezone(timezone.utc).isoformat()
            self.save()
            from trading_runtime.journal import RuntimeEvent, RuntimeEventType
            from trading_runtime.setup_controller import SetupController
            for context in self.contexts.values():
                self.journal.append(RuntimeEvent(
                    RuntimeEventType.SLOW_ALPHA_READY, timestamp_text, context.symbol,
                    context.episode_id, context.research_cycle_id,
                    {'context': context.to_dict(), 'alpha': SetupController.alpha(context, now).to_dict()},
                    event_id='research:' + context.episode_id))

    def remove(self, symbol: str) -> None:
        with self.lock:
            self.contexts.pop(symbol.upper(), None)
            self.save()

    def save(self) -> None:
        with self.lock:
            atomic_json(self.path, {
                "schema_version": 1,
                "generated_at": self.generated_at,
                "trade_threshold": config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
                "context_ttl_seconds": config.CANDIDATE_CONTEXT_TTL_SECONDS,
                "candidates": [self.contexts[symbol].to_dict() for symbol in sorted(self.contexts)],
            })


def contexts_from_cycle(result: Mapping[str, Any], *, now: datetime) -> list[CandidateContext]:
    """Admit monitorable candidates that pass every true-hard slow rule."""

    market = result.get("market_context", {})
    reasoning = result.get("llm_reasoning", {})
    if not isinstance(market, Mapping) or market.get("effective_regular_session") is not True:
        return []
    if not isinstance(reasoning, Mapping) or reasoning.get("status") != "AVAILABLE":
        return []
    rows = result.get("analyzed_candidates", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    timestamp_text = now.astimezone(timezone.utc).isoformat()
    expires = (now.astimezone(timezone.utc) + timedelta(seconds=config.CANDIDATE_CONTEXT_TTL_SECONDS)).isoformat()
    cycle_id = f"{timestamp_text}:{result.get('generated_at', '')}"
    contexts: list[CandidateContext] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        coordinator = row.get("coordinator_decision", {})
        technical = row.get("technical_context", {})
        metrics = row.get("deterministic_technical_metrics", {})
        llm = row.get("llm_analysis", {})
        if not all(isinstance(item, Mapping) for item in (coordinator, technical, metrics, llm)):
            continue
        score = _number(coordinator.get("combined_score"))
        validation = metrics.get("technical_validation", {})
        vetoes = coordinator.get("vetoes", [])
        true_hard_failures, signal_quality_failures = (
            failed_gates(validation) if isinstance(validation, Mapping)
            else (["TECHNICAL_VALIDATION_MALFORMED"], [])
        )
        if (
            score is None
            or score < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE
            or row.get("technical_disposition") not in {"QUALIFIED", "MONITORABLE"}
            or metrics.get("technical_data_valid") is not True
            or bool(true_hard_failures)
            or (isinstance(vetoes, Sequence) and not isinstance(vetoes, (str, bytes)) and bool(vetoes))
        ):
            continue
        values = {
            "analysis_price": _number(metrics.get("current_price")),
            "stop": _number(row.get("stop")),
            "target": _number(row.get("target")),
            "risk_reward": _number(row.get("risk_reward_ratio")),
        }
        if (
            any(value is None for value in values.values())
            or not (0 < values["stop"] < values["analysis_price"] < values["target"])
            or values["risk_reward"] < config.MIN_RISK_REWARD_RATIO
        ):
            continue
        qualitative = llm.get("qualitative_analysis", {})
        news = llm.get("news_analysis", {})
        evidence = technical.get("evidence", [])
        conflicts = technical.get("conflicts", [])
        rules = validation.get("rules", []) if isinstance(validation, Mapping) else []
        trace = reasoning.get("trace", {}) if isinstance(reasoning, Mapping) else {}
        diagnostics = trace.get("diagnostics", {}) if isinstance(trace, Mapping) else {}
        cache_rows = diagnostics.get("per_candidate", {}) if isinstance(diagnostics, Mapping) else {}
        cache_detail = cache_rows.get(str(row.get("symbol", "")).upper(), {}) if isinstance(cache_rows, Mapping) else {}
        hard_rules = [
            item for item in rules
            if isinstance(item, Mapping)
            and item.get("classification") in TRUE_HARD_CLASSIFICATIONS
        ]
        contexts.append(CandidateContext(
            symbol=str(row.get("symbol", "")).upper(),
            research_cycle_id=cycle_id,
            research_timestamp=timestamp_text,
            analysis_price=float(values["analysis_price"]),
            slow_context_score=max(0.0, min(1.0, score)),
            technical_context_score=float(_number(coordinator.get("technical_score")) or 0),
            qualitative_score=float(_number(coordinator.get("qualitative_score")) or 0),
            news_score=float(_number(coordinator.get("news_score")) or 0),
            sector_score=float(_number(coordinator.get("sector_score")) or 0),
            market_score=float(_number(coordinator.get("market_score")) or 0),
            llm_thesis=str(qualitative.get("summary", "")) if isinstance(qualitative, Mapping) else "",
            llm_conflicts=[str(item) for item in (qualitative.get("reasons_against", []) if isinstance(qualitative, Mapping) else [])],
            catalyst_classification=str(news.get("catalyst_type", "NONE")) if isinstance(news, Mapping) else "NONE",
            slow_technical_evidence=[str(item) for item in evidence] if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)) else [],
            slow_technical_conflicts=[str(item) for item in conflicts] if isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) else [],
            slow_hard_gate_status={
                str(item.get("rule_name")): str(item.get("status")) for item in hard_rules
            },
            suggested_stop_reference=float(values["stop"]),
            suggested_target_reference=float(values["target"]),
            invalidation_condition=str(row.get("invalidation_condition") or ""),
            expiration_timestamp=expires,
            research_vwap=_number(metrics.get("vwap")),
            research_ema9=_number(metrics.get("ema9")),
            intraday_support_reference=_number(row.get("supporting_indicators", {}).get("intraday_support_reference")) if isinstance(row.get("supporting_indicators"), Mapping) else None,
            intraday_resistance_reference=_number(row.get("supporting_indicators", {}).get("intraday_resistance_reference")) if isinstance(row.get("supporting_indicators"), Mapping) else None,
            research_bid=_number(metrics.get("bid")),
            research_ask=_number(metrics.get("ask")),
            risk_reward_ratio=float(values["risk_reward"]),
            coordinator_confidence=float(_number(coordinator.get("confidence")) or 0),
            slow_score_formula=" + ".join(
                f"{config.COORDINATOR_WEIGHTS[name]:.2f}*{name}"
                for name in ("technical", "news", "sector", "market", "qualitative")
            ),
            technical_confidence=_number(
                validation.get("technical_confidence", validation.get("confidence"))
            ) if isinstance(validation, Mapping) else None,
            true_hard_gate_failures=true_hard_failures,
            signal_quality_failures=signal_quality_failures,
            metadata={
                'score_provenance': {
                    'technical': {'status': 'AVAILABLE', 'source': 'PYTHON_COMPLETED_CANDLES', 'source_timestamp': metrics.get('latest_completed_bar_timestamp')},
                    'news': {'status': 'UNAVAILABLE' if row.get('news_context', {}).get('unavailable_fields') else 'UNKNOWN', 'source': 'NEWS_CONTEXT', 'context': str(row.get('news_context', {}).get('sentiment', 'UNKNOWN')), 'fallback_used': bool(row.get('news_context', {}).get('unavailable_fields'))},
                    'sector': {'status': 'UNKNOWN', 'source': 'SECTOR_CONTEXT', 'context': str(row.get('sector_context', {}).get('sector', 'UNKNOWN'))},
                    'market': {'status': 'UNKNOWN', 'source': 'MARKET_CONTEXT', 'context': str(row.get('market_context', {}).get('regime', 'UNKNOWN'))},
                    'qualitative': {'status': 'AVAILABLE' if llm else 'UNAVAILABLE', 'source': config.CODEX_REASONING_MODEL, 'cache_hit': bool(cache_detail.get('cache_hit')), 'source_timestamp': cache_detail.get('generated_at')},
                },
                "sector": (
                    str(row.get("sector_context", {}).get("sector", "GENERAL"))
                    if isinstance(row.get("sector_context"), Mapping) else "GENERAL"
                ),
                "market_regime": (
                    str(row.get("market_context", {}).get("regime", "UNKNOWN"))
                    if isinstance(row.get("market_context"), Mapping) else "UNKNOWN"
                ),
                "admission_reason": (
                    "FAST_WATCH_ADMITTED_WITH_SIGNAL_QUALITY_WARNINGS"
                    if signal_quality_failures else "FAST_WATCH_ADMITTED"
                ),
            },
            canonical_market_data={
                **{
                    name: metrics.get(name)
                    for name in (
                        "current_price", "bid", "ask", "quote_as_of",
                        "quote_retrieved_at", "volume", "relative_volume",
                        "vwap", "ema9", "ema20", "rsi14", "macd",
                        "macd_signal", "macd_histogram", "intraday_high",
                        "intraday_low", "previous_close", "level2",
                    )
                    if metrics.get(name) is not None
                },
                "symbol": str(row.get("symbol", "")).upper(),
                "intraday_support_reference": _number(
                    row.get("supporting_indicators", {}).get(
                        "intraday_support_reference"
                    )
                ) if isinstance(row.get("supporting_indicators"), Mapping) else None,
                "intraday_resistance_reference": _number(
                    row.get("supporting_indicators", {}).get(
                        "intraday_resistance_reference"
                    )
                ) if isinstance(row.get("supporting_indicators"), Mapping) else None,
                "market_direction": (
                    str(row.get("market_context", {}).get("regime", "UNKNOWN"))
                    if isinstance(row.get("market_context"), Mapping) else "UNKNOWN"
                ),
                "candles": list(metrics.get("recent_5_minute_candles", []))
                if isinstance(metrics.get("recent_5_minute_candles"), Sequence)
                and not isinstance(metrics.get("recent_5_minute_candles"), (str, bytes))
                else [],
                "context_timestamp": timestamp_text,
                "context_expires_at": expires,
            },
            qualitative_generated_at=(
                str(cache_detail.get("generated_at"))
                if isinstance(cache_detail, Mapping) and cache_detail.get("generated_at")
                else str(trace.get("reasoning_invocation_timestamp"))
                if isinstance(trace, Mapping) and trace.get("reasoning_invocation_timestamp")
                else None
            ),
            qualitative_cache_hit=(
                bool(cache_detail.get("cache_hit"))
                if isinstance(cache_detail, Mapping) else False
            ),
            qualitative_cache_age_seconds=(
                _number(cache_detail.get("cache_age_seconds"))
                if isinstance(cache_detail, Mapping) else None
            ),
        ))
    return contexts
