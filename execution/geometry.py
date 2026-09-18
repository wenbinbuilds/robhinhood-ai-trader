"""Deterministic geometry evidence; never optimizes levels against required RR."""
from datetime import datetime
from typing import Mapping, Any
from zoneinfo import ZoneInfo

import config


class GeometryEngine:
    """Typed boundary over the existing deterministic structural validator."""
    @staticmethod
    def decision(symbol, episode_id, now, metrics, market_data, failures=()):
        from trading_runtime.contracts import GeometryDecision
        entry, stop, target = (metrics.get('refreshed_' + key) for key in ('entry', 'stop', 'target'))
        diagnostic = metrics.get('geometry_diagnostics', {})
        return GeometryDecision(
            episode_id, symbol, now.isoformat(), entry,
            market_data.get('intraday_support_reference'), market_data.get('intraday_resistance_reference'),
            stop, target, entry-stop if entry is not None and stop is not None else None,
            target-entry if target is not None and entry is not None else None,
            metrics.get('refreshed_risk_reward_ratio'), diagnostic.get('entry_drift'),
            diagnostic.get('RR_AT_RESEARCH_PRICE'), diagnostic.get('RR_AT_LIVE_ENTRY'),
            not failures and all(v is not None for v in (entry, stop, target)),
            tuple(failures), tuple((market_data.get('structure_evidence') or {}).items()))


def completed_structure(candles, *, now: datetime) -> dict[str, Any]:
    from agent.technical_indicators import calculate_indicators
    indicators = calculate_indicators(candles, now=now, completed_only=True)
    day = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date()
    bars = [bar for bar in indicators['candles'] if datetime.fromisoformat(
        bar['begins_at'].replace('Z', '+00:00')
    ).astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date() == day]
    if not bars:
        raise ValueError('CURRENT_SESSION_COMPLETED_STRUCTURE_UNAVAILABLE')
    latest = datetime.fromisoformat(bars[-1]['begins_at'].replace('Z', '+00:00'))
    # The latest expected completed five-minute bar must be present. A slow
    # context TTL is not authority to retain an older structural snapshot.
    age = (now - latest).total_seconds()
    if age >= 600:
        raise ValueError('COMPLETED_STRUCTURE_STALE')
    resistance = max(bar['high'] for bar in bars)
    support = min(bar['low'] for bar in bars)
    highs = sorted({bar['high'] for bar in bars})
    levels = []
    for level in highs:
        touches = [bar for bar in bars if bar['high'] == level]
        levels.append({
            'price': level, 'touches': len(touches),
            'volume': sum(bar['volume'] for bar in touches),
            'strength': ('REPEATED_SESSION_HIGH' if level == resistance and len(touches) > 1
                         else 'SESSION_HIGH' if level == resistance else 'MINOR_BAR_HIGH'),
        })
    return {
        **indicators,
        'intraday_support_reference': support,
        'intraday_resistance_reference': resistance,
        'structure_evidence': {
            'source': 'COMPLETED_SESSION_CANDLES',
            'latest_completed_bar': bars[-1]['begins_at'],
            'support': support, 'resistance': resistance,
            'target_selection': 'SESSION_HIGH_NO_RR_OPTIMIZATION',
            'resistance_levels': levels,
        },
    }


def geometry_diagnostics(research: Mapping[str, Any], entry, stop, target,
                         *, evidence=None) -> dict[str, Any]:
    original = research.get('research_entry', research.get('entry'))
    old_stop = research.get('research_stop', research.get('stop'))
    old_target = research.get('research_target', research.get('target'))
    def rr(e, s, t):
        return (t - e) / (e - s) if all(isinstance(v, (int, float)) for v in (e,s,t)) and e > s else None
    research_rr = rr(original, old_stop, old_target)
    frozen_rr = rr(entry, old_stop, old_target)
    live_rr = rr(entry, stop, target)
    drift = (entry / original - 1) if original and entry else None
    drift_failure = bool(research_rr is not None and research_rr >= config.MIN_RISK_REWARD_RATIO
                         and frozen_rr is not None and frozen_rr < config.MIN_RISK_REWARD_RATIO
                         and drift is not None and drift > 0)
    failure = live_rr is not None and live_rr < config.MIN_RISK_REWARD_RATIO
    stop_only_rr = rr(entry, stop, old_target)
    target_only_rr = rr(entry, old_stop, target)
    return {
        'research_entry': original, 'research_stop': old_stop, 'research_target': old_target,
        'entry_drift': drift, 'RR_AT_RESEARCH_PRICE': research_rr,
        'RR_AT_LIVE_ENTRY': live_rr, 'RR_AT_LIVE_ENTRY_WITH_RESEARCH_LEVELS': frozen_rr,
        'RR_DEGRADATION': live_rr - research_rr if live_rr is not None and research_rr is not None else None,
        'entry_drift_crossed_rr_boundary': drift_failure,
        'stop_widening_crossed_rr_boundary': bool(
            frozen_rr is not None and frozen_rr >= config.MIN_RISK_REWARD_RATIO
            and stop_only_rr is not None and stop_only_rr < config.MIN_RISK_REWARD_RATIO),
        'resistance_contraction_crossed_rr_boundary': bool(
            frozen_rr is not None and frozen_rr >= config.MIN_RISK_REWARD_RATIO
            and target_only_rr is not None and target_only_rr < config.MIN_RISK_REWARD_RATIO),
        'geometry_restored_valid_rr': bool(drift_failure and live_rr is not None and live_rr >= config.MIN_RISK_REWARD_RATIO),
        'geometry_rejection_class': ('ENTRY_EXTENDED' if failure and drift_failure else
                                    'SETUP_NO_LONGER_HAS_SUFFICIENT_UPSIDE' if failure or (entry and target and target <= entry) else None),
        'reevaluation': 'REEVALUATABLE_NEXT_SLOW_CYCLE',
        'structural_maximum_entry': ((target + config.MIN_RISK_REWARD_RATIO * stop) / (1 + config.MIN_RISK_REWARD_RATIO)
                                     if target and stop else None),
        'nearest_observed_resistance': min((x['price'] for x in (evidence or {}).get('resistance_levels', []) if entry and x['price'] > entry), default=None),
        'distance_to_target_resistance': target - entry if target is not None and entry is not None else None,
        'reward_percent': 100 * (target-entry)/entry if target is not None and entry else None,
        'risk_percent': 100 * (entry-stop)/entry if stop is not None and entry else None,
        'structure_evidence': evidence,
    }
