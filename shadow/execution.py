"""Deterministic local fills and exits; this module has no broker methods."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import config
from agent.news_agent import NewsAgent
from execution.models import TradePlan, TradePlanError
from risk.risk_manager import RiskManager, RiskRequest
from portfolio.construction import PortfolioConstructor
from shadow.models import ShadowPosition, ShadowTrade
from shadow.portfolio import ShadowPortfolio, synchronized
from strategies.identity import (
    POSITION_INTERNAL_ID, SCALP_INTERNAL_ID, is_scalp_strategy,
    strategy_display_name,
)


def _number(value: Any) -> float | None:
    try:
        result = None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    return result if result is not None and result == result else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


class ShadowExecutionEngine:
    def __init__(
        self,
        portfolio: ShadowPortfolio,
        risk_manager: RiskManager | None = None,
        news_agent: NewsAgent | None = None,
        portfolio_constructor: PortfolioConstructor | None = None,
    ) -> None:
        if config.SHADOW_INTRABAR_BOTH_HIT_POLICY != "STOP_FIRST":
            raise ValueError("unsupported shadow intrabar policy; failing closed")
        self.portfolio = portfolio
        self.risk_manager = risk_manager or RiskManager()
        self.news_agent = news_agent or NewsAgent()
        self.portfolio_constructor = portfolio_constructor or PortfolioConstructor()

    def _symbol_conflict(self, symbol: str, incoming_strategy: str) -> dict[str, Any] | None:
        existing = next(
            (item for item in self.portfolio.state.open_positions
             if item.symbol == symbol.upper()),
            None,
        )
        if existing is None:
            return None
        existing_name = strategy_display_name(
            existing.strategy_id or existing.strategy
        )
        incoming_name = strategy_display_name(incoming_strategy)
        cross_strategy = existing_name != incoming_name
        return {
            "symbol": symbol.upper(),
            "reason": (
                "EXISTING_POSITION_OTHER_STRATEGY"
                if cross_strategy else "DUPLICATE_POSITION"
            ),
            "blocker": "BLOCKED_BY_EXISTING_POSITION",
            "existing_strategy": existing_name,
            "requested_strategy": incoming_name,
        }

    @staticmethod
    def market_closing(now: datetime) -> bool:
        eastern = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
        cutoff_minutes = 16 * 60 - config.FORCE_EXIT_MINUTES_BEFORE_CLOSE
        current_minutes = eastern.hour * 60 + eastern.minute
        return eastern.weekday() < 5 and current_minutes >= cutoff_minutes

    @synchronized
    def open_candidate(
        self,
        coordinator: Mapping[str, Any],
        candidate_data: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[ShadowPosition | None, dict[str, Any]]:
        symbol = str(coordinator.get("symbol", "")).upper()
        if coordinator.get("decision") != "TRADE_CANDIDATE":
            return None, {"symbol": symbol, "reason": "NOT_TRADE_CANDIDATE"}
        if self.market_closing(now):
            return None, {"symbol": symbol, "reason": "MARKET_CLOSING"}
        conflict = self._symbol_conflict(symbol, POSITION_INTERNAL_ID)
        if conflict:
            return None, conflict

        entry = _number(coordinator.get("entry"))
        stop = _number(coordinator.get("stop"))
        target = _number(coordinator.get("target"))
        quote_time = _timestamp(candidate_data.get("quote_as_of"))
        quote_age = (
            (now.astimezone(timezone.utc) - quote_time).total_seconds()
            if quote_time is not None
            else None
        )
        if quote_age is None or quote_age < -30 or quote_age > config.MAX_QUOTE_AGE_SECONDS:
            return None, {"symbol": symbol, "reason": "STALE_DATA"}
        if entry is None or entry <= 0:
            return None, {"symbol": symbol, "reason": "INVALID_ENTRY"}
        if stop is None or stop <= 0 or stop >= entry:
            return None, {"symbol": symbol, "reason": "INVALID_STOP"}
        if target is None or target <= entry:
            return None, {"symbol": symbol, "reason": "INVALID_TARGET"}

        result = self.risk_manager.evaluate(
            RiskRequest(
                account_equity=self.portfolio.state.equity,
                entry_price=entry,
                stop_price=stop,
                daily_realized_pnl=self.portfolio.state.daily_pnl,
                open_positions=len(self.portfolio.state.open_positions),
                trades_today=self.portfolio.state.trades_today,
                available_buying_power=self.portfolio.state.cash,
            )
        )
        if not result.approved or result.max_shares <= 0:
            reason_text = " ".join(result.reasons).lower()
            code = (
                "MAX_POSITIONS" if "simultaneous" in reason_text
                else "MAX_TRADES" if "trades per day" in reason_text
                else "DAILY_LOSS_LIMIT" if "daily loss" in reason_text
                else "RISK_REJECTED"
            )
            return None, {"symbol": symbol, "reason": code, "details": list(result.reasons)}
        try:
            plan = self.portfolio_constructor.construct(
                coordinator, result.to_dict(), candidate_data, now=now
            )
        except TradePlanError as exc:
            return None, {"symbol": symbol, "reason": "INVALID_TRADE_PLAN", "details": [str(exc)]}
        return self.open_trade_plan(plan, candidate_data, now=now)

    @synchronized
    def open_trade_plan(
        self,
        plan: TradePlan,
        candidate_data: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[ShadowPosition | None, dict[str, Any]]:
        """Apply local fill mechanics without changing the risk-sized quantity."""

        symbol = plan.symbol
        if any(p.episode_id == plan.episode_id or p.trade_id == plan.trade_id
               for p in [*self.portfolio.state.open_positions, *self.portfolio.state.closed_positions]):
            return None, {'symbol': symbol, 'reason': 'EPISODE_ALREADY_EXECUTED',
                          'pre_execution_passed': False,
                          'portfolio_attempted': False}
        if self.market_closing(now):
            return None, {"symbol": symbol, "reason": "MARKET_CLOSING",
                          'pre_execution_passed': False,
                          'portfolio_attempted': False}
        conflict = self._symbol_conflict(symbol, plan.strategy)
        if conflict:
            return None, {**conflict, 'pre_execution_passed': False,
                          'portfolio_attempted': False}
        quote_time = _timestamp(candidate_data.get("quote_as_of"))
        quote_age = (
            (now.astimezone(timezone.utc) - quote_time).total_seconds()
            if quote_time is not None else None
        )
        if quote_age is None or quote_age < -30 or quote_age > config.MAX_QUOTE_AGE_SECONDS:
            return None, {"symbol": symbol, "reason": "STALE_DATA",
                          'pre_execution_passed': False,
                          'portfolio_attempted': False}
        ask = _number(candidate_data.get("ask"))
        reference_fill = (
            max(plan.entry_price, ask) if ask is not None and ask > 0
            else plan.entry_price
        )
        is_scalp = is_scalp_strategy(plan.strategy)
        entry_slippage_bps = config.SCALP_ENTRY_SLIPPAGE_BPS if is_scalp else config.SHADOW_ENTRY_SLIPPAGE_BPS
        fill = reference_fill * (1 + entry_slippage_bps / 10_000)
        risk_per_share = fill - plan.stop_price
        risk_reward = (
            (plan.target_price - fill) / risk_per_share if risk_per_share > 0 else 0
        )
        minimum_rr = config.SCALP_MIN_RISK_REWARD if is_scalp else config.MIN_RISK_REWARD_RATIO
        if risk_reward < minimum_rr:
            return None, {"symbol": symbol, "reason": "LOW_RISK_REWARD",
                          'pre_execution_passed': False,
                          'portfolio_attempted': False,
                          'fill_risk_reward_ratio': risk_reward}
        from trading_runtime.portfolio_controller import PortfolioController
        from trading_runtime.journal import RuntimeEvent, RuntimeEventType
        from dataclasses import asdict
        decision, result = PortfolioController(self.portfolio, self.risk_manager).evaluate(plan, fill)
        self.portfolio.journal.append(RuntimeEvent(
            RuntimeEventType.PORTFOLIO_APPROVED if decision.approved else RuntimeEventType.PORTFOLIO_REJECTED,
            now.isoformat(), symbol, plan.episode_id, plan.research_cycle_id, asdict(decision)))
        if not decision.approved:
            reason_text = " ".join(result.reasons).lower()
            code = (
                "MAX_POSITIONS" if "simultaneous" in reason_text
                else "MAX_TRADES" if "trades per day" in reason_text
                else "DAILY_LOSS_LIMIT" if "daily loss" in reason_text
                else "RISK_REJECTED"
            )
            return None, {"symbol": symbol, "reason": code,
                          "details": list(result.reasons),
                          'pre_execution_passed': True,
                          'portfolio_attempted': True,
                          'portfolio_approved': False,
                          'portfolio_reasons': list(decision.reasons)}

        from rl.actions import EntryAction
        from rl.safety import SafetyOverride
        from trading_runtime.contracts import MarketSnapshot
        safety = SafetyOverride.evaluate(
            EntryAction.ENTER, target='SHADOW', risk=result, portfolio=decision,
            market_snapshot=MarketSnapshot.from_mapping(
                candidate_data, now=now, max_age=config.PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS))
        if not safety.approved:
            return None, {"symbol": symbol, "reason": "SAFETY_OVERRIDE:" + ','.join(safety.reasons),
                          'pre_execution_passed': True,
                          'portfolio_attempted': True,
                          'portfolio_approved': True,
                          'safety_approved': False,
                          'safety_reasons': list(safety.reasons)}

        news = plan.news_context
        sector = plan.sector_context
        market = plan.market_context
        technical = plan.technical_context
        event_ids = [
            str(item.get("event_id"))
            for item in news.get("event_clusters", [])
            if isinstance(item, Mapping) and item.get("event_id")
        ] if isinstance(news, Mapping) else []
        position = ShadowPosition(
            episode_id=plan.episode_id, research_cycle_id=plan.research_cycle_id,
            entry_intent_id=plan.entry_intent_id,
            trade_id=plan.trade_id, symbol=symbol, direction="LONG",
            strategy=plan.strategy, entry_price=round(fill, 4),
            requested_entry_price=plan.entry_price,
            entry_timestamp=now.astimezone(timezone.utc).isoformat(),
            quantity=plan.quantity, notional_value=round(fill * plan.quantity, 4),
            stop=plan.stop_price, target=plan.target_price,
            risk_per_share=round(risk_per_share, 4),
            maximum_theoretical_loss=round(risk_per_share * plan.quantity, 4),
            risk_reward_ratio=round(risk_reward, 3),
            coordinator_confidence=plan.coordinator_confidence,
            coordinator_score=plan.coordinator_score,
            technical_score=plan.technical_score,
            news_score=plan.news_score,
            sector_score=plan.sector_score,
            market_score=plan.market_score,
            catalyst_type=str(news.get("catalyst_type", "NONE")) if isinstance(news, Mapping) else "NONE",
            sector=str(sector.get("sector", "GENERAL")) if isinstance(sector, Mapping) else "GENERAL",
            market_regime=str(market.get("regime", "UNKNOWN")) if isinstance(market, Mapping) else "UNKNOWN",
            news_event_ids_at_entry=event_ids, last_price=round(fill, 4),
            estimated_entry_slippage_cost=round((fill - reference_fill) * plan.quantity, 4),
            strategy_id=SCALP_INTERNAL_ID if is_scalp else POSITION_INTERNAL_ID,
            strategy_display_name='SCALP' if is_scalp else 'POSITION',
            entry_reason=(
                str(technical.get('scalp_decision', {}).get('setup_type') or plan.thesis)
                if is_scalp and isinstance(technical, Mapping) else plan.thesis
            ),
            risk_allocated=round(risk_per_share * plan.quantity, 4),
            capital_allocated=round(fill * plan.quantity, 4),
            quoted_entry_bid=_number(candidate_data.get('bid')),
            quoted_entry_ask=ask,
            estimated_spread_cost=round(
                max(0, (ask-(_number(candidate_data.get('bid')) or ask))) * plan.quantity
                if ask else 0, 4),
            thesis=plan.thesis or None,
            invalidation_condition=plan.invalidation_condition or None,
            technical_context=dict(technical) if isinstance(technical, Mapping) else {},
            news_context=dict(news) if isinstance(news, Mapping) else {},
            sector_context=dict(sector) if isinstance(sector, Mapping) else {},
            market_context=dict(market) if isinstance(market, Mapping) else {},
            scalp_lifecycle_state='ACTIVE' if is_scalp else None,
            scalp_lifecycle_updated_at=(now.astimezone(timezone.utc).isoformat()
                                         if is_scalp else None),
            scalp_max_hold_seconds=(config.SCALP_MAX_HOLD_SECONDS if is_scalp else None),
            scalp_hold_seconds=(0.0 if is_scalp else None),
            scalp_time_remaining_seconds=(float(config.SCALP_MAX_HOLD_SECONDS)
                                          if is_scalp else None),
            scalp_exit_status=('MONITORING' if is_scalp else None),
            scalp_next_required_action=('MONITOR_POSITION' if is_scalp else None),
        )
        self.portfolio.add_position(position)
        return position, {"symbol": symbol, "status": "OPENED",
                          "risk": result.to_dict(),
                          'pre_execution_passed': True,
                          'portfolio_attempted': True,
                          'portfolio_approved': True,
                          'safety_approved': True,
                          'fill_risk_reward_ratio': risk_reward}

    def monitor_positions(
        self,
        data_lookup: Callable[[str], Mapping[str, Any]],
        *,
        now: datetime,
        market_context: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        # Collect inputs outside the shared-state lock. No provider or LLM waits
        # may block the fast watcher. Recheck position identity inside the lock.
        payloads = {
            position.symbol: data_lookup(position.symbol)
            for position in self.portfolio.snapshot().open_positions
        }
        return self._monitor_positions(payloads.get, now=now, market_context=market_context)

    @synchronized
    def _monitor_positions(
        self, data_lookup, *, now, market_context=None,
    ):
        evaluated: list[dict[str, Any]] = []
        exited: list[dict[str, Any]] = []
        warnings: list[str] = []
        marks: dict[str, float] = {}
        for position in list(self.portfolio.state.open_positions):
            payload = data_lookup(position.symbol) or {}
            current = _number(payload.get("current_price"))
            quote_time = _timestamp(payload.get("quote_as_of"))
            latest = _timestamp(position.last_price_timestamp)
            fast_owns = (
                position.quote_mode == "REALTIME_FAST" and latest is not None
                and 0 <= (now - latest).total_seconds() <= config.FAST_QUOTE_MAX_AGE_SECONDS
            )
            if latest is not None and (quote_time is None or quote_time < latest):
                evaluated.append({"symbol": position.symbol, "status": "OPEN", "reason": "NEWER_MARK_PRESERVED"})
                continue
            quote_age = (
                (now.astimezone(timezone.utc) - quote_time).total_seconds()
                if quote_time is not None
                else None
            )
            if current is None or quote_age is None or quote_age < -30 or quote_age > config.MAX_QUOTE_AGE_SECONDS:
                warnings.append(f"{position.symbol}: fresh shadow-position price unavailable")
                evaluated.append({"symbol": position.symbol, "status": "OPEN", "reason": "STALE_DATA"})
                continue
            entry_at = _timestamp(position.entry_timestamp)
            carried = entry_at is not None and entry_at.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date() < now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date()
            if carried or self.market_closing(now):
                position.quote_source = 'PERIODIC_ROBINHOOD_SNAPSHOT'
                position.last_price_timestamp = quote_time.isoformat()
                # An overdue intraday position exits at the available fresh
                # mark, before retrospective bars can relabel it as a winner.
                trade = self.close_at_price(
                    position.trade_id, current, 'MISSED_EOD_RECOVERY_EXIT' if carried else 'END_OF_DAY_EXIT', now=now,
                    exit_method='SLOW_SNAPSHOT_EXIT',
                    warnings=['MISSED_EOD_MONITORING_WINDOW: recovered at first fresh mark'] if carried else [],
                )
                evaluated.append({'symbol': position.symbol, 'status': 'MISSED_EOD_RECOVERY_EXIT' if carried else 'END_OF_DAY_EXIT'})
                if trade is not None:
                    exited.append(trade.to_dict())
                continue
            if not fast_owns:
                marks[position.symbol] = current
                position.last_price_timestamp = quote_time.isoformat()
                position.current_bid = _number(payload.get("bid"))
                position.current_ask = _number(payload.get("ask"))
                position.quote_mode = "DEGRADED_SNAPSHOT"
                position.quote_source = "PERIODIC_ROBINHOOD_SNAPSHOT"
                position.mark_price_method = "SNAPSHOT_CURRENT_PRICE"
                position.monitoring_status = "PRICE_MONITORING_DEGRADED"
            exit_reason: str | None = None
            base_exit: float | None = None
            entry_time = _timestamp(position.entry_timestamp)
            candles = payload.get("candles", [])
            eligible: list[tuple[datetime, Mapping[str, Any]]] = []
            if entry_time is not None and isinstance(candles, list):
                for candle in candles:
                    if not isinstance(candle, Mapping) or candle.get("interpolated"):
                        continue
                    begins = _timestamp(candle.get("begins_at"))
                    if begins is not None and entry_time < begins <= now.astimezone(timezone.utc):
                        eligible.append((begins, candle))
            reconstructed = False
            for _, candle in ([] if fast_owns else sorted(eligible, key=lambda item: item[0])):
                low = _number(candle.get("low"))
                high = _number(candle.get("high"))
                if low is None or high is None:
                    continue
                stop_hit = low <= position.stop
                target_hit = high >= position.target
                reconstructed = stop_hit or target_hit
                if stop_hit and target_hit:
                    exit_reason, base_exit = "STOP_HIT", min(position.stop, _number(candle.get("open")) or position.stop, current)
                    break
                if stop_hit:
                    exit_reason, base_exit = "STOP_HIT", min(position.stop, _number(candle.get("open")) or position.stop, current)
                    break
                if target_hit:
                    exit_reason, base_exit = "TARGET_HIT", position.target
                    break

            raw_news = payload.get("news_items", [])
            new_material_news: list[str] = []
            news_changed_thesis = False
            exit_warnings: list[str] = []
            technical_invalidation = False
            if exit_reason is None:
                vwap = _number(payload.get("vwap"))
                ema9 = _number(payload.get("ema9"))
                ema20 = _number(payload.get("ema20"))
                technical_invalidation = (
                    vwap is not None
                    and ema9 is not None
                    and ema20 is not None
                    and current < vwap
                    and ema9 < ema20
                )
                if technical_invalidation:
                    exit_reason, base_exit = "INVALIDATED", current
                    exit_warnings.append(
                        "momentum invalidated: price below VWAP and EMA 9 below EMA 20"
                    )
            if exit_reason is None and isinstance(raw_news, list):
                context = self.news_agent.analyze(
                    position.symbol,
                    [item for item in raw_news if isinstance(item, Mapping)],
                    now=now,
                )
                new_material = [
                    item for item in context.event_clusters
                    if item.event_id not in position.news_event_ids_at_entry and item.importance >= 0.7
                ]
                new_material_news = [item.event_id for item in new_material]
                if any(item.sentiment in {"NEGATIVE", "VERY_NEGATIVE"} for item in new_material):
                    exit_reason, base_exit = "INVALIDATED", current
                    news_changed_thesis = True
                    exit_warnings.append("new material negative news changed the thesis")
                position.news_event_ids_at_entry.extend(
                    item.event_id for item in new_material
                    if item.event_id not in position.news_event_ids_at_entry
                )

            entry_eastern = (
                entry_time.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
                if entry_time is not None else None
            )
            now_eastern = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
            carried_past_entry_day = (
                entry_eastern is not None and now_eastern.date() > entry_eastern.date()
            )
            if exit_reason is None and (self.market_closing(now) or carried_past_entry_day):
                exit_reason, base_exit = "END_OF_DAY_EXIT", current
                if carried_past_entry_day:
                    exit_warnings.append(
                        "position was recovered after its entry session and exited at the first fresh mark"
                    )
            if exit_reason is None:
                if not fast_owns:
                    position.last_price = current
                    position.unrealized_pnl = round((current - position.entry_price) * position.quantity, 4)
                evaluated.append({
                    "symbol": position.symbol,
                    "status": "OPEN",
                    "current_price": current,
                    "technical_invalidation": technical_invalidation,
                    "new_material_news": new_material_news,
                    "news_changed_thesis": news_changed_thesis,
                    "market_context": dict(market_context or {}),
                })
                continue

            assert base_exit is not None
            trade = self.close_at_price(
                position.trade_id, base_exit, exit_reason, now=now,
                exit_method="RECONSTRUCTED_FROM_BAR_DATA" if reconstructed else "SLOW_SNAPSHOT_EXIT",
                warnings=(exit_warnings + ([
                    "stop assumed first because one bar touched stop and target"
                ] if any(
                    _number(c.get("low")) is not None and _number(c.get("high")) is not None
                    and _number(c.get("low")) <= position.stop and _number(c.get("high")) >= position.target
                    for _, c in eligible
                ) and exit_reason == "STOP_HIT" else [])),
            )
            evaluated.append({
                "symbol": position.symbol,
                "status": exit_reason,
                "technical_invalidation": technical_invalidation,
                "new_material_news": new_material_news,
                "news_changed_thesis": news_changed_thesis,
                "market_context": dict(market_context or {}),
            })
            if trade is not None:
                exited.append(trade.to_dict())
        self.portfolio.revalue(marks)
        return evaluated, exited, warnings

    @synchronized
    def close_at_price(self, trade_id, base_exit, reason, *, now, exit_method, warnings=()):
        """Shared, idempotent LOCAL fill calculation; no broker integration."""
        position = next((p for p in self.portfolio.state.open_positions if p.trade_id == trade_id), None)
        if position is None:
            return None
        is_scalp = is_scalp_strategy(position.strategy_id or position.strategy)
        exit_slippage_bps = config.SCALP_EXIT_SLIPPAGE_BPS if is_scalp else config.SHADOW_EXIT_SLIPPAGE_BPS
        fill = round(base_exit * (1 - exit_slippage_bps / 10_000), 4)
        net = (fill - position.entry_price) * position.quantity
        holding_seconds = max(0, (now - (_timestamp(position.entry_timestamp) or now)).total_seconds())
        exit_slippage_cost = (base_exit-fill)*position.quantity
        trade = ShadowTrade(
            episode_id=position.episode_id, research_cycle_id=position.research_cycle_id,
            entry_intent_id=position.entry_intent_id,
            price_source=position.quote_source, quote_timestamp=position.last_price_timestamp,
            trade_id=position.trade_id, symbol=position.symbol, strategy=position.strategy,
            sector=position.sector, entry_timestamp=position.entry_timestamp,
            entry_price=position.entry_price, quantity=position.quantity,
            stop=position.stop, target=position.target, risk_reward_ratio=position.risk_reward_ratio,
            exit_timestamp=now.astimezone(timezone.utc).isoformat(), exit_price=fill, exit_reason=reason,
            gross_pnl=round((base_exit - position.requested_entry_price) * position.quantity, 4),
            estimated_slippage_cost=round(position.estimated_entry_slippage_cost + (base_exit - fill) * position.quantity, 4),
            net_pnl=round(net, 4),
            return_percent=round(net / position.notional_value * 100, 4) if position.notional_value else 0,
            holding_time_minutes=round(holding_seconds / 60, 2),
            coordinator_confidence=position.coordinator_confidence, coordinator_score=position.coordinator_score,
            technical_score=position.technical_score, news_score=position.news_score,
            sector_score=position.sector_score, market_score=position.market_score,
            catalyst_type=position.catalyst_type, market_regime=position.market_regime,
            warnings=list(warnings), exit_method=exit_method,
            slow_context_score=position.slow_context_score,
            live_market_score=position.live_market_score,
            dynamic_score=position.dynamic_score,
            slow_weight=position.slow_weight,
            live_weight=position.live_weight,
            context_age_seconds=position.context_age_seconds,
            qualitative_score=position.qualitative_score,
            llm_score=position.llm_score,
            discovery_timestamp=position.discovery_timestamp,
            watchlist_timestamp=position.watchlist_timestamp,
            trade_ready_timestamp=position.trade_ready_timestamp,
            state_transition_history=list(position.state_transition_history) + [{
                "timestamp": now.astimezone(timezone.utc).isoformat(),
                "previous_state": "EXIT_PENDING",
                "new_state": "CLOSED",
                "reason": reason,
            }],
            strategy_id=position.strategy_id,
            strategy_display_name=position.strategy_display_name,
            entry_reason=position.entry_reason,
            risk_allocated=position.risk_allocated,
            capital_allocated=position.capital_allocated,
            quoted_entry_bid=position.quoted_entry_bid,
            quoted_entry_ask=position.quoted_entry_ask,
            quoted_exit_bid=position.current_bid,
            quoted_exit_ask=position.current_ask,
            estimated_spread_cost=position.estimated_spread_cost,
            entry_slippage_cost=position.estimated_entry_slippage_cost,
            exit_slippage_cost=round(exit_slippage_cost, 4),
            holding_time_seconds=round(holding_seconds, 3),
            maximum_favorable_excursion=position.maximum_favorable_excursion,
            maximum_adverse_excursion=position.maximum_adverse_excursion,
            configured_max_hold_seconds=(position.scalp_max_hold_seconds
                                         if is_scalp else None),
            crossed_max_hold_at=position.scalp_overdue_since if is_scalp else None,
            exit_decision_at=(now.astimezone(timezone.utc).isoformat()
                              if is_scalp and position.scalp_overdue_since else None),
            max_hold_decision_delay_seconds=(
                round(max(0.0, holding_seconds - float(
                    position.scalp_max_hold_seconds or config.SCALP_MAX_HOLD_SECONDS)), 3)
                if is_scalp and position.scalp_overdue_since else None
            ),
            max_hold_close_delay_seconds=(
                round(max(0.0, holding_seconds - float(
                    position.scalp_max_hold_seconds or config.SCALP_MAX_HOLD_SECONDS)), 3)
                if is_scalp and position.scalp_overdue_since else None
            ),
            recovery_exit=reason == 'SCALP_RECOVERY_TIME_EXIT',
            max_hold_delay_reasons=list(position.scalp_max_hold_delay_reasons),
        )
        self.portfolio.close_position(trade_id, trade)
        return trade
