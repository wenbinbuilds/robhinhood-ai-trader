"""Chronological, prefix-only deterministic scalp simulator."""
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from typing import Mapping, Sequence

import config
from strategies.scalp.signals import ScalpSignalEngine, completed_micro_bars
from watcher.models import FastQuote, timestamp


class ScalpBacktester:
    def __init__(self, *, entry_slippage_bps=None, exit_slippage_bps=None,
                 latency_seconds=None):
        self.signal = ScalpSignalEngine()
        self.entry_bps = config.SCALP_ENTRY_SLIPPAGE_BPS if entry_slippage_bps is None else entry_slippage_bps
        self.exit_bps = config.SCALP_EXIT_SLIPPAGE_BPS if exit_slippage_bps is None else exit_slippage_bps
        self.latency_seconds = (config.SCALP_MIN_ENTRY_LATENCY_SECONDS
                                if latency_seconds is None else latency_seconds)

    def run(self, rows: Sequence[Mapping]):
        ordered = sorted(rows, key=lambda row: _at(row['timestamp']))
        positions, closed, seen, pending = {}, [], set(), {}
        histories = {}
        for row in ordered:
            now, symbol = _at(row['timestamp']), str(row['symbol']).upper()
            history = histories.setdefault(symbol, [])
            # Input at T may contain a future bar; the signal engine filters it.
            history.append(dict(row))
            data = dict(row)
            data['candles'] = [bar for item in history for bar in item.get('candles', []) or []]
            quote = FastQuote(symbol, row.get('bid'), row.get('ask'), row.get('last_price', row.get('price')),
                              _at(row.get('quote_timestamp', row['timestamp'])),
                              str(row.get('provider', 'HISTORICAL')), bool(row.get('is_market_open', True)))
            position = positions.get(symbol)
            if position:
                self._mark(position, quote)
                reason = self._exit(position, quote, data, now)
                if reason:
                    closed.append(self._close(position, quote, now, reason, data)); positions.pop(symbol)
                continue
            if symbol in pending:
                pending_episode, ready_at = pending[symbol]
                if now < ready_at: continue
                decision = self.signal.evaluate(pending_episode, symbol, quote, data, now=now)
                pending.pop(symbol, None)
                if decision.approved and self._limits_allow(closed, symbol, now, positions):
                    positions[symbol] = self._open(decision, quote, now, row)
                continue
            bars = completed_micro_bars(data['candles'], now)
            features = self.signal.features(symbol, quote, data, now=now)
            setup, evidence = self.signal.classify(features, bars)
            evidence_at = bars[-1]['begins_at'] if bars else now.isoformat()
            episode = 'SCALP-'+symbol+'-'+sha256(
                f'{symbol}:{setup}:{evidence_at}:{features.recent_high}:{features.recent_low}'.encode()).hexdigest()[:24]
            if episode in seen: continue
            decision = self.signal.evaluate(episode, symbol, quote, data, now=now)
            if not decision.approved or not self._limits_allow(closed, symbol, now, positions): continue
            seen.add(episode)
            if self.latency_seconds > 0:
                pending[symbol] = (episode, now+timedelta(seconds=self.latency_seconds))
            else:
                positions[symbol] = self._open(decision, quote, now, row)
        # EOD truncation uses only each symbol's final known executable bid.
        for symbol, position in list(positions.items()):
            row = histories[symbol][-1]; now = _at(row['timestamp'])
            quote = FastQuote(symbol, row.get('bid'), row.get('ask'), row.get('last_price', row.get('price')),
                              _at(row.get('quote_timestamp', row['timestamp'])), 'HISTORICAL', True)
            closed.append(self._close(position, quote, now, 'EOD_EXIT', row))
        return closed

    def _open(self, decision, quote, now, row):
        ask, bid = float(quote.ask), float(quote.bid)
        fill = ask*(1+self.entry_bps/10_000)
        f = decision.features
        return {'strategy_id': 'SCALP', 'episode_id': decision.episode_id,
            'symbol': decision.symbol, 'entry_time': now, 'entry_price': fill,
            'quoted_entry_bid': bid, 'quoted_entry_ask': ask,
            'stop': decision.stop, 'target': decision.target,
            'risk': fill-decision.stop, 'mfe': 0.0, 'mae': 0.0,
            'setup_type': decision.setup_type, 'market_regime': row.get('market_regime', 'UNKNOWN'),
            'relative_strength_spy': f.relative_strength_spy,
            'relative_strength_qqq': f.relative_strength_qqq,
            'relative_strength_sector': f.relative_strength_sector}

    @staticmethod
    def _limits_allow(closed, symbol, now, positions):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(config.MARKET_TIMEZONE)
        day = now.astimezone(zone).date()
        session = [t for t in closed if t.get('exit_time') and
                   _at(t['exit_time']).astimezone(zone).date() == day]
        if len(session) >= config.SCALP_MAX_TRADES_PER_SESSION: return False
        if sum(t['symbol'] == symbol for t in session) >= config.SCALP_MAX_TRADES_PER_SYMBOL: return False
        if len(positions) >= config.MAX_SIMULTANEOUS_POSITIONS: return False
        consecutive = 0
        for trade in reversed(session):
            if (trade.get('net_pnl') or 0) < 0: consecutive += 1
            else: break
        if consecutive >= config.SCALP_MAX_CONSECUTIVE_LOSSES: return False
        if -sum(min(0, trade.get('net_pnl') or 0) for trade in session) \
                >= config.SHADOW_STARTING_CAPITAL*config.SCALP_MAX_DAILY_LOSS_PERCENT:
            return False
        return True

    @staticmethod
    def _mark(position, quote):
        if quote.exit_price is None: return
        delta = quote.exit_price-position['entry_price']
        position['mfe'], position['mae'] = max(position['mfe'], delta), min(position['mae'], delta)

    def _exit(self, p, quote, data, now):
        price, last = quote.exit_price, quote.last_price or quote.exit_price
        if price is None: return None
        if min(price, last) <= p['stop']: return 'STOP_HIT'
        if price >= p['target']: return 'TARGET_HIT'
        if (now-p['entry_time']).total_seconds() >= config.SCALP_MAX_HOLD_SECONDS: return 'SCALP_TIME_EXIT'
        if (_n(data.get('very_short_momentum', data.get('return_1'))) or 0) <= -.001: return 'MOMENTUM_REVERSAL'
        if _n(data.get('vwap')) and price < _n(data.get('vwap')): return 'VWAP_LOSS'
        if _n(data.get('ema9')) and price < _n(data.get('ema9')): return 'EMA9_LOSS'
        return None

    def _close(self, p, quote, now, reason, data):
        bid = quote.exit_price
        if bid is None:
            return {**p, 'exit_time': now.isoformat(), 'exit_reason': 'UNAVAILABLE',
                    'outcome_status': 'UNAVAILABLE'}
        fill = bid*(1-self.exit_bps/10_000)
        gross = bid-p['quoted_entry_ask']
        net = fill-p['entry_price']
        spread_cost = max(0, p['quoted_entry_ask']-p['quoted_entry_bid'])
        slippage_cost = (p['entry_price']-p['quoted_entry_ask'])+(bid-fill)
        risk = p['risk']
        return {**p, 'exit_time': now.isoformat(), 'exit_price': fill,
                'quoted_exit_bid': quote.bid, 'quoted_exit_ask': quote.ask,
                'exit_reason': reason, 'gross_pnl': gross, 'net_pnl': net,
                'return_percent': net/p['entry_price']*100,
                'r_multiple': net/risk if risk > 0 else None,
                'holding_seconds': max(0, (now-p['entry_time']).total_seconds()),
                'mfe': p['mfe'], 'mae': p['mae'],
                'spread_cost': spread_cost, 'slippage_cost': slippage_cost,
                'estimated_cost': spread_cost+slippage_cost,
                'outcome_status': 'SIMULATED', 'time_period': time_period(now),
                'market_regime': data.get('market_regime', p['market_regime'])}


def time_period(at):
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
    minutes = local.hour*60+local.minute
    if minutes < 10*60: return 'OPENING_WINDOW'
    if minutes < 11*60+30: return 'MID_MORNING'
    if minutes < 14*60: return 'MIDDAY'
    if minutes < 15*60: return 'AFTERNOON'
    return 'POWER_HOUR'


def _at(value):
    parsed = timestamp(value)
    if parsed is None: raise ValueError('timestamp must be timezone-aware')
    return parsed


def _n(value):
    try: return float(value)
    except (TypeError, ValueError): return None
