"""Independent deterministic management for canonical open scalp positions."""
from datetime import datetime, timezone
from typing import Mapping

import config
from shadow.execution import ShadowExecutionEngine
from strategies.scalp.events import ScalpEventJournal
from strategies.scalp.setup import ScalpSetupController
from watcher.models import FastQuote, timestamp


class ScalpPositionController:
    def __init__(self, portfolio, *, setup_controller=None, events=None, engine=None):
        self.portfolio = portfolio
        self.setup_controller = setup_controller or ScalpSetupController(config.SCALP_STATE_PATH)
        self.events = events or ScalpEventJournal(config.SCALP_EVENT_LOG_PATH)
        self.engine = engine or ShadowExecutionEngine(portfolio)

    def process_quotes(self, quotes: Mapping[str, FastQuote], data_lookup=lambda _: {}, *, now):
        closed = []
        touched = False
        for position in list(self.portfolio.snapshot().open_positions):
            if position.strategy not in {'SCALP', config.SCALP_STRATEGY_ID}: continue
            quote = quotes.get(position.symbol)
            if quote is None or quote.timestamp.tzinfo is None: continue
            age = quote.age_at(now)
            if not 0 <= age <= config.SCALP_MAX_QUOTE_AGE_SECONDS or quote.exit_price is None: continue
            with self.portfolio.lock:
                canonical = next((p for p in self.portfolio.state.open_positions
                                  if p.trade_id == position.trade_id), None)
                if canonical is None: continue
                price = quote.exit_price
                favorable = max(0.0, price-canonical.entry_price)
                adverse = min(0.0, price-canonical.entry_price)
                canonical.maximum_favorable_excursion = max(canonical.maximum_favorable_excursion, favorable)
                canonical.maximum_adverse_excursion = min(canonical.maximum_adverse_excursion, adverse)
                risk = canonical.entry_price-canonical.stop
                if (config.SCALP_PROFIT_PROTECTION_ENABLED and risk > 0
                        and favorable >= config.SCALP_BREAKEVEN_ARM_R*risk):
                    canonical.profit_protection_armed = True
                canonical.current_bid, canonical.current_ask = quote.bid, quote.ask
                canonical.last_price, canonical.last_price_timestamp = quote.mark_price, quote.timestamp.isoformat()
                canonical.quote_source, canonical.quote_mode = quote.source, 'REALTIME_FAST'
                touched = True
                projected_pnl = self.portfolio.state.daily_pnl + sum(
                    ((price-p.entry_price)*p.quantity if p.trade_id == canonical.trade_id
                     else p.unrealized_pnl) for p in self.portfolio.state.open_positions)
                hard_loss = projected_pnl <= -self.portfolio.state.starting_capital*config.MAX_DAILY_LOSS_PERCENT
                reason = self._exit_reason(canonical, quote, data_lookup(position.symbol) or {}, now,
                                           hard_loss=hard_loss)
                if reason is None: continue
                trade = self.engine.close_at_price(canonical.trade_id, price, reason, now=now,
                    exit_method='SCALP_REALTIME_EXIT')
            if trade is None: continue
            self.setup_controller.close(trade.episode_id, trade.symbol)
            event = {'STOP_HIT': 'ScalpStopHit', 'TARGET_HIT': 'ScalpTargetHit',
                     'SCALP_TIME_EXIT': 'ScalpTimeExit', 'EOD_EXIT': 'ScalpEodExit',
                     'HARD_RISK_EXIT': 'ScalpHardRiskExit',
                     'SCALP_PROFIT_PROTECTION_EXIT': 'ScalpProfitProtectionExit'}.get(
                         reason, 'ScalpMomentumExit')
            self.events.emit(event, timestamp=now, symbol=trade.symbol,
                episode_id=trade.episode_id, trade_id=trade.trade_id, exit_reason=reason)
            self.events.emit('ScalpPositionClosed', timestamp=now, symbol=trade.symbol,
                episode_id=trade.episode_id, trade=trade.to_dict())
            print(f"[SCALP {trade.symbol} episode={trade.episode_id}] exit={reason} "
                  f"hold={trade.holding_time_seconds:.0f}s gross={trade.gross_pnl:+.2f} "
                  f"cost={trade.estimated_slippage_cost+trade.estimated_spread_cost:.2f} "
                  f"net={trade.net_pnl:+.2f}", flush=True)
            closed.append(trade)
        if touched:
            self.portfolio.revalue()
            self.portfolio.save(now)
        return closed

    def _exit_reason(self, position, quote, data, now, *, hard_loss=False):
        price = quote.exit_price
        last = quote.last_price or price
        # Mandatory stop has first priority over every soft/profit rule.
        if min(price, last) <= position.stop: return 'STOP_HIT'
        if price >= position.target: return 'TARGET_HIT'
        if self.engine.market_closing(now): return 'EOD_EXIT'
        if hard_loss: return 'HARD_RISK_EXIT'
        entered = timestamp(position.entry_timestamp)
        if entered and (now.astimezone(timezone.utc)-entered).total_seconds() >= config.SCALP_MAX_HOLD_SECONDS:
            return 'SCALP_TIME_EXIT'
        if position.profit_protection_armed and price <= position.entry_price:
            return 'SCALP_PROFIT_PROTECTION_EXIT'
        momentum = _n(data.get('very_short_momentum', data.get('return_1')))
        if momentum is not None and momentum <= -.001: return 'MOMENTUM_REVERSAL'
        vwap = _n(data.get('vwap'))
        if vwap and price < vwap: return 'VWAP_LOSS'
        ema9 = _n(data.get('ema9'))
        if ema9 and price < ema9: return 'EMA9_LOSS'
        volume_acceleration = _n(data.get('volume_acceleration'))
        if volume_acceleration is not None and volume_acceleration < 0 \
                and momentum is not None and momentum <= 0:
            return 'VOLUME_FAILURE'
        return None


def _n(value):
    try: return float(value)
    except (TypeError, ValueError): return None
