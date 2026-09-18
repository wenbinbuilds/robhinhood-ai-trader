"""Scalp entry orchestration over shared canonical portfolio/risk/execution."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from uuid import uuid5, NAMESPACE_URL

import config
from execution.models import TradePlan
from risk.risk_manager import RiskLimits, RiskManager, RiskRequest
from shadow.execution import ShadowExecutionEngine
from strategies.scalp.events import ScalpEventJournal
from strategies.scalp.setup import ScalpSetupController
from strategies.scalp.signals import ScalpSignalEngine, completed_micro_bars


def scalp_risk_manager():
    return RiskManager(RiskLimits(
        max_position_percent=Decimal(str(min(config.MAX_POSITION_PERCENT, config.SCALP_MAX_POSITION_PERCENT))),
        max_risk_per_trade_percent=Decimal(str(min(config.MAX_RISK_PER_TRADE_PERCENT, config.SCALP_MAX_RISK_PER_TRADE_PERCENT))),
        max_daily_loss_percent=Decimal(str(min(config.MAX_DAILY_LOSS_PERCENT, config.SCALP_MAX_DAILY_LOSS_PERCENT))),
        max_simultaneous_positions=config.MAX_SIMULTANEOUS_POSITIONS,
        max_trades_per_day=max(config.MAX_TRADES_PER_DAY, config.SCALP_MAX_TRADES_PER_SESSION)))


class ScalpEntryController:
    def __init__(self, portfolio, *, setup_path=config.SCALP_STATE_PATH,
                 events_path=config.SCALP_EVENT_LOG_PATH, signal_engine=None):
        self.portfolio = portfolio
        self.signal_engine = signal_engine or ScalpSignalEngine()
        self.setup_controller = ScalpSetupController(setup_path)
        self.events = ScalpEventJournal(events_path)
        self.risk_manager = scalp_risk_manager()
        self.engine = ShadowExecutionEngine(portfolio, risk_manager=self.risk_manager)

    def process(self, symbol, quote, market_data: Mapping, *, now):
        if not config.SCALP_ENABLED:
            return None, {'symbol': symbol, 'reason': 'SCALP_DISABLED'}
        if config.MODE != 'SHADOW_TRADING' or config.SCALP_MODE != 'SHADOW':
            return None, {'symbol': symbol, 'reason': 'SCALP_SHADOW_ONLY'}
        bars = completed_micro_bars(market_data.get('candles', []) or [], now)
        features = self.signal_engine.features(symbol, quote, market_data, now=now)
        setup_type, evidence = self.signal_engine.classify(features, bars)
        evidence_at = bars[-1]['begins_at'] if bars else now.astimezone(timezone.utc).isoformat()
        episode = self.setup_controller.episode(
            symbol, setup_type, evidence, evidence_at, features, now=now)
        self.events.emit('ScalpCandidateDetected', timestamp=now, symbol=symbol,
                         episode_id=episode.episode_id, setup_type=setup_type)
        if episode.state == 'CLOSED':
            return None, {'symbol': symbol, 'reason': 'STALE_SCALP_EPISODE', 'episode_id': episode.episode_id}
        decision = self.signal_engine.evaluate(
            episode.episode_id, symbol, quote, market_data, now=now)
        guard_reasons = self._session_limits(symbol, now)
        reasons = tuple(dict.fromkeys([*decision.rejection_reasons, *guard_reasons]))
        if reasons:
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                             episode_id=episode.episode_id, reasons=list(reasons),
                             decision=decision.to_dict())
            return None, {'symbol': symbol, 'reason': reasons[0], 'reasons': list(reasons),
                          'decision': decision}
        self.events.emit('ScalpEntryReady', timestamp=now, symbol=symbol,
                         episode_id=episode.episode_id, decision=decision.to_dict())
        snapshot = self.portfolio.snapshot()
        allocated = sum(p.notional_value for p in snapshot.open_positions
                        if p.strategy in {'SCALP', config.SCALP_STRATEGY_ID})
        scalp_buying_power = max(0.0, min(snapshot.cash,
            snapshot.equity*config.SCALP_CAPITAL_ALLOCATION_PERCENT-allocated))
        risk = self.risk_manager.evaluate(RiskRequest(
            account_equity=snapshot.equity, entry_price=decision.entry_price,
            stop_price=decision.stop, daily_realized_pnl=snapshot.daily_pnl,
            open_positions=len(snapshot.open_positions), trades_today=self._session_trades(now).__len__(),
            available_buying_power=scalp_buying_power))
        if not risk.approved or risk.max_shares < 1:
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                episode_id=episode.episode_id, reasons=list(risk.reasons), gate='RISK')
            return None, {'symbol': symbol, 'reason': 'SCALP_RISK_REJECTED', 'details': list(risk.reasons)}
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
        position, detail = self.engine.open_trade_plan(plan, payload, now=now)
        if position is None:
            self.events.emit('ScalpEntryBlocked', timestamp=now, symbol=symbol,
                episode_id=episode.episode_id, reasons=[detail.get('reason')], gate='EXECUTION')
            return None, detail
        self.events.emit('ScalpPositionOpened', timestamp=now, symbol=symbol,
            episode_id=episode.episode_id, trade_id=position.trade_id,
            entry=position.entry_price, stop=position.stop, target=position.target)
        print(f"[SCALP {symbol} episode={episode.episode_id}] setup={decision.setup_type} "
              f"spread={(decision.features.spread_pct or 0)*100:.3f}% "
              f"expected_move={(decision.expected_move_pct or 0)*100:.3f}% "
              f"cost={(decision.estimated_cost_pct or 0)*100:.3f}% "
              f"net_edge={(decision.expected_net_edge_pct or 0)*100:.3f}% "
              f"entry={position.entry_price:.4f} stop={position.stop:.4f} "
              f"target={position.target:.4f} SHADOW_ENTRY", flush=True)
        return position, {'status': 'OPENED', 'strategy_id': 'SCALP',
                          'trade_id': position.trade_id, 'episode_id': episode.episode_id,
                          'decision': decision, **detail}

    def _session_trades(self, now):
        zone = ZoneInfo(config.MARKET_TIMEZONE)
        day = now.astimezone(zone).date()
        return [t for t in self.portfolio.snapshot().closed_positions
                if t.strategy in {'SCALP', config.SCALP_STRATEGY_ID}
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

    def on_quotes(self, quotes, data_lookup, *, now):
        results = []
        for symbol, quote in quotes.items():
            if self.portfolio.has_symbol(symbol): continue
            position, detail = self.process(symbol, quote, data_lookup(symbol) or {}, now=now)
            results.append(detail)
        return results
