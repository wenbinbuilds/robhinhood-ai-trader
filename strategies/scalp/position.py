"""Independent deterministic management for canonical open scalp positions."""

from datetime import datetime, timedelta, timezone
from typing import Mapping

import config
from shadow.execution import ShadowExecutionEngine
from strategies.scalp.events import ScalpEventJournal
from strategies.scalp.setup import ScalpSetupController
from watcher.models import FastQuote, timestamp
from strategies.identity import is_scalp_strategy


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError('scalp lifecycle timestamps must be timezone-aware')
    return value.astimezone(timezone.utc)


class ScalpPositionController:
    def __init__(self, portfolio, *, setup_controller=None, events=None, engine=None):
        self.portfolio = portfolio
        self.setup_controller = setup_controller or ScalpSetupController(config.SCALP_STATE_PATH)
        self.events = events or ScalpEventJournal(config.SCALP_EVENT_LOG_PATH)
        self.engine = engine or ShadowExecutionEngine(portfolio)
        self.startup_overdue_trade_ids: set[str] = set()
        self.last_lifecycle: list[dict] = []

    def reconcile_startup(self, *, now: datetime) -> dict:
        """Restore durable wall-clock ages before any new entry discovery.

        No price is invented here. Overdue positions remain canonical portfolio
        positions until the watcher supplies a fresh executable bid.
        """

        current = _utc(now)
        restored = overdue = pending = 0
        rows = []
        event_rows = []
        with self.portfolio.lock:
            for position in self.portfolio.state.open_positions:
                if not is_scalp_strategy(position.strategy_id or position.strategy):
                    continue
                restored += 1
                entered = timestamp(position.entry_timestamp)
                if entered is None:
                    self._invalid_entry_time(position, current)
                    rows.append(self._lifecycle_row(position, current))
                    continue
                hold = max(0.0, (current-entered).total_seconds())
                self._update_age(position, entered, current, hold)
                position.scalp_recovered = True
                event_rows.append(('ScalpPositionRecovered', position))
                if hold >= self._max_hold(position):
                    overdue += 1
                    pending += 1
                    self.startup_overdue_trade_ids.add(position.trade_id)
                    changed = self._mark_overdue(
                        position, current, hold, 'QUOTE_UNAVAILABLE', recovery=True,
                    )
                    if changed:
                        event_rows.append(('ScalpPositionOverdue', position))
                        event_rows.append(('ScalpOverdueExitPending', position))
                else:
                    position.scalp_lifecycle_state = 'ACTIVE'
                    position.scalp_exit_status = 'MONITORING'
                    position.scalp_exit_reason = None
                    position.scalp_next_required_action = 'MONITOR_POSITION'
                rows.append(self._lifecycle_row(position, current))
            if restored:
                self.portfolio.save(current)

        for kind, position in event_rows:
            self._emit_lifecycle(kind, position, current)
        self.last_lifecycle = rows
        if restored:
            print(
                'SCALP POSITION RECOVERY: '
                f'restored={restored} overdue={overdue} exit_pending={pending} closed=0',
                flush=True,
            )
            for row in rows:
                print(
                    f"[SCALP {row['symbol']} episode={row['episode_id']}] "
                    f"age={row['hold_seconds']:.0f}s max_hold={row['max_hold_seconds']}s "
                    f"overdue_by={row['overdue_by_seconds']:.0f}s "
                    f"status={row['exit_status']}",
                    flush=True,
                )
        return {
            'restored': restored, 'overdue': overdue,
            'exit_pending': pending, 'closed': 0, 'positions': rows,
        }

    def process_quotes(self, quotes: Mapping[str, FastQuote], data_lookup=lambda _: {}, *, now):
        current = _utc(now)
        closed = []
        touched = False
        rows = []
        pending_events = []
        for snapshot in list(self.portfolio.snapshot().open_positions):
            if not is_scalp_strategy(snapshot.strategy_id or snapshot.strategy):
                continue
            quote = quotes.get(snapshot.symbol)
            trade = None
            with self.portfolio.lock:
                position = next((p for p in self.portfolio.state.open_positions
                                 if p.trade_id == snapshot.trade_id), None)
                if position is None:
                    continue
                entered = timestamp(position.entry_timestamp)
                if entered is None:
                    self._invalid_entry_time(position, current)
                    rows.append(self._lifecycle_row(position, current))
                    touched = True
                    continue
                hold = max(0.0, (current-entered).total_seconds())
                self._update_age(position, entered, current, hold)
                overdue = hold >= self._max_hold(position)
                quote_reason, quote_age = self._quote_rejection(quote, current, entered)
                position.scalp_latest_exit_quote_age_seconds = quote_age
                if quote_reason is not None:
                    position.monitoring_status = 'PRICE_MONITORING_DEGRADED'
                    if overdue:
                        changed = self._mark_overdue(
                            position, current, hold, quote_reason,
                            recovery=position.trade_id in self.startup_overdue_trade_ids,
                        )
                        if changed:
                            pending_events.append(('ScalpPositionOverdue', position))
                            pending_events.append(('ScalpOverdueExitPending', position))
                    else:
                        position.scalp_lifecycle_state = 'ACTIVE'
                        position.scalp_exit_status = 'MONITORING_DEGRADED'
                        position.scalp_exit_reason = quote_reason
                        position.scalp_next_required_action = 'WAIT_FOR_FRESH_EXIT_QUOTE'
                    rows.append(self._lifecycle_row(position, current))
                    touched = True
                    continue

                # A usable scalp exit is always the executable bid. The shared
                # fill engine then applies configured scalp exit slippage.
                assert quote is not None and quote.executable_bid is not None
                price = quote.executable_bid
                favorable = max(0.0, price-position.entry_price)
                adverse = min(0.0, price-position.entry_price)
                position.maximum_favorable_excursion = max(
                    position.maximum_favorable_excursion, favorable,
                )
                position.maximum_adverse_excursion = min(
                    position.maximum_adverse_excursion, adverse,
                )
                risk = position.entry_price-position.stop
                if (config.SCALP_PROFIT_PROTECTION_ENABLED and risk > 0
                        and favorable >= config.SCALP_BREAKEVEN_ARM_R*risk):
                    position.profit_protection_armed = True
                position.current_bid, position.current_ask = quote.bid, quote.ask
                position.last_price = quote.mark_price
                position.last_price_timestamp = quote.timestamp.isoformat()
                position.quote_source, position.quote_mode = quote.source, 'REALTIME_FAST'
                position.monitoring_status = 'ACTIVE'
                touched = True
                projected_pnl = self.portfolio.state.daily_pnl + sum(
                    ((price-p.entry_price)*p.quantity if p.trade_id == position.trade_id
                     else p.unrealized_pnl) for p in self.portfolio.state.open_positions
                )
                hard_loss = projected_pnl <= (
                    -self.portfolio.state.starting_capital*config.MAX_DAILY_LOSS_PERCENT
                )
                recovery_time_exit = (
                    overdue and position.trade_id in self.startup_overdue_trade_ids
                )
                # Mandatory exits never wait for signal/history data.
                reason = self._priority_exit_reason(
                    position, quote, current, hard_loss=hard_loss,
                    recovery_time_exit=recovery_time_exit,
                )
                if reason is None:
                    try:
                        data = data_lookup(position.symbol) or {}
                    except Exception:
                        data = {}
                    reason = self._signal_exit_reason(position, quote, data)
                if reason is None:
                    position.scalp_lifecycle_state = (
                        'OVERDUE_SCALP_POSITION' if overdue else 'ACTIVE'
                    )
                    position.scalp_exit_status = (
                        'OVERDUE_EXIT_PENDING' if overdue else 'MONITORING'
                    )
                    position.scalp_exit_reason = (
                        'AWAITING_EXIT_DECISION' if overdue else None
                    )
                    position.scalp_next_required_action = (
                        'EXIT_ON_NEXT_VALIDATION' if overdue else 'MONITOR_POSITION'
                    )
                    rows.append(self._lifecycle_row(position, current))
                    continue

                if reason in {'SCALP_TIME_EXIT', 'SCALP_RECOVERY_TIME_EXIT'}:
                    if not position.scalp_max_hold_delay_reasons:
                        if recovery_time_exit:
                            position.scalp_max_hold_delay_reasons.append('PROCESS_RESTART')
                        elif hold > self._max_hold(position):
                            position.scalp_max_hold_delay_reasons.append(
                                'WATCHER_SCHEDULING_DELAY'
                            )
                    position.scalp_exit_status = 'EXIT_DECIDED'
                    position.scalp_exit_reason = reason
                    position.scalp_next_required_action = 'CLOSE_SHADOW_POSITION'
                trade = self.engine.close_at_price(
                    position.trade_id, price, reason, now=current,
                    exit_method='SCALP_REALTIME_EXIT',
                )
            if trade is None:
                continue
            self.setup_controller.close(
                trade.episode_id, trade.symbol, now=now,
                reason=f'TRADE_COMPLETED:{trade.exit_reason}',
            )
            event = {
                'STOP_HIT': 'ScalpStopHit', 'TARGET_HIT': 'ScalpTargetHit',
                'SCALP_TIME_EXIT': 'ScalpTimeExit',
                'SCALP_RECOVERY_TIME_EXIT': 'ScalpRecoveryTimeExit',
                'EOD_EXIT': 'ScalpEodExit', 'HARD_RISK_EXIT': 'ScalpHardRiskExit',
                'SCALP_PROFIT_PROTECTION_EXIT': 'ScalpProfitProtectionExit',
            }.get(reason, 'ScalpMomentumExit')
            self.events.emit(
                event, timestamp=current, symbol=trade.symbol,
                episode_id=trade.episode_id, trade_id=trade.trade_id,
                exit_reason=reason, recovery=trade.recovery_exit,
                crossed_max_hold_at=trade.crossed_max_hold_at,
                exit_decision_at=trade.exit_decision_at,
                position_closed_at=trade.exit_timestamp,
                decision_delay_seconds=trade.max_hold_decision_delay_seconds,
                close_delay_seconds=trade.max_hold_close_delay_seconds,
            )
            self.events.emit(
                'ScalpPositionClosed', timestamp=current, symbol=trade.symbol,
                episode_id=trade.episode_id, trade=trade.to_dict(),
            )
            print(
                f"[SCALP EXIT] {trade.symbol} episode={trade.episode_id} reason={reason} "
                f"hold={trade.holding_time_seconds:.0f}s gross={trade.gross_pnl:+.2f} "
                f"cost={trade.estimated_slippage_cost+trade.estimated_spread_cost:.2f} "
                f"net={trade.net_pnl:+.2f}",
                flush=True,
            )
            rows.append(self._closed_lifecycle_row(trade))
            closed.append(trade)

        if touched:
            self.portfolio.revalue()
            self.portfolio.save(current)
        for kind, position in pending_events:
            self._emit_lifecycle(kind, position, current)
        for position in {
            item.trade_id: item for _kind, item in pending_events
        }.values():
            row = self._lifecycle_row(position, current)
            print(
                f"[SCALP {row['symbol']} episode={row['episode_id']}] "
                f"hold={row['hold_seconds']:.0f}s max_hold={row['max_hold_seconds']}s "
                f"overdue_by={row['overdue_by_seconds']:.0f}s "
                f"status={row['exit_status']} reason={row['exit_reason']}",
                flush=True,
            )
        self.last_lifecycle = rows
        return closed

    @staticmethod
    def _max_hold(position) -> int:
        return int(position.scalp_max_hold_seconds or config.SCALP_MAX_HOLD_SECONDS)

    def _update_age(self, position, entered, current, hold):
        maximum = self._max_hold(position)
        position.scalp_max_hold_seconds = maximum
        position.scalp_hold_seconds = round(hold, 3)
        position.scalp_time_remaining_seconds = round(max(0.0, maximum-hold), 3)
        position.scalp_overdue_by_seconds = round(max(0.0, hold-maximum), 3)
        position.scalp_lifecycle_updated_at = current.isoformat()
        if hold >= maximum and position.scalp_overdue_since is None:
            position.scalp_overdue_since = (entered+timedelta(seconds=maximum)).isoformat()
        if hold < maximum:
            position.scalp_overdue_since = None
            position.scalp_overdue_detected_at = None
            position.scalp_overdue_by_seconds = 0.0

    def _mark_overdue(self, position, current, hold, reason, *, recovery):
        before = (
            position.scalp_lifecycle_state, position.scalp_exit_status,
            position.scalp_exit_reason,
        )
        position.scalp_lifecycle_state = 'OVERDUE_SCALP_POSITION'
        position.scalp_exit_status = 'OVERDUE_EXIT_PENDING'
        position.scalp_exit_reason = reason
        position.scalp_next_required_action = 'WAIT_FOR_FRESH_EXIT_QUOTE'
        position.scalp_recovered = position.scalp_recovered or recovery
        if position.scalp_overdue_detected_at is None:
            position.scalp_overdue_detected_at = current.isoformat()
        delay_reason = 'PROCESS_RESTART' if recovery else reason
        if delay_reason not in position.scalp_max_hold_delay_reasons:
            position.scalp_max_hold_delay_reasons.append(delay_reason)
        if reason not in position.scalp_max_hold_delay_reasons:
            position.scalp_max_hold_delay_reasons.append(reason)
        return before != (
            position.scalp_lifecycle_state, position.scalp_exit_status,
            position.scalp_exit_reason,
        )

    @staticmethod
    def _invalid_entry_time(position, current):
        position.scalp_lifecycle_state = 'INVALID_SCALP_POSITION'
        position.scalp_lifecycle_updated_at = current.isoformat()
        position.scalp_exit_status = 'OVERDUE_EXIT_PENDING'
        position.scalp_exit_reason = 'INVALID_ENTRY_TIMESTAMP'
        position.scalp_next_required_action = 'MANUAL_STATE_REVIEW'

    @staticmethod
    def _quote_rejection(quote, now, entered):
        if quote is None:
            return 'QUOTE_UNAVAILABLE', None
        if quote.timestamp.tzinfo is None:
            return 'STALE_QUOTE', None
        age = quote.age_at(now)
        if quote.timestamp < entered or not 0 <= age <= config.SCALP_MAX_QUOTE_AGE_SECONDS:
            return 'STALE_QUOTE', age
        if quote.executable_bid is None:
            return 'QUOTE_UNAVAILABLE', age
        return None, age

    def _priority_exit_reason(self, position, quote, now, *, hard_loss=False,
                              recovery_time_exit=False):
        price = quote.executable_bid
        assert price is not None
        last = quote.last_price or price
        if min(price, last) <= position.stop:
            return 'STOP_HIT'
        if price >= position.target:
            return 'TARGET_HIT'
        if self.engine.market_closing(now):
            return 'EOD_EXIT'
        if hard_loss:
            return 'HARD_RISK_EXIT'
        entered = timestamp(position.entry_timestamp)
        if entered and (now-entered).total_seconds() >= self._max_hold(position):
            return ('SCALP_RECOVERY_TIME_EXIT' if recovery_time_exit
                    else 'SCALP_TIME_EXIT')
        if position.profit_protection_armed and price <= position.entry_price:
            return 'SCALP_PROFIT_PROTECTION_EXIT'
        return None

    @staticmethod
    def _signal_exit_reason(position, quote, data):
        price = quote.executable_bid
        assert price is not None
        momentum = _n(data.get('very_short_momentum', data.get('return_1')))
        if momentum is not None and momentum <= -.001:
            return 'MOMENTUM_REVERSAL'
        vwap = _n(data.get('vwap'))
        if vwap and price < vwap:
            return 'VWAP_LOSS'
        ema9 = _n(data.get('ema9'))
        if ema9 and price < ema9:
            return 'EMA9_LOSS'
        volume_acceleration = _n(data.get('volume_acceleration'))
        if (volume_acceleration is not None and volume_acceleration < 0
                and momentum is not None and momentum <= 0):
            return 'VOLUME_FAILURE'
        return None

    def _lifecycle_row(self, position, current):
        return {
            'symbol': position.symbol, 'episode_id': position.episode_id,
            'trade_id': position.trade_id, 'entry_time': position.entry_timestamp,
            'current_time': current.isoformat(),
            'hold_seconds': position.scalp_hold_seconds,
            'max_hold_seconds': self._max_hold(position),
            'time_remaining_seconds': position.scalp_time_remaining_seconds,
            'overdue_by_seconds': position.scalp_overdue_by_seconds,
            'lifecycle_state': position.scalp_lifecycle_state,
            'exit_status': position.scalp_exit_status,
            'exit_reason': position.scalp_exit_reason,
            'recovery': position.scalp_recovered,
            'stop': position.stop, 'target': position.target,
            'latest_exit_quote_age_seconds': position.scalp_latest_exit_quote_age_seconds,
            'next_required_action': position.scalp_next_required_action,
            'crossed_max_hold_at': position.scalp_overdue_since,
            'overdue_detected_at': position.scalp_overdue_detected_at,
            'max_hold_delay_reasons': list(position.scalp_max_hold_delay_reasons),
        }

    @staticmethod
    def _closed_lifecycle_row(trade):
        return {
            'symbol': trade.symbol, 'episode_id': trade.episode_id,
            'trade_id': trade.trade_id, 'entry_time': trade.entry_timestamp,
            'current_time': trade.exit_timestamp,
            'hold_seconds': trade.holding_time_seconds,
            'max_hold_seconds': trade.configured_max_hold_seconds,
            'time_remaining_seconds': 0.0,
            'overdue_by_seconds': trade.max_hold_close_delay_seconds or 0.0,
            'lifecycle_state': 'CLOSED',
            'exit_status': ('RECOVERY_TIME_EXIT' if trade.recovery_exit else 'CLOSED'),
            'exit_reason': trade.exit_reason, 'recovery': trade.recovery_exit,
            'stop': trade.stop, 'target': trade.target,
            'latest_exit_quote_age_seconds': None,
            'next_required_action': 'NONE',
            'crossed_max_hold_at': trade.crossed_max_hold_at,
            'exit_decision_at': trade.exit_decision_at,
            'position_closed_at': trade.exit_timestamp,
            'decision_delay_seconds': trade.max_hold_decision_delay_seconds,
            'close_delay_seconds': trade.max_hold_close_delay_seconds,
            'max_hold_delay_reasons': list(trade.max_hold_delay_reasons),
        }

    def _emit_lifecycle(self, event, position, now):
        self.events.emit(
            event, timestamp=now, symbol=position.symbol,
            episode_id=position.episode_id,
            lifecycle=self._lifecycle_row(position, now),
        )


def _n(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
