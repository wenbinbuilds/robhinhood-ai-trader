"""Causal feature construction and train-only normalization."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence, Any
import json
import math
import numpy as np

from rl.setups import SETUP_TYPES, REGIMES, classify_setup, classify_regime


NUMERIC_FEATURES = (
    'price_vs_vwap', 'distance_to_vwap', 'distance_to_ema9', 'distance_to_ema20',
    'ema9_vs_ema20', 'rsi14', 'macd', 'macd_signal', 'macd_histogram',
    'relative_volume', 'spread', 'recent_return_1', 'recent_return_3',
    'recent_return_5', 'stock_return_5m', 'stock_return_15m',
    'spy_return_5m', 'spy_return_15m', 'sector_return_5m', 'sector_return_15m',
    'stock_minus_spy_return_5m', 'stock_minus_spy_return_15m',
    'stock_minus_sector_return_5m', 'stock_minus_sector_return_15m',
    'intraday_volatility', 'current_bar_volume_vs_recent_average',
    'volume_slope', 'volume_acceleration', 'up_bar_volume_ratio', 'down_bar_volume_ratio',
    'pullback_volume_contraction', 'breakout_volume_expansion', 'spy_return', 'qqq_return',
    'spy_vs_vwap', 'qqq_vs_vwap', 'market_volatility', 'sector_return',
    'stock_minus_sector_return', 'stock_minus_market_return', 'relative_strength_market',
    'relative_strength_sector', 'technical_score', 'news_score', 'sector_score',
    'market_score', 'qualitative_score', 'slow_score', 'live_score', 'dynamic_score',
    'dynamic_score_delta_1', 'dynamic_score_delta_3', 'dynamic_score_slope',
    'live_score_slope', 'time_above_watch_threshold', 'time_above_trade_threshold',
    'entry_drift_pct', 'research_rr', 'live_rr', 'distance_to_support',
    'distance_to_resistance', 'setup_age', 'confirmation_count', 'has_position',
    'position_return', 'unrealized_return', 'distance_to_stop', 'distance_to_target',
    'holding_seconds', 'maximum_favorable_excursion', 'maximum_adverse_excursion',
)
FEATURE_NAMES = NUMERIC_FEATURES + tuple('setup_' + x.lower() for x in SETUP_TYPES) \
    + tuple('regime_' + x.lower() for x in REGIMES)
VECTOR_NAMES = FEATURE_NAMES + tuple(name + '_available' for name in FEATURE_NAMES)


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamps must be timezone-aware')
    return parsed.astimezone(timezone.utc)


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class ObservationBuilder:
    """Build a vector using only rows/candles known at observation time."""
    feature_schema_version = '1.0'

    def build(self, rows: Sequence[Mapping[str, Any]], index: int):
        if not 0 <= index < len(rows):
            raise IndexError(index)
        current = rows[index]
        at = _time(current['timestamp'])
        history = [r for r in rows[:index+1] if _time(r['timestamp']) <= at]
        if history[-1] is not current:
            raise ValueError('rows must be chronological')
        session = current.get('session_date') or at.date().isoformat()
        history = [r for r in history if (r.get('session_date') or _time(r['timestamp']).date().isoformat()) == session]
        candles = []
        for row in history:
            for candle in row.get('candles', []) or []:
                began = _time(candle['begins_at'])
                if began + timedelta(minutes=5) <= at and not candle.get('is_forming') and not candle.get('interpolated'):
                    candles.append(candle)
        # Deduplicate repeated snapshots and preserve only the prefix.
        candles = list({c['begins_at']: c for c in candles}.values())
        candles.sort(key=lambda c: _time(c['begins_at']))
        prices = [_number(r.get('price', r.get('current_price'))) for r in history]
        prices = [p for p in prices if p is not None]
        features = self._direct(current, at)
        price = prices[-1] if prices else None
        for periods in (1, 3, 5):
            features[f'recent_return_{periods}'] = (
                price / prices[-periods-1] - 1 if price is not None and len(prices) > periods else None)
        stock_return = features.get('recent_return_1')
        sector_return, spy_return = _number(current.get('sector_return')), _number(current.get('spy_return'))
        features['stock_minus_sector_return'] = stock_return-sector_return if stock_return is not None and sector_return is not None else None
        features['stock_minus_market_return'] = stock_return-spy_return if stock_return is not None and spy_return is not None else None
        features['relative_strength_sector'] = features['stock_minus_sector_return']
        features['relative_strength_market'] = features['stock_minus_market_return']
        for minutes in (5, 15):
            stock = self._return_at(history, at, minutes)
            spy, sector = _number(current.get(f'spy_return_{minutes}m')), _number(current.get(f'sector_return_{minutes}m'))
            features[f'stock_return_{minutes}m'] = stock
            features[f'stock_minus_spy_return_{minutes}m'] = stock-spy if stock is not None and spy is not None else None
            features[f'stock_minus_sector_return_{minutes}m'] = stock-sector if stock is not None and sector is not None else None
        features['relative_strength_market'] = features.get('stock_minus_spy_return_5m')
        features['relative_strength_sector'] = features.get('stock_minus_sector_return_5m')
        self._candle_features(features, candles, price)
        self._score_features(features, history, at)
        causal_row = dict(current)
        known_lows = [_number(c.get('low')) for c in candles if _number(c.get('low')) is not None]
        known_highs = [_number(c.get('high')) for c in candles if _number(c.get('high')) is not None]
        causal_row['support'] = min(known_lows) if known_lows else None
        causal_row['resistance'] = max(known_highs) if known_highs else None
        previous = history[-2] if len(history) > 1 else None
        setup = classify_setup(causal_row, previous)
        regime = classify_regime(current)
        for name in SETUP_TYPES:
            features['setup_' + name.lower()] = float(setup.setup_type == name)
        for name in REGIMES:
            features['regime_' + name.lower()] = float(regime == name)
        vector = []
        for name in FEATURE_NAMES:
            value = _number(features.get(name))
            vector.append(0.0 if value is None else value)
        vector.extend(1.0 if _number(features.get(name)) is not None else 0.0 for name in FEATURE_NAMES)
        meta = {'timestamp': at.isoformat(), 'setup_type': setup.setup_type,
                'setup_type_confidence': setup.confidence, 'setup_evidence': list(setup.evidence),
                'market_regime': regime, 'latest_completed_candle': candles[-1]['begins_at'] if candles else None}
        return np.asarray(vector, dtype=np.float32), features, meta

    def _direct(self, row, at):
        price = _number(row.get('price', row.get('current_price')))
        get = lambda name: _number(row.get(name))
        result = {name: get(name) for name in NUMERIC_FEATURES}
        bid, ask = get('bid'), get('ask')
        if result.get('spread') is None and bid and ask and ask >= bid:
            result['spread'] = (ask-bid)/((ask+bid)/2)
        for name in ('vwap', 'ema9', 'ema20'):
            level = get(name)
            result['distance_to_' + name] = price/level-1 if price and level else None
        result['price_vs_vwap'] = result['distance_to_vwap']
        result['ema9_vs_ema20'] = get('ema9')/get('ema20')-1 if get('ema9') and get('ema20') else None
        result['macd_histogram'] = get('macd')-get('macd_signal') if get('macd') is not None and get('macd_signal') is not None else None
        for level, key in (('support', 'distance_to_support'), ('resistance', 'distance_to_resistance')):
            value = get(level) if self._known_at(row, level, at) else None
            result[key] = (price/value-1 if level == 'support' else value/price-1) if price and value else None
        research = get('research_entry')
        result['entry_drift_pct'] = price/research-1 if price and research else get('entry_drift_pct')
        stock_return, sector_return, spy_return = get('recent_return_1'), get('sector_return'), get('spy_return')
        result['stock_minus_sector_return'] = stock_return-sector_return if stock_return is not None and sector_return is not None else None
        result['stock_minus_market_return'] = stock_return-spy_return if stock_return is not None and spy_return is not None else None
        result['relative_strength_sector'] = result['stock_minus_sector_return']
        result['relative_strength_market'] = result['stock_minus_market_return']
        return result

    @staticmethod
    def _known_at(row, level, at):
        as_of = row.get(level + '_as_of', row.get('structure_as_of'))
        if as_of is None:
            return False
        try: return _time(as_of) <= at
        except (ValueError, TypeError): return False

    @staticmethod
    def _return_at(history, at, minutes):
        current = _number(history[-1].get('price', history[-1].get('current_price')))
        eligible = [r for r in history[:-1] if _time(r['timestamp']) <= at-timedelta(minutes=minutes)]
        prior = _number(eligible[-1].get('price', eligible[-1].get('current_price'))) if eligible else None
        return current/prior-1 if current and prior else None

    @staticmethod
    def _candle_features(f, candles, price):
        if not candles:
            return
        closes = [_number(x.get('close')) for x in candles]
        volumes = [_number(x.get('volume')) for x in candles]
        valid_closes, valid_volumes = [x for x in closes if x is not None], [x for x in volumes if x is not None]
        if len(valid_closes) >= 2:
            returns = np.diff(np.log(valid_closes))
            f['intraday_volatility'] = float(np.std(returns))
        if valid_volumes:
            average = float(np.mean(valid_volumes[-6:-1])) if len(valid_volumes) > 1 else None
            f['current_bar_volume_vs_recent_average'] = valid_volumes[-1]/average if average else None
            f['volume_slope'] = valid_volumes[-1]-valid_volumes[-2] if len(valid_volumes) > 1 else None
            f['volume_acceleration'] = (valid_volumes[-1]-2*valid_volumes[-2]+valid_volumes[-3]) if len(valid_volumes) > 2 else None
            f['breakout_volume_expansion'] = valid_volumes[-1]/average if average else None
            f['pullback_volume_contraction'] = (1-valid_volumes[-1]/valid_volumes[-2]) if len(valid_volumes) > 1 and valid_volumes[-2] else None
        up = [c for c in candles if _number(c.get('close')) is not None and _number(c.get('open')) is not None and _number(c.get('close')) >= _number(c.get('open'))]
        total = sum(_number(c.get('volume')) or 0 for c in candles)
        up_volume = sum(_number(c.get('volume')) or 0 for c in up)
        f['up_bar_volume_ratio'] = up_volume/total if total else None
        f['down_bar_volume_ratio'] = 1-f['up_bar_volume_ratio'] if f['up_bar_volume_ratio'] is not None else None
        if price:
            # Prefix-only known levels; never a future/session-final high.
            support = min(_number(c['low']) for c in candles if _number(c.get('low')) is not None)
            resistance = max(_number(c['high']) for c in candles if _number(c.get('high')) is not None)
            f['distance_to_support'] = price/support-1 if support else None
            f['distance_to_resistance'] = resistance/price-1

    @staticmethod
    def _score_features(f, history, at):
        for score in ('dynamic_score', 'live_score'):
            values = [(_time(r['timestamp']), _number(r.get(score))) for r in history]
            values = [(t, v) for t, v in values if v is not None]
            if len(values) >= 2:
                elapsed = (values[-1][0]-values[-2][0]).total_seconds()/60
                f[score + '_slope'] = (values[-1][1]-values[-2][1])/elapsed if elapsed > 0 else None
            if score == 'dynamic_score' and values:
                f['dynamic_score_delta_1'] = values[-1][1]-values[-2][1] if len(values) >= 2 else None
                f['dynamic_score_delta_3'] = values[-1][1]-values[-4][1] if len(values) >= 4 else None
        for name, threshold in (('watch', .60), ('trade', .72)):
            start = None
            for row in history:
                if (_number(row.get('dynamic_score')) or -1) >= threshold:
                    start = start or _time(row['timestamp'])
                else:
                    start = None
            f['time_above_' + name + '_threshold'] = (at-start).total_seconds() if start else 0.0


@dataclass
class FeatureNormalizer:
    mean: np.ndarray | None = None
    std: np.ndarray | None = None
    fitted_split: str | None = None

    def fit(self, vectors, *, split='train'):
        if split != 'train':
            raise ValueError('normalization may only be fit on training data')
        values = np.asarray(vectors, dtype=np.float64)
        self.mean = values.mean(axis=0)
        self.std = values.std(axis=0)
        self.std[self.std < 1e-12] = 1.0
        self.fitted_split = split
        return self

    def transform(self, vector):
        if self.mean is None or self.std is None:
            raise ValueError('normalizer is not fitted')
        result = (np.asarray(vector)-self.mean)/self.std
        # Availability masks remain binary and interpretable.
        result[len(FEATURE_NAMES):] = np.asarray(vector)[len(FEATURE_NAMES):]
        return result.astype(np.float32)

    def to_dict(self):
        return {'version': '1.0', 'feature_names': list(VECTOR_NAMES),
                'mean': self.mean.tolist(), 'std': self.std.tolist(),
                'fitted_split': self.fitted_split}

    @classmethod
    def from_dict(cls, data):
        if data.get('feature_names') != list(VECTOR_NAMES):
            raise ValueError('normalizer feature schema mismatch')
        return cls(np.asarray(data['mean']), np.asarray(data['std']), data.get('fitted_split'))
