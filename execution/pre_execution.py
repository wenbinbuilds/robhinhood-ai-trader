"""Mandatory single-symbol market-data refresh before an entry can execute."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import config
from execution.models import TradePlan, TradePlanError, build_trade_plan, parse_timestamp
from risk.risk_manager import RiskManager, RiskRequest
from execution.geometry import completed_structure, geometry_diagnostics, GeometryEngine
from trading_runtime.contracts import GeometryDecision, RiskDecision


# One owner per pre-entry fact. Slow-owned fields may be replaced by a valid
# recomputation, but never erased by an absent value in a partial refresh.
FIELD_OWNERSHIP: Mapping[str, str] = {
    "symbol": "candidate_state_and_fresh_quote_identity",
    "current_price": "fresh_quote",
    "bid": "fresh_quote",
    "ask": "fresh_quote",
    "quote_as_of": "fresh_quote_exchange_timestamp",
    "volume": "fresh_candles_or_canonical_slow_research",
    "relative_volume": "fresh_candles_or_canonical_slow_research",
    "vwap": "fresh_candles_or_canonical_slow_research",
    "ema9": "fresh_candles_or_canonical_slow_research",
    "ema20": "fresh_candles_or_canonical_slow_research",
    "rsi14": "fresh_candles_or_canonical_slow_research",
    "macd": "fresh_candles_or_canonical_slow_research",
    "macd_signal": "fresh_candles_or_canonical_slow_research",
    "candles": "fresh_recent_candles_or_canonical_slow_research",
    "intraday_support_reference": "technical_context",
    "intraday_resistance_reference": "technical_context",
    "context_timestamp": "candidate_state",
    "context_expires_at": "candidate_state",
    "risk_parameters": "risk_manager_and_config",
}


class PreExecutionMarketDataProvider(Protocol):
    def refresh_symbol(self, symbol: str, *, now: datetime) -> Mapping[str, Any]:
        """Return a newly fetched normalized quote and recent candles for symbol."""


@dataclass(frozen=True)
class PreExecutionRiskContext:
    account_equity: float
    available_buying_power: float | None
    daily_realized_pnl: float | None
    open_positions: int
    trades_today: int


@dataclass(frozen=True)
class PreExecutionResult:
    approved: bool
    reason: str | None
    plan: TradePlan | None
    market_data: Mapping[str, Any]
    quote_age_seconds: float | None
    risk_reward_ratio: float | None
    risk_result: Mapping[str, Any]
    technical_metrics: Mapping[str, Any]
    log_record: Mapping[str, Any]
    geometry_decision: GeometryDecision | None = None
    risk_decision: RiskDecision | None = None


class PreExecutionValidator:
    """Rebuild geometry and sizing from post-reasoning market facts."""

    def __init__(self, *, technical_agent=None,
                 risk_manager: RiskManager | None = None) -> None:
        if technical_agent is None:
            from agent.technical_agent import TechnicalAgent
            technical_agent = TechnicalAgent()
        self.technical_agent = technical_agent
        self.risk_manager = risk_manager or RiskManager()

    def evaluate(
        self,
        coordinator: Mapping[str, Any],
        refresher: PreExecutionMarketDataProvider | None,
        risk: PreExecutionRiskContext,
        *,
        now: datetime,
        trade_id: str | None = None,
        canonical_context: Mapping[str, Any] | None = None,
    ) -> PreExecutionResult:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        symbol = str(coordinator.get("symbol", "")).upper()
        from trading_runtime.setup_controller import episode_id
        identity = dict(episode_id=coordinator.get('episode_id') or episode_id(symbol, coordinator.get('research_cycle_id') or current.isoformat()),
                        research_cycle_id=coordinator.get('research_cycle_id') or current.isoformat())
        if refresher is None:
            return self._rejected(symbol, current, "PRE_EXECUTION_REFRESH_UNAVAILABLE", market_data=identity)
        try:
            refreshed = refresher.refresh_symbol(symbol, now=current)
        except Exception as exc:
            return self._rejected(
                symbol, current,
                f"PRE_EXECUTION_REFRESH_FAILED:{type(exc).__name__}",
                market_data=identity,
            )
        if not isinstance(refreshed, Mapping):
            return self._rejected(symbol, current, "PRE_EXECUTION_REFRESH_MALFORMED", market_data=identity)
        refreshed_data = dict(refreshed)
        refreshed_data.update(identity)
        finished = parse_timestamp(refreshed_data.get('refresh_completed_at'))
        if finished is not None and finished > current:
            current = finished
        if 'structure_evidence' in refreshed_data:
            try:
                # Recompute rather than trusting proposed target/stop values.
                refreshed_data.update(completed_structure(refreshed_data.get('candles', []), now=current))
            except ValueError as exc:
                return self._rejected(symbol, current, str(exc), market_data=refreshed_data)
        if str(refreshed_data.get("symbol", "")).upper() != symbol:
            return self._rejected(
                symbol, current, "PRE_EXECUTION_REFRESH_SYMBOL_MISMATCH",
                market_data=refreshed_data,
            )

        quote_at = parse_timestamp(refreshed_data.get("quote_as_of"))
        if quote_at is None:
            return self._rejected(
                symbol, current, "REFRESHED_EXCHANGE_TIMESTAMP_MISSING_OR_INVALID",
                market_data=refreshed_data,
            )
        quote_age = (current - quote_at).total_seconds()
        from trading_runtime.contracts import MarketSnapshot
        market_snapshot = MarketSnapshot.from_mapping(refreshed_data, now=current,
                              max_age=config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS)
        refreshed_data['market_quality'] = {'quote': market_snapshot.quote_status.value,
                                          'candles': market_snapshot.candle_status.value}
        if quote_age < 0:
            return self._rejected(
                symbol, current, "REFRESHED_QUOTE_TIMESTAMP_FUTURE",
                market_data=refreshed_data, quote_age=quote_age,
            )
        if quote_age > config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS:
            return self._rejected(
                symbol, current, "REFRESHED_QUOTE_STALE",
                market_data=refreshed_data, quote_age=quote_age,
            )

        market_data = self.merge_market_data(canonical_context, refreshed_data)
        geometry_timestamps = self.geometry_timestamp_evidence(market_data, current)
        market_data['geometry_timestamps'] = geometry_timestamps
        if (market_data.get('structure_evidence')
                and not geometry_timestamps['coherent']):
            return self._rejected(
                symbol, current, 'GEOMETRY_TIMESTAMPS_INCOHERENT',
                market_data=market_data, quote_age=quote_age,
            )

        from agent.candidate_analyzer import CandidateData
        from agent.gate_policy import failed_gates

        technical = self.technical_agent.analyze(
            CandidateData.from_mapping(market_data), now=current
        )
        metrics = dict(technical.metrics)
        setup = technical.candidate_plan
        validation = metrics.get("technical_validation")
        rules = (
            validation.get("rules", []) if isinstance(validation, Mapping) else []
        )
        rules_by_name = {
            str(item.get("rule_name")): item
            for item in rules if isinstance(item, Mapping)
        }
        stop_rule = rules_by_name.get("STOP_REFERENCE_AVAILABLE", {})
        resistance_rule = rules_by_name.get("RESISTANCE_ABOVE_ENTRY", {})
        resistance_actual = resistance_rule.get("actual", {})
        resistance_actual = (
            resistance_actual if isinstance(resistance_actual, Mapping) else {}
        )
        rr_rule = rules_by_name.get("MINIMUM_RISK_REWARD", {})
        # CandidateAnalyzer omits plan fields when a hard geometry rule fails.
        # Preserve the audited inputs anyway so a rejection can be diagnosed.
        audited_entry = resistance_actual.get("entry")
        audited_stop = stop_rule.get("actual")
        audited_target = resistance_actual.get("resistance")
        audited_rr = rr_rule.get("actual")
        metrics.update(
            refreshed_entry=(setup.get("entry") if setup.get("entry") is not None else audited_entry),
            refreshed_stop=(setup.get("stop") if setup.get("stop") is not None else audited_stop),
            refreshed_target=(setup.get("target") if setup.get("target") is not None else audited_target),
            refreshed_risk_reward_ratio=(
                setup.get("risk_reward_ratio")
                if setup.get("risk_reward_ratio") is not None else audited_rr
            ),
        )
        metrics['geometry_diagnostics'] = geometry_diagnostics(
            coordinator, metrics['refreshed_entry'], metrics['refreshed_stop'],
            metrics['refreshed_target'], evidence=market_data.get('structure_evidence'),
        )
        stop_sources = {
            name: market_data.get(name) for name in
            ('intraday_support_reference', 'intraday_low', 'ema20', 'vwap')
        }
        metrics['geometry_diagnostics']['stop_sources'] = stop_sources
        metrics['geometry_diagnostics']['stop_selection_reason'] = [
            name for name, value in stop_sources.items() if value == audited_stop
        ]
        hard_failures, _signal_failures = (
            failed_gates(validation) if isinstance(validation, Mapping)
            else (["TECHNICAL_VALIDATION_MALFORMED"], [])
        )
        if hard_failures:
            metrics['geometry_decision'] = asdict(GeometryEngine.decision(
                symbol, identity['episode_id'], current, metrics, market_data, hard_failures))
            return self._rejected(
                symbol, current,
                "REFRESHED_HARD_GATE_FAILED:" + ",".join(hard_failures),
                market_data=market_data, quote_age=quote_age,
                metrics=metrics,
            )

        entry = setup.get("entry")
        stop = setup.get("stop")
        target = setup.get("target")
        risk_reward = setup.get("risk_reward_ratio")
        if any(value is None for value in (entry, stop, target, risk_reward)):
            return self._rejected(
                symbol, current, "REFRESHED_TRADE_GEOMETRY_UNAVAILABLE",
                market_data=market_data, quote_age=quote_age,
                risk_reward=risk_reward, metrics=metrics,
            )

        geometry = GeometryEngine.decision(symbol, identity['episode_id'], current, metrics, market_data)
        risk_evaluation, risk_decision = self.risk_manager.evaluate_geometry(geometry, risk)
        risk_result = risk_evaluation.to_dict()
        metrics['geometry_decision'] = asdict(geometry)
        metrics['risk_decision'] = asdict(risk_decision)
        if risk_result.get("approved") is not True:
            reasons = risk_result.get("reasons", [])
            reason_text = " ".join(str(item) for item in reasons).lower()
            code = (
                "MAX_POSITIONS" if "simultaneous" in reason_text
                else "MAX_TRADES" if "trades per day" in reason_text
                else "DAILY_LOSS_LIMIT" if "daily loss" in reason_text
                else "BUYING_POWER_UNAVAILABLE" if "buying power" in reason_text
                else "RISK_REJECTED"
            )
            return self._rejected(
                symbol, current, "REFRESHED_RISK_REJECTED:" + code,
                market_data=market_data, quote_age=quote_age,
                risk_reward=float(risk_reward), risk_result=risk_result,
                metrics=metrics,
            )

        refreshed_coordinator = {
            **dict(coordinator),
            **identity,
            "decision": "TRADE_CANDIDATE",
            "entry": entry,
            "stop": stop,
            "target": target,
            "risk_per_share": setup.get("risk_per_share"),
            "risk_reward_ratio": risk_reward,
            "technical_score": technical.context.technical_score,
            "technical_context": technical.context.to_dict(),
            "supporting_indicators": dict(setup.get("supporting_indicators", {})),
            "invalidation_condition": setup.get("invalidation_condition"),
        }
        try:
            plan = build_trade_plan(
                refreshed_coordinator, risk_result, market_data,
                now=current, trade_id=trade_id,
            )
        except TradePlanError as exc:
            return self._rejected(
                symbol, current,
                f"REFRESHED_TRADE_PLAN_INVALID:{type(exc).__name__}",
                market_data=market_data, quote_age=quote_age,
                risk_reward=float(risk_reward), risk_result=risk_result,
                metrics=metrics,
            )
        record = self._record(
            symbol, current, "APPROVED", None, market_data,
            quote_age, float(risk_reward), metrics, risk_result,
        )
        market_data["pre_execution_refresh"] = record
        return PreExecutionResult(
            True, None, plan, market_data, quote_age, float(risk_reward),
            risk_result, metrics, record, geometry, risk_decision,
        )

    @staticmethod
    def geometry_timestamp_evidence(
        market_data: Mapping[str, Any], evaluated_at: datetime,
    ) -> dict[str, Any]:
        """Prove the quote is no older than the completed structural bar set."""

        quote_at = parse_timestamp(market_data.get('quote_as_of'))
        structure = market_data.get('structure_evidence')
        structure = structure if isinstance(structure, Mapping) else {}
        bar_at = parse_timestamp(structure.get('latest_completed_bar'))
        interval_seconds = 300.0
        if bar_at is not None:
            for candle in market_data.get('candles', []) or []:
                if not isinstance(candle, Mapping):
                    continue
                if parse_timestamp(candle.get('begins_at')) == bar_at:
                    raw_interval = candle.get('interval_seconds', 300)
                    if isinstance(raw_interval, (int, float)) and raw_interval > 0:
                        interval_seconds = float(raw_interval)
                    break
        bar_close = bar_at + timedelta(seconds=interval_seconds) if bar_at else None
        refreshed_at = parse_timestamp(market_data.get('refresh_completed_at'))
        current = evaluated_at.astimezone(timezone.utc)
        coherent = bool(
            quote_at and bar_close
            and quote_at >= bar_close
            and quote_at <= current
            and (refreshed_at is None or quote_at <= refreshed_at <= current)
        )
        return {
            'quote_timestamp': quote_at.isoformat() if quote_at else None,
            'support_timestamp': bar_at.isoformat() if bar_at else None,
            'resistance_timestamp': bar_at.isoformat() if bar_at else None,
            'latest_completed_bar_timestamp': bar_at.isoformat() if bar_at else None,
            'latest_completed_bar_close_timestamp': (
                bar_close.isoformat() if bar_close else None
            ),
            'structure_generated_at': refreshed_at.isoformat() if refreshed_at else None,
            'geometry_evaluated_at': current.isoformat(),
            'coherent': coherent,
            'rule': 'quote_timestamp >= latest_completed_bar_close_timestamp',
        }

    @staticmethod
    def merge_market_data(
        canonical_context: Mapping[str, Any] | None,
        refreshed: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Overlay a partial refresh without erasing canonical slow fields.

        Quote facts are refresh-authoritative and may never fall back to their
        pre-LLM values. Other explicitly recomputed values replace the slow
        value only when usable; None/empty partial values preserve the owner.
        """

        result = dict(canonical_context or {})
        quote_owned = {
            "symbol", "current_price", "bid", "ask", "quote_as_of",
            "candidate_quote_retrieved_at", "quote_retrieved_at",
            "quote_freshness_source",
        }
        for name, value in refreshed.items():
            if name in quote_owned:
                result[name] = value
            elif value is not None and value != "" and value != [] and value != {}:
                result[name] = value
        return result

    def _rejected(
        self, symbol: str, now: datetime, reason: str, *,
        market_data: Mapping[str, Any] | None = None,
        quote_age: float | None = None,
        risk_reward: float | None = None,
        risk_result: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> PreExecutionResult:
        data = dict(market_data or {})
        risk_value = dict(risk_result or {})
        metric_value = dict(metrics or {})
        geometry = GeometryDecision(**metric_value['geometry_decision']) if metric_value.get('geometry_decision') else None
        decision = RiskDecision(**metric_value['risk_decision']) if metric_value.get('risk_decision') else None
        record = self._record(
            symbol, now, "REJECTED", reason, data, quote_age,
            risk_reward, metric_value, risk_value,
        )
        return PreExecutionResult(
            False, reason, None, data, quote_age, risk_reward,
            risk_value, metric_value, record, geometry, decision,
        )

    @staticmethod
    def _record(
        symbol: str, now: datetime, status: str, reason: str | None,
        market_data: Mapping[str, Any], quote_age: float | None,
        risk_reward: float | None, metrics: Mapping[str, Any],
        risk_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        validation = metrics.get("technical_validation")
        quote_fresh = (
            quote_age is not None
            and 0 <= quote_age <= config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS
        )
        hard_gates = [{
            "rule_name": "PRE_EXECUTION_QUOTE_FRESHNESS",
            "actual": quote_age,
            "required": (
                "exchange timestamp age between 0 and "
                f"{config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS}s"
            ),
            "status": "PASS" if quote_fresh else "FAIL",
            "type": "HARD",
        }]
        if isinstance(validation, Mapping):
            hard_gates.extend(list(validation.get("rules", [])))
        required_rule = next(
            (
                item for item in hard_gates
                if isinstance(item, Mapping)
                and item.get("rule_name") == "REQUIRED_FIELDS_PRESENT"
            ),
            None,
        )
        missing_required = (
            list(required_rule.get("actual", []))
            if isinstance(required_rule, Mapping)
            and isinstance(required_rule.get("actual"), list)
            else []
        )
        context_at = parse_timestamp(market_data.get("context_timestamp"))
        context_age = (
            (now - context_at).total_seconds() if context_at is not None else None
        )
        return {
            "event": "PRE_EXECUTION_REFRESH",
            'episode_id': market_data.get('episode_id'),
            'research_cycle_id': market_data.get('research_cycle_id'),
            'geometry_decision': metrics.get('geometry_decision'),
            'risk_decision': metrics.get('risk_decision'),
            "timestamp": now.isoformat(),
            "symbol": symbol,
            "status": status,
            'geometry_diagnostics': metrics.get('geometry_diagnostics'),
            'geometry_timestamps': market_data.get('geometry_timestamps'),
            "refreshed_quote_as_of": market_data.get("quote_as_of"),
            "refreshed_quote_age_seconds": quote_age,
            "refreshed_bid": market_data.get("bid"),
            "refreshed_ask": market_data.get("ask"),
            "refreshed_spread_percent": metrics.get("spread_percent"),
            "refreshed_price": metrics.get("current_price"),
            "refreshed_vwap": metrics.get("vwap"),
            "refreshed_price_vs_vwap": metrics.get("price_vs_vwap"),
            "refreshed_entry": metrics.get("refreshed_entry"),
            "refreshed_stop_loss": metrics.get("refreshed_stop"),
            "refreshed_take_profit": metrics.get("refreshed_target"),
            "refreshed_risk_reward_ratio": (
                risk_reward if risk_reward is not None
                else metrics.get("refreshed_risk_reward_ratio")
            ),
            "refreshed_risk_distance": (
                metrics.get("refreshed_entry") - metrics.get("refreshed_stop")
                if isinstance(metrics.get("refreshed_entry"), (int, float))
                and isinstance(metrics.get("refreshed_stop"), (int, float))
                else None
            ),
            "refreshed_reward_distance": (
                metrics.get("refreshed_target") - metrics.get("refreshed_entry")
                if isinstance(metrics.get("refreshed_target"), (int, float))
                and isinstance(metrics.get("refreshed_entry"), (int, float))
                else None
            ),
            "minimum_required_risk_reward": config.MIN_RISK_REWARD_RATIO,
            "minimum_stop_distance_percent": 0.2,
            "refreshed_position_size": risk_result.get("max_shares"),
            "context_age_seconds": context_age,
            "required_fields_status": (
                str(required_rule.get("status"))
                if isinstance(required_rule, Mapping) else "NOT_EVALUATED"
            ),
            "missing_required_fields": missing_required,
            "hard_gate_summary": {
                str(item.get("rule_name")): str(item.get("status"))
                for item in hard_gates
                if isinstance(item, Mapping)
                and str(item.get("type")) == "HARD"
            },
            "hard_gate_results": hard_gates,
            "rejection_reason": reason,
        }
