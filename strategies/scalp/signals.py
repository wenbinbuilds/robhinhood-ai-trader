"""Causal microstructure features and transparent deterministic entry logic."""
from dataclasses import replace
from datetime import datetime, timezone
from math import isfinite
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

import config
from strategies.scalp.models import ScalpEntryDecision, ScalpFeatures
from strategies.scalp.freshness import completed_micro_bars, micro_bar_freshness
from agent.technical_indicators import ema_series
from watcher.models import FastQuote


SETUP_TYPES = ('MICRO_BREAKOUT', 'VWAP_RECLAIM', 'EMA9_CONTINUATION',
               'MICRO_PULLBACK', 'MOMENTUM_BURST', 'UNCLASSIFIED')


def _n(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError): return None


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
        if prior_ema9 is None:
            ema_values = ema_series(closes, 9)
            prior_ema9 = ema_values[-2] if len(ema_values) >= 2 else None
        ret1 = closes[-1]/closes[-2]-1 if len(closes) >= 2 and closes[-2] else None
        ret3 = closes[-1]/closes[-4]-1 if len(closes) >= 4 and closes[-4] else None
        returns = [closes[i]/closes[i-1]-1 for i in range(1, len(closes)) if closes[i-1]]
        quote_micro = (dict(data.get('quote_microstructure', {}))
                       if isinstance(data.get('quote_microstructure'), Mapping) else {})
        quote_samples = int(quote_micro.get('sample_count') or 0)
        quote_short = _n(quote_micro.get('short_price_momentum'))
        quote_very_short = _n(quote_micro.get('very_short_momentum'))
        price_return_1 = quote_very_short if quote_very_short is not None else ret1
        price_return_3 = quote_short if quote_short is not None else ret3
        price_timing_source = (
            'REAL_PROVIDER_QUOTES' if quote_short is not None
            else 'PROVIDER_5_MINUTE_CONTEXT'
        )
        recent = bars[-6:]
        high = max((bar['high'] for bar in recent), default=None)
        low = min((bar['low'] for bar in recent), default=None)
        avg_volume = mean(volumes[-6:-1]) if len(volumes) >= 2 else None
        volume_slope = volumes[-1]-volumes[-2] if len(volumes) >= 2 else None
        volume_accel = volumes[-1]-2*volumes[-2]+volumes[-3] if len(volumes) >= 3 else None
        expansion = volumes[-1]/avg_volume if avg_volume else None
        contraction = 1-volumes[-1]/volumes[-2] if len(volumes) >= 2 and volumes[-2] else None
        stock_return = price_return_3
        def relative(name):
            benchmark = _n(data.get(name))
            return stock_return-benchmark if stock_return is not None and benchmark is not None else None
        freshness = micro_bar_freshness(
            data.get('candles', []) or [], now=now,
            provider_status=data.get('micro_bar_provider_status', 'OK' if bars else 'UNAVAILABLE'),
        )
        bar_status = freshness.freshness_status
        provider_status = freshness.provider_status
        feature_bar_status = bar_status if provider_status == 'OK' else 'PROVIDER_ERROR'
        bar_timestamp = freshness.latest_completed_bar_timestamp
        benchmark_timestamp = data.get('spy_feature_timestamp') or bar_timestamp
        volume_status = str(data.get('relative_volume_status') or (
            'UNAVAILABLE' if data.get('relative_volume') is None
            else bar_status
        )).upper()
        liquidity_source = data.get('relative_volume_source', 'UNAVAILABLE')
        liquidity_formula = (
            'LATEST_COMPLETED_BAR_VOLUME/MEAN_PREVIOUS_UP_TO_5_COMPLETED_BARS'
            if liquidity_source == 'COMPLETED_MICRO_BAR_RATIO'
            else 'PROVIDER_SUPPLIED_RELATIVE_VOLUME'
            if liquidity_source in {'PROVIDER', 'PROVIDER_HISTORY'}
            else 'MOMENTUM_SCANNER_VALUE'
            if liquidity_source == 'MOMENTUM_SCANNER' else 'UNAVAILABLE'
        )
        provenance = {
            'return_3': {
                'value': price_return_3,
                'source_timestamp': (quote_micro.get('latest_timestamp')
                                     if price_timing_source == 'REAL_PROVIDER_QUOTES'
                                     else bar_timestamp),
                'status': ('FRESH' if price_timing_source == 'REAL_PROVIDER_QUOTES'
                           else feature_bar_status),
                'source': price_timing_source,
                'fallback_used': price_timing_source != 'REAL_PROVIDER_QUOTES',
            },
            'price_vs_vwap': {'value': price/vwap-1 if price and vwap else None,
                              'source_timestamp': bar_timestamp,
                              'status': feature_bar_status, 'fallback_used': False},
            'ema9_slope': {'value': ema9-prior_ema9 if ema9 is not None and prior_ema9 is not None else None,
                           'source_timestamp': bar_timestamp, 'status': feature_bar_status,
                           'fallback_used': data.get('previous_ema9') is not None},
            'relative_strength_spy': {
                'value': relative('spy_return_3bar'),
                'source_timestamp': benchmark_timestamp,
                'status': (data.get('spy_feature_status') or feature_bar_status)
                if data.get('spy_return_3bar') is not None
                and data.get('benchmark_provider_status', 'OK') == 'OK' else 'UNAVAILABLE',
                'fallback_used': False,
            },
            'breakout_volume_expansion': {'value': expansion, 'source_timestamp': bar_timestamp,
                                          'status': feature_bar_status, 'fallback_used': False},
            'spread_pct': {'value': spread,
                           'source_timestamp': quote.timestamp.isoformat() if quote else None,
                           'status': 'FRESH' if age is not None and 0 <= age <= config.SCALP_MAX_QUOTE_AGE_SECONDS else 'STALE' if quote else 'UNAVAILABLE',
                           'fallback_used': False},
            'relative_volume': {'value': _n(data.get('relative_volume')),
                                'source_timestamp': data.get('relative_volume_timestamp') or bar_timestamp,
                                'status': volume_status,
                                'source': liquidity_source,
                                'fallback_used': liquidity_source == 'COMPLETED_MICRO_BAR_RATIO',
                                'current_volume': volumes[-1] if volumes else None,
                                'baseline_volume': avg_volume,
                                'baseline_bar_count': min(5, max(0, len(volumes)-1)),
                                'bar_timeframe_seconds': freshness.bar_timeframe_seconds,
                                'completed_bars_only': True,
                                'same_time_of_day_normalized': False,
                                'formula': liquidity_formula},
        }
        provenance['volume_expansion'] = {
            **provenance['relative_volume'],
            'legacy_field_alias': 'relative_volume',
            'concept': 'RECENT_COMPLETED_BAR_ACTIVITY_VS_SHORT_BASELINE',
        }
        critical = tuple(provenance[name] for name in (
            'return_3', 'price_vs_vwap', 'ema9_slope', 'relative_strength_spy',
            'breakout_volume_expansion', 'spread_pct',
        ))
        if bar_status == 'STALE' or any(item['status'] == 'STALE' for item in critical):
            signal_data_status = 'STALE'
        elif (provider_status != 'OK' or bar_status == 'UNAVAILABLE'
              or any(item['value'] is None or item['status'] in {'UNAVAILABLE', 'PROVIDER_ERROR'}
                     for item in critical)):
            signal_data_status = 'UNAVAILABLE'
        else:
            signal_data_status = 'VALID'
        return ScalpFeatures(
            symbol.upper(), now.astimezone(timezone.utc).isoformat(), age, spread, price,
            price/vwap-1 if price and vwap else None,
            price/ema9-1 if price and ema9 else None,
            price/ema20-1 if price and ema20 else None,
            ema9-prior_ema9 if ema9 is not None and prior_ema9 is not None else None,
            ema9/ema20-1 if ema9 and ema20 else None, price_return_1, price_return_3,
            (quote_short if quote_short is not None else mean(returns[-3:]) if returns else None),
            _n(data.get('rsi14')),
            (_n(data.get('macd'))-_n(data.get('macd_signal'))
             if _n(data.get('macd')) is not None and _n(data.get('macd_signal')) is not None else None),
            _n(data.get('relative_volume')), volume_slope, volume_accel, expansion, contraction,
            relative('spy_return_3bar'), relative('qqq_return_3bar'), relative('sector_return_3bar'),
            price/ema9-1 if price and ema9 else None,
            high/price-1 if price and high else None, price/low-1 if price and low else None,
            (_n(quote_micro.get('very_short_realized_volatility'))
             if _n(quote_micro.get('very_short_realized_volatility')) is not None
             else pstdev(returns[-6:]) if len(returns) >= 2 else None),
            high, low, vwap, ema9, ema20,
            bar_timestamp, len(bars), freshness.to_dict(), provenance,
            signal_data_status, volume_expansion=_n(data.get('relative_volume')),
            price_timing_source=price_timing_source,
            quote_sample_count=quote_samples,
            quote_window_seconds=_n(quote_micro.get('window_seconds')),
            quote_microstructure=quote_micro,
            entry_extension_reference={
                'production_reference_type': 'EMA9',
                'production_reference_price': ema9,
                'production_reference_timestamp': bar_timestamp,
                'candidate_price': price,
                'candidate_price_timestamp': (
                    quote.timestamp.astimezone(timezone.utc).isoformat()
                    if quote and quote.timestamp.tzinfo else None
                ),
                'formula': 'candidate_mark_price / ema9 - 1',
                'threshold_pct': config.SCALP_MAX_EXTENSION_PCT,
                'volatility_adjusted': False,
                'setup_specific': False,
            })

    @staticmethod
    def score(features: ScalpFeatures):
        """Return auditable production component math and its final score."""

        f = features
        specifications = (
            ('micro_momentum', f.return_3, .25,
             lambda value: value / .003),
            ('vwap_alignment', f.price_vs_vwap, .20,
             lambda value: 1.0 if value > 0 else 0.0),
            ('ema_slope', (f.ema9_slope / f.price
                           if f.ema9_slope is not None and f.price else None), .15,
             lambda value: value / .001),
            ('relative_strength', f.relative_strength_spy, .15,
             lambda value: value / .002),
            ('volume_expansion', f.breakout_volume_expansion, .15,
             lambda value: (value - 1.0) / .5),
            ('spread_quality', f.spread_pct, .10,
             lambda value: 1.0 - value / config.SCALP_MAX_SPREAD_PCT),
        )
        breakdown = {}
        for name, raw, weight, normalize in specifications:
            unclamped = normalize(raw) if raw is not None else None
            normalized = (min(1.0, max(0.0, unclamped))
                          if unclamped is not None else None)
            breakdown[name] = {
                'raw': raw, 'unclamped': unclamped, 'normalized': normalized,
                'weight': weight,
                'contribution': normalized * weight if normalized is not None else None,
                'clamped_min': unclamped is not None and unclamped <= 0,
                'clamped_max': unclamped is not None and unclamped >= 1,
                'missing': raw is None,
                'fallback_used': bool(f.feature_provenance.get(
                    {'micro_momentum': 'return_3', 'vwap_alignment': 'price_vs_vwap',
                     'ema_slope': 'ema9_slope', 'relative_strength': 'relative_strength_spy',
                     'volume_expansion': 'breakout_volume_expansion',
                     'spread_quality': 'spread_pct'}[name], {}
                ).get('fallback_used')),
            }
        raw_extension = f.entry_extension
        unclamped_penalty = ((raw_extension - config.SCALP_MAX_EXTENSION_PCT)
                             / config.SCALP_MAX_EXTENSION_PCT
                             if raw_extension is not None else None)
        normalized_penalty = (min(1.0, max(0.0, unclamped_penalty))
                              if unclamped_penalty is not None else None)
        breakdown['entry_extension_penalty'] = {
            'raw': raw_extension, 'unclamped': unclamped_penalty,
            'normalized': normalized_penalty, 'weight': -.20,
            'contribution': (-.20 * normalized_penalty
                             if normalized_penalty is not None else None),
            'clamped_min': unclamped_penalty is not None and unclamped_penalty <= 0,
            'clamped_max': unclamped_penalty is not None and unclamped_penalty >= 1,
            'missing': raw_extension is None, 'fallback_used': False,
        }
        if f.signal_data_status != 'VALID':
            return breakdown, None
        contributions = [item['contribution'] for item in breakdown.values()]
        return breakdown, sum(contributions) if all(
            value is not None for value in contributions) else None

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

    @staticmethod
    def extension_reference_audit(setup: str, features: ScalpFeatures, bars):
        """Describe production and setup-structural references without changing either."""

        audit = dict(features.entry_extension_reference)
        structural_type = None
        structural_price = None
        structural_timestamp = features.latest_bar_timestamp
        if setup == 'MICRO_BREAKOUT':
            prior = bars[-6:-1]
            if prior:
                reference_bar = max(prior, key=lambda bar: bar['high'])
                structural_type = 'PRIOR_COMPLETED_MICRO_HIGH'
                structural_price = _n(reference_bar.get('high'))
                structural_timestamp = reference_bar.get('begins_at')
        elif setup in {'EMA9_CONTINUATION', 'MICRO_PULLBACK'}:
            structural_type, structural_price = 'EMA9', features.ema9
        elif setup == 'VWAP_RECLAIM':
            structural_type, structural_price = 'VWAP', features.vwap
        elif setup == 'MOMENTUM_BURST':
            structural_type = 'LATEST_COMPLETED_CLOSE'
            structural_price = _n(bars[-1].get('close')) if bars else None
            structural_timestamp = bars[-1].get('begins_at') if bars else None
        audit.update({
            'setup_type': setup,
            'structural_reference_type': structural_type,
            'structural_reference_price': structural_price,
            'structural_reference_timestamp': structural_timestamp,
            'structural_extension_pct': (
                features.price / structural_price - 1
                if features.price and structural_price else None
            ),
            'production_matches_structural_reference': (
                structural_type == 'EMA9'
                and structural_price == features.ema9
            ),
        })
        return audit

    def evaluate(self, episode_id: str, symbol: str, quote: FastQuote | None,
                 data: Mapping[str, Any], *, now: datetime,
                 features: ScalpFeatures | None = None, bars=None):
        bars = (completed_micro_bars(data.get('candles', []) or [], now)
                if bars is None else list(bars))
        f = features or self.features(symbol, quote, data, now=now)
        score_breakdown, score = self.score(f)
        f = replace(f, score_breakdown=score_breakdown)
        setup, evidence = self.classify(f, bars)
        f = replace(
            f,
            entry_extension_reference=self.extension_reference_audit(setup, f, bars),
        )
        reasons = []
        if quote is None or quote.symbol != symbol.upper(): reasons.append('QUOTE_UNAVAILABLE')
        elif f.quote_age is None or not 0 <= f.quote_age <= config.SCALP_MAX_QUOTE_AGE_SECONDS: reasons.append('STALE_QUOTE')
        if quote is not None and quote.is_market_open is not True: reasons.append('MARKET_CLOSED')
        if f.spread_pct is None: reasons.append('INVALID_BID_ASK')
        elif f.spread_pct > config.SCALP_MAX_SPREAD_PCT: reasons.append('SPREAD_TOO_WIDE')
        if f.bar_count < 6: reasons.append('INSUFFICIENT_COMPLETED_MICRO_BARS')
        bar_status = f.micro_bar_freshness.get('freshness_status', 'UNAVAILABLE')
        if bar_status == 'STALE':
            reasons.append('MICRO_BARS_STALE')
        elif bar_status == 'UNAVAILABLE':
            reasons.append('MICRO_BARS_UNAVAILABLE')
        if f.micro_bar_freshness.get('provider_status') != 'OK':
            reasons.append('MICRO_BAR_REFRESH_FAILED')
        volume_status = f.feature_provenance.get(
            'volume_expansion', {}
        ).get('status', 'UNAVAILABLE')
        if f.micro_bar_freshness.get('provider_status') != 'OK':
            reasons.append('VOLUME_DATA_UNAVAILABLE')
        elif volume_status == 'STALE' or bar_status == 'STALE':
            reasons.append('VOLUME_DATA_STALE')
        elif f.volume_expansion is None or volume_status == 'UNAVAILABLE':
            reasons.append('VOLUME_DATA_UNAVAILABLE')
        elif f.volume_expansion < config.SCALP_MIN_VOLUME_EXPANSION:
            reasons.append('VOLUME_EXPANSION_BELOW_MINIMUM')

        # Gate ordering is intentional: later calculations remain available as
        # offline counterfactual diagnostics, but their rejection labels are
        # emitted only when the candidate actually reached that stage.
        eligible = not reasons
        if eligible and setup == 'UNCLASSIFIED':
            reasons.append('UNCLASSIFIED_SETUP')
        signal_eligible = not reasons
        if signal_eligible:
            if score is None:
                reasons.append('SIGNAL_DATA_STALE' if f.signal_data_status == 'STALE'
                               else 'SIGNAL_DATA_INVALID')
            elif score < config.SCALP_MIN_SIGNAL_SCORE:
                reasons.append('SIGNAL_SCORE_BELOW_THRESHOLD')
        score_passed = signal_eligible and score is not None and score >= config.SCALP_MIN_SIGNAL_SCORE
        if score_passed and (f.entry_extension or 0) > config.SCALP_MAX_EXTENSION_PCT:
            reasons.append('ENTRY_OVEREXTENDED')
        entry = quote.ask if quote and quote.ask else None
        structural = [x for x in (f.recent_low, f.vwap, f.ema9) if x and entry and x < entry]
        stop = max(structural) if structural else None
        risk_pct = (entry-stop)/entry if entry and stop else None
        extension_passed = score_passed and 'ENTRY_OVEREXTENDED' not in reasons
        if extension_passed and (
            risk_pct is None or risk_pct < config.SCALP_MIN_STOP_DISTANCE_PCT
        ):
            reasons.append('INVALID_STOP')
        projected = max(config.SCALP_MIN_EXPECTED_MOVE_PCT,
                        min(config.SCALP_MAX_EXPECTED_MOVE_PCT,
                            max((f.realized_volatility or 0)*1.25, f.return_3 or 0)))
        resistance_distance = f.recent_high/entry-1 if f.recent_high and entry and f.recent_high > entry else None
        expected_move = min(projected, resistance_distance) if resistance_distance else projected
        cost = ((f.spread_pct or 0) +
                (config.SCALP_ENTRY_SLIPPAGE_BPS+config.SCALP_EXIT_SLIPPAGE_BPS)/10_000 +
                (2*config.SCALP_COMMISSION_PER_SHARE/entry if entry else 0)) if entry else None
        net_edge = expected_move-cost if cost is not None else None
        stop_passed = extension_passed and 'INVALID_STOP' not in reasons
        if stop_passed and (
            net_edge is None or net_edge < config.SCALP_MIN_EXPECTED_NET_EDGE
        ):
            reasons.append('INSUFFICIENT_NET_EDGE')
        target = entry*(1+expected_move) if entry else None
        reward_pct = expected_move
        rr = reward_pct/risk_pct if risk_pct and reward_pct is not None else None
        net_reward = reward_pct-cost if reward_pct is not None and cost is not None else None
        net_rr = net_reward/risk_pct if risk_pct and net_reward is not None else None
        edge_passed = stop_passed and 'INSUFFICIENT_NET_EDGE' not in reasons
        if edge_passed and (target is None or entry is None or target <= entry):
            reasons.append('INVALID_TARGET')
        target_passed = edge_passed and 'INVALID_TARGET' not in reasons
        if target_passed and (rr is None or rr < config.SCALP_MIN_RISK_REWARD):
            reasons.append('SCALP_RR_BELOW_MINIMUM')
        return ScalpEntryDecision(
            'SCALP', episode_id, symbol.upper(), setup, evidence,
            round(score, 6) if score is not None else None,
            expected_move, cost, net_edge, entry, stop, target, risk_pct,
            reward_pct, rr, net_reward, net_rr, config.SCALP_MAX_HOLD_SECONDS,
            not reasons, tuple(dict.fromkeys(reasons)), now.astimezone(timezone.utc).isoformat(), f)
