"""Deterministic, timestamp-local setup and market-regime classifications."""
from dataclasses import dataclass
from typing import Mapping, Any


SETUP_TYPES = ('BREAKOUT', 'VWAP_RECLAIM', 'PULLBACK_CONTINUATION',
               'MOMENTUM_CONTINUATION', 'REVERSAL', 'UNCLASSIFIED')
REGIMES = ('TRENDING_BULL', 'TRENDING_BEAR', 'CHOPPY',
           'HIGH_VOLATILITY', 'LOW_VOLATILITY', 'UNKNOWN')


@dataclass(frozen=True)
class SetupClassification:
    setup_type: str
    confidence: float
    evidence: tuple[str, ...]


def classify_setup(row: Mapping[str, Any], previous: Mapping[str, Any] | None = None):
    price, vwap = _n(row.get('price', row.get('current_price'))), _n(row.get('vwap'))
    ema9, ema20 = _n(row.get('ema9')), _n(row.get('ema20'))
    resistance, support = _n(row.get('resistance')), _n(row.get('support'))
    rvol, prior_price = _n(row.get('relative_volume')), _n((previous or {}).get('price'))
    evidence = []
    label = 'UNCLASSIFIED'
    if price and resistance and price >= resistance and (rvol or 0) >= 1.2:
        label, evidence = 'BREAKOUT', ['price_at_or_above_known_resistance', 'relative_volume_expansion']
    elif price and vwap and previous and _n(previous.get('price')) is not None \
            and _n(previous.get('vwap')) is not None \
            and _n(previous.get('price')) < _n(previous.get('vwap')) and price >= vwap:
        label, evidence = 'VWAP_RECLAIM', ['crossed_vwap_from_below']
    elif price and ema9 and ema20 and price >= ema9 > ema20 and prior_price and price > prior_price:
        label, evidence = 'MOMENTUM_CONTINUATION', ['price_above_ema9', 'ema9_above_ema20']
    elif price and support and ema20 and abs(price-support)/price <= .005 and price >= ema20:
        label, evidence = 'PULLBACK_CONTINUATION', ['near_known_support', 'above_ema20']
    elif price and vwap and ema9 and ema20 and price > vwap and ema9 <= ema20:
        label, evidence = 'REVERSAL', ['price_above_vwap', 'ema_structure_not_confirmed']
    confidence = min(1.0, .35 + .2 * len(evidence)) if evidence else 0.0
    return SetupClassification(label, confidence, tuple(evidence))


def classify_regime(row: Mapping[str, Any]):
    spy = _n(row.get('spy_return'))
    qqq = _n(row.get('qqq_return'))
    spy_vwap = _n(row.get('spy_vs_vwap'))
    qqq_vwap = _n(row.get('qqq_vs_vwap'))
    vol = _n(row.get('market_volatility'))
    if vol is not None and vol >= .025:
        return 'HIGH_VOLATILITY'
    if vol is not None and vol <= .004:
        return 'LOW_VOLATILITY'
    if None not in (spy, qqq, spy_vwap, qqq_vwap):
        if spy > 0 and qqq > 0 and spy_vwap > 0 and qqq_vwap > 0:
            return 'TRENDING_BULL'
        if spy < 0 and qqq < 0 and spy_vwap < 0 and qqq_vwap < 0:
            return 'TRENDING_BEAR'
        return 'CHOPPY'
    return 'UNKNOWN'


def _n(value):
    try:
        result = float(value)
        return result if result == result and result not in (float('inf'), float('-inf')) else None
    except (TypeError, ValueError):
        return None
