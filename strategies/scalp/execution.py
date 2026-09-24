"""Scalp entry orchestration over shared canonical portfolio/risk/execution."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from typing import Mapping
from uuid import uuid5, NAMESPACE_URL

import config
from execution.models import TradePlan
from risk.risk_manager import RiskLimits, RiskManager, RiskRequest
from shadow.execution import ShadowExecutionEngine
from execution.execution_guard import read_kill_switch
from strategies.scalp.events import ScalpEventJournal
from strategies.scalp.setup import ScalpSetupController
from strategies.scalp.signals import ScalpSignalEngine, completed_micro_bars
from strategies.identity import is_scalp_strategy, strategy_display_name


EPISODE_PRECONDITION_REASONS = frozenset({
    'QUOTE_UNAVAILABLE', 'STALE_QUOTE', 'MARKET_CLOSED', 'INVALID_BID_ASK',
    'SPREAD_TOO_WIDE', 'INSUFFICIENT_COMPLETED_MICRO_BARS',
    'MICRO_BARS_STALE', 'MICRO_BARS_UNAVAILABLE', 'MICRO_BAR_REFRESH_FAILED',
    'VOLUME_EXPANSION_BELOW_MINIMUM', 'VOLUME_DATA_STALE',
    'VOLUME_DATA_UNAVAILABLE', 'UNCLASSIFIED_SETUP',
})


def scalp_risk_manager():
    return RiskManager(RiskLimits(
        max_position_percent=Decimal(str(min(config.MAX_POSITION_PERCENT, config.SCALP_MAX_POSITION_PERCENT))),
        max_risk_per_trade_percent=Decimal(str(min(config.MAX_RISK_PER_TRADE_PERCENT, config.SCALP_MAX_RISK_PER_TRADE_PERCENT))),
        max_daily_loss_percent=Decimal(str(min(config.MAX_DAILY_LOSS_PERCENT, config.SCALP_MAX_DAILY_LOSS_PERCENT))),
        max_simultaneous_positions=config.MAX_SIMULTANEOUS_POSITIONS,
        max_trades_per_day=max(config.MAX_TRADES_PER_DAY, config.SCALP_MAX_TRADES_PER_SESSION)))


class ScalpEntryController:
    def __init__(self, portfolio, *, setup_path=config.SCALP_STATE_PATH,
                 events_path=config.SCALP_EVENT_LOG_PATH, signal_engine=None,
                 enabled=None, kill_switch_path=None, clock=None):
        self.portfolio = portfolio
        self.signal_engine = signal_engine or ScalpSignalEngine()
        snapshot = portfolio.snapshot()
        executed = {
            item.episode_id for item in [
                *snapshot.open_positions, *snapshot.closed_positions,
            ] if is_scalp_strategy(item.strategy_id or item.strategy)
        }
        self.setup_controller = ScalpSetupController(
            setup_path, executed_episode_ids=executed,
        )
        self.events = ScalpEventJournal(events_path)
        self.risk_manager = scalp_risk_manager()
        self.engine = ShadowExecutionEngine(portfolio, risk_manager=self.risk_manager)
        self.enabled = config.SCALP_ENABLED if enabled is None else bool(enabled)
        self.kill_switch_path = Path(kill_switch_path or config.LIVE_KILL_SWITCH_PATH)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _latency_template(self, quote, cycle_timing=None):
        timing = dict(cycle_timing or {})
        timing.update({
            'exchange_timestamp': (
                quote.timestamp.astimezone(timezone.utc).isoformat()
                if quote is not None and quote.timestamp.tzinfo else None
            ),
            'provider_request_start': (
                quote.request_started_at.astimezone(timezone.utc).isoformat()
                if quote is not None and quote.request_started_at else None
            ),
            'provider_response_received': (
                (quote.received_at or quote.request_finished_at).astimezone(
                    timezone.utc
                ).isoformat()
                if quote is not None and (quote.received_at or quote.request_finished_at)
                else None
            ),
            'quote_ingested_at': timing.get('quote_ingested_at'),
            'scalp_cycle_start': timing.get('scalp_cycle_start'),
            'feature_compute_start': None,
            'feature_compute_end': None,
            'feature_compute_duration_ms': None,
            'setup_compute_start': None,
            'setup_compute_end': None,
            'setup_compute_duration_ms': None,
            'geometry_compute_start': None,
            'geometry_compute_end': None,
            'geometry_compute_duration_ms': None,
            'setup_first_detected_at': None,
            'episode_created_at': None,
            'first_eligible_at': None,
            'first_score_060_at': None,
            'first_score_065_at': None,
            'first_score_068_at': None,
            'first_score_070_at': None,
            'first_score_075_at': None,
            'first_extension_failure_at': None,
            'first_edge_pass_at': None,
            'first_rr_pass_at': None,
            'risk_start': None,
            'risk_end': None,
            'risk_duration_ms': None,
            'preexecution_start': None,
            'preexecution_end': None,
            'preexecution_duration_ms': None,
            'shadow_entry_requested_at': None,
            'shadow_position_created_at': None,
            'shadow_execution_duration_ms': None,
        })
        return timing

    def _stamp(self):
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def process(self, symbol, quote, market_data: Mapping, *, now,
                cycle_timing=None):
        latency = self._latency_template(quote, cycle_timing)
        if not self.enabled:
            return None, {'symbol': symbol, 'reason': 'SCALP_DISABLED',
                          'latency': latency}
        if config.MODE != 'SHADOW_TRADING' or config.SCALP_MODE != 'SHADOW':
            return None, {'symbol': symbol, 'reason': 'SCALP_SHADOW_ONLY',
                          'latency': latency}
        if config.LIVE_TRADING_ENABLED is not False or config.ROBINHOOD_EXECUTION_ENABLED is not False:
            return None, {'symbol': symbol, 'reason': 'SCALP_SAFETY_FLAGS_INVALID',
                          'latency': latency}
        if not read_kill_switch(self.kill_switch_path).trading_blocked:
            return None, {'symbol': symbol, 'reason': 'SCALP_KILL_SWITCH_NOT_BLOCKED',
                          'latency': latency}
        existing = next((
            item for item in self.portfolio.snapshot().open_positions
            if item.symbol == symbol.upper()
        ), None)
        if existing is not None:
            reason = (
                'DUPLICATE_POSITION'
                if is_scalp_strategy(existing.strategy_id or existing.strategy)
                else 'EXISTING_POSITION_OTHER_STRATEGY'
            )
            return None, {'symbol': symbol, 'reason': reason,
                          'reasons': [reason],
                          'entry_attempted': False, 'latency': latency}
        bars = completed_micro_bars(market_data.get('candles', []) or [], now)
        feature_started = perf_counter_ns()
        latency['feature_compute_start'] = self._stamp()
        features = self.signal_engine.features(symbol, quote, market_data, now=now)
        latency['feature_compute_end'] = self._stamp()
        latency['feature_compute_duration_ms'] = (
            perf_counter_ns() - feature_started
        ) / 1_000_000
        setup_started = perf_counter_ns()
        latency['setup_compute_start'] = self._stamp()
        setup_type, evidence = self.signal_engine.classify(features, bars)
        latency['setup_compute_end'] = self._stamp()
        latency['setup_compute_duration_ms'] = (
            perf_counter_ns() - setup_started
        ) / 1_000_000
        if setup_type != 'UNCLASSIFIED':
            latency['setup_first_detected_at'] = latency['setup_compute_end']
        evidence_at = bars[-1]['begins_at'] if bars else now.astimezone(timezone.utc).isoformat()
        episode = self.setup_controller.episode(
            symbol, setup_type, evidence, evidence_at, features, now=now,
            bars=bars)
        latency['episode_created_at'] = episode.created_at
        self.events.emit('ScalpCandidateDetected', timestamp=now, symbol=symbol,
                         episode_id=episode.episode_id, setup_type=setup_type)
        geometry_started = perf_counter_ns()
        latency['geometry_compute_start'] = self._stamp()
        decision = self.signal_engine.evaluate(
            episode.episode_id, symbol, quote, market_data, now=now,
            features=features, bars=bars)
        latency['geometry_compute_end'] = self._stamp()
        latency['geometry_compute_duration_ms'] = (
            perf_counter_ns() - geometry_started
        ) / 1_000_000
        precondition_reasons = [
            reason for reason in decision.rejection_reasons
            if reason in EPISODE_PRECONDITION_REASONS
        ]
        milestone_stamp = latency['geometry_compute_end']
        if setup_type != 'UNCLASSIFIED' and not precondition_reasons:
            latency['first_eligible_at'] = milestone_stamp
        if decision.signal_score is not None:
            for threshold, name in (
                (.60, 'first_score_060_at'), (.65, 'first_score_065_at'),
                (.68, 'first_score_068_at'), (.70, 'first_score_070_at'),
                (.75, 'first_score_075_at'),
            ):
                if decision.signal_score >= threshold:
                    latency[name] = milestone_stamp
        if 'ENTRY_OVEREXTENDED' in decision.rejection_reasons:
            latency['first_extension_failure_at'] = milestone_stamp
        reached_edge = not set(decision.rejection_reasons).intersection({
            *EPISODE_PRECONDITION_REASONS, 'SIGNAL_SCORE_BELOW_THRESHOLD',
            'SIGNAL_DATA_STALE', 'SIGNAL_DATA_INVALID', 'ENTRY_OVEREXTENDED',
            'INVALID_STOP', 'INSUFFICIENT_NET_EDGE',
        })
        if reached_edge and decision.expected_net_edge_pct is not None:
            latency['first_edge_pass_at'] = milestone_stamp
        reached_rr = reached_edge and not set(decision.rejection_reasons).intersection({
            'INVALID_TARGET', 'SCALP_RR_BELOW_MINIMUM',
        })
        if reached_rr and decision.risk_reward_ratio is not None:
            latency['first_rr_pass_at'] = milestone_stamp
        persisted = self.setup_controller.record_latency_milestones(
            episode.episode_id, symbol, {
                name: latency.get(name) for name in (
                    'setup_first_detected_at', 'episode_created_at',
                    'first_eligible_at', 'first_score_060_at',
                    'first_score_065_at', 'first_score_068_at',
                    'first_score_070_at', 'first_score_075_at',
                    'first_extension_failure_at', 'first_edge_pass_at',
                    'first_rr_pass_at',
                )
            },
        )
        if persisted is not None:
            episode = persisted
            latency.update(episode.latency_milestones)
        episode_lifecycle = self._episode_lifecycle(episode, now)
        if episode.state in {'ENTERED', 'RESOLVED'} and not precondition_reasons:
            return None, {'symbol': symbol, 'reason': 'STALE_SCALP_EPISODE',
                          'episode_id': episode.episode_id,
                          'setup_type': setup_type,
                          'setup_evidence': list(evidence),
                          'decision': decision,
                          'episode_lifecycle': episode_lifecycle,
                          'entry_attempted': False, 'latency': latency}
        guard_reasons = self._session_limits(symbol, now)
        reasons = tuple(dict.fromkeys([*decision.rejection_reasons, *guard_reasons]))
        if reasons:
            if episode.state == 'READY':
                forming = self.setup_controller.transition(
                    episode.episode_id, symbol, 'FORMING', now=now,
                    reason='LATEST_OBSERVATION_NO_LONGER_ENTRY_READY',
                )
                if forming is not None:
                    episode_lifecycle = self._episode_lifecycle(forming, now)
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                             episode_id=episode.episode_id, reasons=list(reasons),
                             decision=decision.to_dict())
            return None, {'symbol': symbol, 'reason': reasons[0], 'reasons': list(reasons),
                          'decision': decision, 'episode_id': episode.episode_id,
                          'episode_lifecycle': episode_lifecycle,
                          'entry_attempted': False, 'risk_attempted': False,
                          'latency': latency}
        ready = self.setup_controller.transition(
            episode.episode_id, symbol, 'READY', now=now,
            reason='ALL_ENTRY_SIGNAL_GATES_PASSED',
        )
        if ready is not None:
            episode_lifecycle = self._episode_lifecycle(ready, now)
        self.events.emit('ScalpEntryReady', timestamp=now, symbol=symbol,
                         episode_id=episode.episode_id, decision=decision.to_dict())
        snapshot = self.portfolio.snapshot()
        allocated = sum(
            p.notional_value for p in snapshot.open_positions
            if is_scalp_strategy(p.strategy_id or p.strategy)
        )
        scalp_buying_power = max(0.0, min(snapshot.cash,
            snapshot.equity*config.SCALP_CAPITAL_ALLOCATION_PERCENT-allocated))
        risk_started = perf_counter_ns()
        latency['risk_start'] = self._stamp()
        risk = self.risk_manager.evaluate(RiskRequest(
            account_equity=snapshot.equity, entry_price=decision.entry_price,
            stop_price=decision.stop, daily_realized_pnl=snapshot.daily_pnl,
            open_positions=len(snapshot.open_positions), trades_today=self._session_trades(now).__len__(),
            available_buying_power=scalp_buying_power))
        latency['risk_end'] = self._stamp()
        latency['risk_duration_ms'] = (
            perf_counter_ns() - risk_started
        ) / 1_000_000
        if not risk.approved or risk.max_shares < 1:
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                episode_id=episode.episode_id, reasons=list(risk.reasons), gate='RISK')
            print(
                f"[SCALP RISK REJECTION] {symbol} episode={episode.episode_id} "
                f"reasons={','.join(risk.reasons) or 'MAX_SHARES_BELOW_ONE'}",
                flush=True,
            )
            return None, {'symbol': symbol, 'reason': 'SCALP_RISK_REJECTED',
                          'reasons': ['SCALP_RISK_REJECTED'],
                          'details': list(risk.reasons), 'decision': decision,
                          'episode_id': episode.episode_id,
                          'episode_lifecycle': episode_lifecycle,
                          'entry_attempted': True, 'risk_attempted': True,
                          'risk_approved': False, 'risk': risk.to_dict(),
                          'latency': latency}
        quantity = risk.max_shares
        plan = TradePlan(
            trade_id=str(uuid5(NAMESPACE_URL, 'shadow-entry:'+episode.episode_id)),
            episode_id=episode.episode_id, research_cycle_id=evidence_at,
            symbol=symbol, side='BUY', strategy=config.SCALP_STRATEGY_ID,
            decision_timestamp=now.astimezone(timezone.utc).isoformat(),
            decision_price=decision.entry_price, entry_type='MARKET',
            entry_price=decision.entry_price, quantity=quantity,
            notional=decision.entry_price*quantity, stop_price=decision.stop,
            target_price=decision.target, risk_per_share=decision.entry_price-decision.stop,
            maximum_expected_loss=(decision.entry_price-decision.stop)*quantity,
            risk_reward_ratio=decision.risk_reward_ratio, coordinator_score=decision.signal_score,
            technical_score=decision.signal_score, news_score=.5, sector_score=.5,
            market_score=.5, thesis=f'deterministic {decision.setup_type}',
            invalidation_condition='hard stop or deterministic scalp exit',
            market_data_timestamp=quote.timestamp.isoformat(),
            technical_context={'scalp_decision': decision.to_dict()},
            market_context={'regime': market_data.get('market_regime', 'UNKNOWN')})
        payload = {**dict(market_data), 'symbol': symbol, 'current_price': quote.mark_price,
                   'bid': quote.bid, 'ask': quote.ask, 'quote_as_of': quote.timestamp.isoformat(),
                   'provider': quote.source}
        preexecution_started = perf_counter_ns()
        latency['preexecution_start'] = self._stamp()
        position, detail = self.engine.open_trade_plan(plan, payload, now=now)
        latency['preexecution_end'] = self._stamp()
        latency['preexecution_duration_ms'] = (
            perf_counter_ns() - preexecution_started
        ) / 1_000_000
        latency.update(detail.get('execution_latency', {}))
        if position is None:
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                episode_id=episode.episode_id, reasons=[detail.get('reason')], gate='EXECUTION')
            print(
                f"[SCALP ENTRY REJECTED] {symbol} episode={episode.episode_id} "
                f"reason={detail.get('reason', 'EXECUTION_REJECTED')}",
                flush=True,
            )
            return None, {**detail, 'decision': decision,
                          'episode_id': episode.episode_id,
                          'episode_lifecycle': episode_lifecycle,
                          'entry_attempted': True, 'risk_attempted': True,
                          'risk_approved': True, 'risk': risk.to_dict(),
                          'pre_execution_attempted': True,
                          'latency': latency}
        self.events.emit('ScalpPositionOpened', timestamp=now, symbol=symbol,
            episode_id=episode.episode_id, trade_id=position.trade_id,
            entry=position.entry_price, stop=position.stop, target=position.target)
        entered = self.setup_controller.transition(
            episode.episode_id, symbol, 'ENTERED', now=now,
            reason='LOCAL_SHADOW_POSITION_OPENED',
        )
        if entered is not None:
            episode_lifecycle = self._episode_lifecycle(entered, now)
        print(f"[SCALP ENTRY] {symbol} episode={episode.episode_id} setup={decision.setup_type} "
              f"spread={(decision.features.spread_pct or 0)*100:.3f}% "
              f"expected_move={(decision.expected_move_pct or 0)*100:.3f}% "
              f"cost={(decision.estimated_cost_pct or 0)*100:.3f}% "
              f"net_edge={(decision.expected_net_edge_pct or 0)*100:.3f}% "
              f"entry={position.entry_price:.4f} stop={position.stop:.4f} "
              f"target={position.target:.4f} SHADOW_ENTRY", flush=True)
        return position, {'status': 'OPENED', 'strategy_id': position.strategy_id,
                          'strategy_display_name': 'SCALP',
                          'trade_id': position.trade_id, 'episode_id': episode.episode_id,
                          'decision': decision,
                          'episode_lifecycle': episode_lifecycle,
                          'entry_attempted': True,
                          'risk_attempted': True, 'risk_approved': True,
                          'pre_execution_attempted': True, **detail,
                          'latency': latency}

    @staticmethod
    def _episode_lifecycle(episode, now):
        try:
            created = datetime.fromisoformat(episode.created_at.replace('Z', '+00:00'))
            age = (now.astimezone(timezone.utc)-created.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, AttributeError):
            age = None
        return {
            'episode_status': episode.state,
            'episode_created_at': episode.created_at,
            'episode_last_updated_at': episode.last_updated_at,
            'episode_age_seconds': age,
            'stale_after_seconds': episode.stale_after_seconds,
            'initial_structural_fingerprint': episode.initial_structural_fingerprint,
            'current_structural_fingerprint': episode.structural_fingerprint,
            'structure_changed': episode.structure_changed,
            'new_episode_allowed': episode.new_episode_allowed,
            'reason_stale': episode.stale_reason,
            'episode_closed_at': episode.closed_at,
            'close_reason': episode.close_reason,
            'fingerprint_fields': dict(episode.fingerprint_fields),
            'original_fingerprint_fields': dict(episode.original_fingerprint_fields),
            'exact_block_reason': episode.exact_block_reason,
            'current_quote_price': episode.current_quote_price,
            'original_anchor_price': episode.original_anchor_price,
            'current_short_bar_timestamp': episode.current_bar_timestamp,
            'original_bar_timestamp': episode.original_bar_timestamp,
            'current_volume_expansion': episode.current_volume_expansion,
            'original_volume_expansion': episode.original_volume_expansion,
            'transition_history': list(episode.transition_history),
            'latency_milestones': dict(episode.latency_milestones),
        }

    def _session_trades(self, now):
        zone = ZoneInfo(config.MARKET_TIMEZONE)
        day = now.astimezone(zone).date()
        return [t for t in self.portfolio.snapshot().closed_positions
                if is_scalp_strategy(t.strategy_id or t.strategy)
                and datetime.fromisoformat(t.exit_timestamp.replace('Z','+00:00')).astimezone(zone).date() == day]

    def _session_limits(self, symbol, now):
        trades = self._session_trades(now)
        reasons = []
        if len(trades) >= config.SCALP_MAX_TRADES_PER_SESSION: reasons.append('SCALP_SESSION_TRADE_LIMIT')
        symbol_trades = [t for t in trades if t.symbol == symbol.upper()]
        if len(symbol_trades) >= config.SCALP_MAX_TRADES_PER_SYMBOL: reasons.append('SCALP_SYMBOL_TRADE_LIMIT')
        losses = 0
        for trade in reversed(trades):
            if trade.net_pnl < 0: losses += 1
            else: break
        if losses >= config.SCALP_MAX_CONSECUTIVE_LOSSES: reasons.append('SCALP_CONSECUTIVE_LOSS_LIMIT')
        loss = -sum(min(0, t.net_pnl) for t in trades)
        if loss >= self.portfolio.snapshot().starting_capital*config.SCALP_MAX_DAILY_LOSS_PERCENT:
            reasons.append('SCALP_DAILY_LOSS_LIMIT')
        costs = sum(getattr(t, 'estimated_slippage_cost', 0)
                    + getattr(t, 'estimated_spread_cost', 0) for t in trades)
        if costs >= self.portfolio.snapshot().starting_capital*config.SCALP_MAX_TRANSACTION_COST_PERCENT:
            reasons.append('SCALP_TRANSACTION_COST_BUDGET')
        return reasons

    def on_quotes(self, quotes, data_lookup, *, now, cycle_timing=None):
        results = []
        for symbol, quote in quotes.items():
            existing = next((
                item for item in self.portfolio.snapshot().open_positions
                if item.symbol == symbol.upper()
            ), None)
            if existing is not None:
                existing_strategy = strategy_display_name(
                    existing.strategy_id or existing.strategy
                )
                reason = (
                    'DUPLICATE_POSITION' if existing_strategy == 'SCALP'
                    else 'EXISTING_POSITION_OTHER_STRATEGY'
                )
                results.append({'symbol': symbol, 'reason': reason,
                                'reasons': [reason],
                                'blocker': 'BLOCKED_BY_EXISTING_POSITION',
                                'existing_strategy': existing_strategy,
                                'requested_strategy': 'SCALP',
                                'entry_attempted': False,
                                'portfolio_attempted': False,
                                'portfolio_approved': False,
                                'latency': self._latency_template(
                                    quote, cycle_timing,
                                )})
                continue
            position, detail = self.process(
                symbol, quote, data_lookup(symbol) or {}, now=now,
                cycle_timing=cycle_timing,
            )
            results.append(detail)
        return results
