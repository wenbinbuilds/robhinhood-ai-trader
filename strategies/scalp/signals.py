"""Causal microstructure features and transparent deterministic entry logic."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

import config
from strategies.scalp.models import ScalpEntryDecision, ScalpFeatures
from watcher.models import FastQuote, timestamp


SETUP_TYPES = ('MICRO_BREAKOUT', 'VWAP_RECLAIM', 'EMA9_CONTINUATION',
               'MICRO_PULLBACK', 'MOMENTUM_BURST', 'UNCLASSIFIED')


def _n(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError): return None


def completed_micro_bars(rows: Sequence[Mapping[str, Any]], now: datetime):
    """Only retain bars whose full provider-declared interval ended by now."""
    result = []
    for row in rows:
        began = timestamp(row.get('begins_at'))
        interval = _n(row.get('interval_seconds')) or 300
        if began is None or row.get('is_forming') or row.get('interpolated'):
            continue
        if began + timedelta(seconds=interval) > now.astimezone(timezone.utc):
            continue
        values = {name: _n(row.get(name)) for name in ('open','high','low','close','volume')}
        if any(value is None for value in values.values()) or values['high'] < values['low'] or values['volume'] < 0:
            continue
        result.append({**values, 'begins_at': began.isoformat(), 'interval_seconds': interval})
    result.sort(key=lambda row: row['begins_at'])
    return result


class ScalpSignalEngine:
    """Signal attractiveness is scored separately from non-negotiable gates."""
    def features(self, symbol: str, quote: FastQuote | None, data: Mapping[str, Any], *, now: datetime):
        bars = completed_micro_bars(data.get('candles', []) or [], now)
        closes = [bar['close'] for bar in bars]
        volumes = [bar['volume'] for bar in bars]
        price = quote.mark_price if quote else None
        bid, ask = (quote.bid, quote.ask) if quote else (None, None)
        spread = ((ask-bid)/((ask+bid)/2) if bid and ask and ask >= bid else None)
        age = quote.age_at(now) if quote and quote.timestamp.tzinfo else None
        vwap, ema9, ema20 = (_n(data.get(name)) for name in ('vwap','ema9','ema20'))
        prior_ema9 = _n(data.get('previous_ema9'))
        ret1 = closes[-1]/closes[-2]-1 if len(closes) >= 2 and closes[-2] else None
        ret3 = closes[-1]/closes[-4]-1 if len(closes) >= 4 and closes[-4] else None
        returns = [closes[i]/closes[i-1]-1 for i in range(1, len(closes)) if closes[i-1]]
        recent = bars[-6:]
        high = max((bar['high'] for bar in recent), default=None)
        low = min((bar['low'] for bar in recent), default=None)
        avg_volume = mean(volumes[-6:-1]) if len(volumes) >= 2 else None
        volume_slope = volumes[-1]-volumes[-2] if len(volumes) >= 2 else None
        volume_accel = volumes[-1]-2*volumes[-2]+volumes[-3] if len(volumes) >= 3 else None
        expansion = volumes[-1]/avg_volume if avg_volume else None
        contraction = 1-volumes[-1]/volumes[-2] if len(volumes) >= 2 and volumes[-2] else None
        stock_return = ret3
        def relative(name):
            benchmark = _n(data.get(name))
            return stock_return-benchmark if stock_return is not None and benchmark is not None else None
        return ScalpFeatures(
            symbol.upper(), now.astimezone(timezone.utc).isoformat(), age, spread, price,
            price/vwap-1 if price and vwap else None,
            price/ema9-1 if price and ema9 else None,
            price/ema20-1 if price and ema20 else None,
            ema9-prior_ema9 if ema9 is not None and prior_ema9 is not None else None,
            ema9/ema20-1 if ema9 and ema20 else None, ret1, ret3,
            mean(returns[-3:]) if returns else None, _n(data.get('rsi14')),
            (_n(data.get('macd'))-_n(data.get('macd_signal'))
             if _n(data.get('macd')) is not None and _n(data.get('macd_signal')) is not None else None),
            _n(data.get('relative_volume')), volume_slope, volume_accel, expansion, contraction,
            relative('spy_return_3bar'), relative('qqq_return_3bar'), relative('sector_return_3bar'),
            price/ema9-1 if price and ema9 else None,
            high/price-1 if price and high else None, price/low-1 if price and low else None,
            pstdev(returns[-6:]) if len(returns) >= 2 else None,
            high, low, vwap, ema9, ema20,
            bars[-1]['begins_at'] if bars else None, len(bars))

    def classify(self, features: ScalpFeatures, bars):
        evidence = []
        f = features
        previous_high = max((bar['high'] for bar in bars[-6:-1]), default=None)
        previous_close = bars[-2]['close'] if len(bars) >= 2 else None
        if f.price and previous_high and f.price > previous_high and (f.breakout_volume_expansion or 0) >= 1.2:
            return 'MICRO_BREAKOUT', ('fresh_break_above_completed_micro_high','volume_expansion')
        if f.price and f.vwap and previous_close and previous_close < f.vwap <= f.price:
            return 'VWAP_RECLAIM', ('completed_close_below_vwap','current_price_above_vwap')
        if f.price and f.ema9 and f.ema20 and f.price >= f.ema9 > f.ema20 and (f.ema9_slope or 0) > 0:
            return 'EMA9_CONTINUATION', ('price_above_ema9','ema9_rising','ema9_above_ema20')
        if f.price and f.ema9 and f.ema20 and abs(f.price/f.ema9-1) <= .0015 and f.ema9 > f.ema20:
            return 'MICRO_PULLBACK', ('price_near_ema9','ema_trend_positive')
        if (f.return_1 or 0) > .001 and (f.volume_acceleration or 0) > 0:
            return 'MOMENTUM_BURST', ('positive_micro_return','volume_acceleration')
        return 'UNCLASSIFIED', ()

    def evaluate(self, episode_id: str, symbol: str, quote: FastQuote | None,
                 data: Mapping[str, Any], *, now: datetime):
        bars = completed_micro_bars(data.get('candles', []) or [], now)
        f = self.features(symbol, quote, data, now=now)
        setup, evidence = self.classify(f, bars)
        reasons = []
        if quote is None or quote.symbol != symbol.upper(): reasons.append('QUOTE_UNAVAILABLE')
        elif f.quote_age is None or not 0 <= f.quote_age <= config.SCALP_MAX_QUOTE_AGE_SECONDS: reasons.append('STALE_QUOTE')
        if quote is not None and quote.is_market_open is not True: reasons.append('MARKET_CLOSED')
        if f.spread_pct is None: reasons.append('INVALID_BID_ASK')
        elif f.spread_pct > config.SCALP_MAX_SPREAD_PCT: reasons.append('SPREAD_TOO_WIDE')
        if f.bar_count < 6: reasons.append('INSUFFICIENT_COMPLETED_MICRO_BARS')
        latest = timestamp(f.latest_bar_timestamp)
        latest_age = ((now.astimezone(timezone.utc)-latest).total_seconds()
                      - (_n(bars[-1].get('interval_seconds')) or 60)
                      if latest is not None and bars else None)
        if latest_age is None or latest_age > config.SCALP_MAX_MICRO_BAR_AGE_SECONDS:
            reasons.append('MICRO_BARS_STALE')
        if f.relative_volume is None or f.relative_volume < config.SCALP_MIN_RELATIVE_VOLUME: reasons.append('INSUFFICIENT_LIQUIDITY')
        if setup == 'UNCLASSIFIED': reasons.append('UNCLASSIFIED_SETUP')
        components = {
            'momentum': min(1.0, max(0.0, (f.return_3 or 0)/.003)),
            'vwap_alignment': 1.0 if (f.price_vs_vwap or -1) > 0 else 0.0,
            'ema_slope': min(1.0, max(0.0, (f.ema9_slope or 0)/(f.price or 1)/.001)),
            'relative_strength': min(1.0, max(0.0, (f.relative_strength_spy or 0)/.002)),
            'volume': min(1.0, max(0.0, ((f.breakout_volume_expansion or 1)-1)/.5)),
            'spread_quality': max(0.0, 1-(f.spread_pct or config.SCALP_MAX_SPREAD_PCT)/config.SCALP_MAX_SPREAD_PCT),
        }
        extension_penalty = min(1.0, max(0.0, ((f.entry_extension or 0)-config.SCALP_MAX_EXTENSION_PCT)/config.SCALP_MAX_EXTENSION_PCT))
        score = (.25*components['momentum'] + .20*components['vwap_alignment']
                 + .15*components['ema_slope'] + .15*components['relative_strength']
                 + .15*components['volume'] + .10*components['spread_quality']
                 - .20*extension_penalty)
        if score < config.SCALP_MIN_SIGNAL_SCORE: reasons.append('SIGNAL_SCORE_BELOW_THRESHOLD')
        if (f.entry_extension or 0) > config.SCALP_MAX_EXTENSION_PCT: reasons.append('ENTRY_OVEREXTENDED')
        entry = quote.ask if quote and quote.ask else None
        structural = [x for x in (f.recent_low, f.vwap, f.ema9) if x and entry and x < entry]
        stop = max(structural) if structural else None
        risk_pct = (entry-stop)/entry if entry and stop else None
        if risk_pct is None or risk_pct < config.SCALP_MIN_STOP_DISTANCE_PCT: reasons.append('INVALID_STOP')
        projected = max(config.SCALP_MIN_EXPECTED_MOVE_PCT,
                        min(config.SCALP_MAX_EXPECTED_MOVE_PCT,
                            max((f.realized_volatility or 0)*1.25, f.return_3 or 0)))
        resistance_distance = f.recent_high/entry-1 if f.recent_high and entry and f.recent_high > entry else None
        expected_move = min(projected, resistance_distance) if resistance_distance else projected
        cost = ((f.spread_pct or 0) +
                (config.SCALP_ENTRY_SLIPPAGE_BPS+config.SCALP_EXIT_SLIPPAGE_BPS)/10_000 +
                (2*config.SCALP_COMMISSION_PER_SHARE/entry if entry else 0)) if entry else None
        net_edge = expected_move-cost if cost is not None else None
        if net_edge is None or net_edge < config.SCALP_MIN_EXPECTED_NET_EDGE: reasons.append('INSUFFICIENT_NET_EDGE')
        target = entry*(1+expected_move) if entry else None
        reward_pct = expected_move
        rr = reward_pct/risk_pct if risk_pct and reward_pct is not None else None
        net_reward = reward_pct-cost if reward_pct is not None and cost is not None else None
        net_rr = net_reward/risk_pct if risk_pct and net_reward is not None else None
        if target is None or entry is None or target <= entry: reasons.append('INVALID_TARGET')
        if rr is None or rr < config.SCALP_MIN_RISK_REWARD: reasons.append('SCALP_RR_BELOW_MINIMUM')
        return ScalpEntryDecision(
            'SCALP', episode_id, symbol.upper(), setup, evidence, round(score, 6),
            expected_move, cost, net_edge, entry, stop, target, risk_pct,
            reward_pct, rr, net_reward, net_rr, config.SCALP_MAX_HOLD_SECONDS,
            not reasons, tuple(dict.fromkeys(reasons)), now.astimezone(timezone.utc).isoformat(), f)
