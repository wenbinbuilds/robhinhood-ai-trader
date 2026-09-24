"""Durable, strategy-neutral observability for the deterministic scalp path."""

from collections import Counter, defaultdict
from datetime import timezone
from math import isfinite
from statistics import mean, median
from uuid import uuid4

import config
from trading_runtime.journal import EventJournal
from strategies.scalp.signals import SETUP_TYPES
from strategies.scalp.freshness import micro_bar_freshness


OVERTRADING_REASONS = {
    'SCALP_SESSION_TRADE_LIMIT', 'SCALP_SYMBOL_TRADE_LIMIT',
    'SCALP_CONSECUTIVE_LOSS_LIMIT', 'SCALP_DAILY_LOSS_LIMIT',
    'SCALP_TRANSACTION_COST_BUDGET',
}
STAGE_NAMES = (
    'universe_observations', 'provider_ok', 'duplicate_symbol_pass',
    'runtime_safety_pass', 'quote_fresh', 'market_open_pass', 'valid_bid_ask',
    'spread_pass', 'micro_bars_pass', 'execution_liquidity_pass',
    'volume_expansion_pass', 'liquidity_pass',
    'eligible_micro_signals', 'episode_open_pass', 'signal_score_pass', 'extension_pass',
    'stop_pass', 'expected_edge_pass', 'target_pass', 'scalp_rr_pass',
    'geometry_pass', 'overtrading_pass', 'entry_attempts', 'risk_attempts',
    'risk_approved', 'pre_execution_attempts', 'pre_execution_passes',
    'portfolio_attempts', 'portfolio_approved', 'safety_approved', 'entries',
)

EARLY_FILTER_STAGES = {
    'provider_ok', 'quote_fresh', 'market_open_pass', 'valid_bid_ask',
    'spread_pass', 'micro_bars_pass', 'execution_liquidity_pass',
    'volume_expansion_pass', 'liquidity_pass',
    'eligible_micro_signals',
}

STAGE_REASONS = (
    ('provider_ok', {'QUOTE_UNAVAILABLE'}),
    ('duplicate_symbol_pass', {
        'DUPLICATE_POSITION', 'EXISTING_POSITION_OTHER_STRATEGY',
    }),
    ('runtime_safety_pass', {
        'SCALP_DISABLED', 'SCALP_SHADOW_ONLY', 'SCALP_SAFETY_FLAGS_INVALID',
        'SCALP_KILL_SWITCH_NOT_BLOCKED', 'OVERDUE_EXIT_PENDING',
    }),
    ('quote_fresh', {'STALE_QUOTE'}),
    ('market_open_pass', {'MARKET_CLOSED'}),
    ('valid_bid_ask', {'INVALID_BID_ASK'}),
    ('spread_pass', {'SPREAD_TOO_WIDE'}),
    ('micro_bars_pass', {
        'INSUFFICIENT_COMPLETED_MICRO_BARS', 'MICRO_BARS_STALE',
        'MICRO_BARS_UNAVAILABLE', 'MICRO_BAR_REFRESH_FAILED',
    }),
    ('volume_expansion_pass', {
        'VOLUME_EXPANSION_BELOW_MINIMUM', 'VOLUME_DATA_STALE',
        'VOLUME_DATA_UNAVAILABLE',
    }),
    ('eligible_micro_signals', {'UNCLASSIFIED_SETUP'}),
    ('episode_open_pass', {'STALE_SCALP_EPISODE'}),
    ('signal_score_pass', {
        'SIGNAL_SCORE_BELOW_THRESHOLD', 'SIGNAL_DATA_STALE',
        'SIGNAL_DATA_INVALID',
    }),
    ('extension_pass', {'ENTRY_OVEREXTENDED'}),
    ('stop_pass', {'INVALID_STOP'}),
    ('expected_edge_pass', {'INSUFFICIENT_NET_EDGE'}),
    ('target_pass', {'INVALID_TARGET'}),
    ('scalp_rr_pass', {'SCALP_RR_BELOW_MINIMUM'}),
    ('overtrading_pass', OVERTRADING_REASONS),
    ('risk_approved', {'SCALP_RISK_REJECTED'}),
    ('pre_execution_passes', {
        'EPISODE_ALREADY_EXECUTED', 'MARKET_CLOSING', 'STALE_DATA',
        'LOW_RISK_REWARD', 'DUPLICATE_POSITION',
    }),
    ('portfolio_approved', {
        'MAX_POSITIONS', 'MAX_TRADES', 'DAILY_LOSS_LIMIT', 'RISK_REJECTED',
    }),
)


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _decision(value):
    if value is None:
        return None
    return value.to_dict() if hasattr(value, 'to_dict') else dict(value)


def _reasons(detail, quality):
    reasons = []
    raw = detail.get('reasons')
    if isinstance(raw, (list, tuple)):
        reasons.extend(str(item) for item in raw if item)
    reason = detail.get('reason')
    if reason and reason not in reasons:
        reasons.append(str(reason))
    if not reasons:
        if quality.get('provider_status') != 'OK':
            reasons.append('QUOTE_UNAVAILABLE')
        elif quality.get('quote_status') != 'FRESH':
            reasons.append('STALE_QUOTE')
    return tuple(dict.fromkeys(reasons))


def candidate_trace(*, cycle_id, symbol, quality, market_data, detail, now):
    """Build one observation without changing or recomputing a trade decision."""

    decision = _decision(detail.get('decision'))
    features = dict(decision.get('features', {})) if decision else {}
    reasons = _reasons(detail, quality)
    reason_set = set(reasons)
    setup_type = ((decision or {}).get('setup_type')
                  or detail.get('setup_type') or 'UNCLASSIFIED')
    setup_detected = setup_type != 'UNCLASSIFIED'

    flags = {'universe_observations': True}
    alive = quality.get('provider_status') == 'OK'
    flags['provider_ok'] = alive
    # A duplicate seen before signal evaluation is an early symbol guard. A
    # duplicate detected after an entry attempt is a pre-execution race check.
    alive = alive and not (
        'DUPLICATE_POSITION' in reason_set and not detail.get('entry_attempted')
    )
    flags['duplicate_symbol_pass'] = alive
    alive = alive and not reason_set.intersection({
        'SCALP_DISABLED', 'SCALP_SHADOW_ONLY', 'SCALP_SAFETY_FLAGS_INVALID',
        'SCALP_KILL_SWITCH_NOT_BLOCKED',
    })
    flags['runtime_safety_pass'] = alive
    alive = alive and quality.get('quote_status') == 'FRESH' and 'STALE_QUOTE' not in reason_set
    flags['quote_fresh'] = alive
    alive = alive and 'MARKET_CLOSED' not in reason_set
    flags['market_open_pass'] = alive
    alive = alive and 'INVALID_BID_ASK' not in reason_set
    flags['valid_bid_ask'] = alive
    alive = alive and 'SPREAD_TOO_WIDE' not in reason_set
    flags['spread_pass'] = alive
    flags['execution_liquidity_pass'] = alive
    alive = alive and not reason_set.intersection({
        'INSUFFICIENT_COMPLETED_MICRO_BARS', 'MICRO_BARS_STALE',
        'MICRO_BARS_UNAVAILABLE', 'MICRO_BAR_REFRESH_FAILED',
    })
    flags['micro_bars_pass'] = alive
    alive = alive and not reason_set.intersection({
        'VOLUME_EXPANSION_BELOW_MINIMUM', 'VOLUME_DATA_STALE',
        'VOLUME_DATA_UNAVAILABLE',
    })
    flags['volume_expansion_pass'] = alive
    # Read-compatible counter name for older reports.  It is deliberately not
    # printed as execution liquidity.
    flags['liquidity_pass'] = alive
    alive = alive and setup_detected and 'UNCLASSIFIED_SETUP' not in reason_set
    flags['eligible_micro_signals'] = alive
    alive = alive and 'STALE_SCALP_EPISODE' not in reason_set
    flags['episode_open_pass'] = alive
    alive = alive and not reason_set.intersection({
        'SIGNAL_SCORE_BELOW_THRESHOLD', 'SIGNAL_DATA_STALE',
        'SIGNAL_DATA_INVALID',
    })
    flags['signal_score_pass'] = alive
    alive = alive and 'ENTRY_OVEREXTENDED' not in reason_set
    flags['extension_pass'] = alive
    alive = alive and 'INVALID_STOP' not in reason_set
    flags['stop_pass'] = alive
    alive = alive and 'INSUFFICIENT_NET_EDGE' not in reason_set
    flags['expected_edge_pass'] = alive
    alive = alive and 'INVALID_TARGET' not in reason_set
    flags['target_pass'] = alive
    alive = alive and 'SCALP_RR_BELOW_MINIMUM' not in reason_set
    flags['scalp_rr_pass'] = alive
    flags['geometry_pass'] = alive
    alive = alive and not reason_set.intersection(OVERTRADING_REASONS)
    flags['overtrading_pass'] = alive
    flags['entry_attempts'] = bool(detail.get('entry_attempted'))
    flags['risk_attempts'] = bool(detail.get('risk_attempted'))
    flags['risk_approved'] = bool(detail.get('risk_approved'))
    flags['pre_execution_attempts'] = bool(detail.get('pre_execution_attempted'))
    flags['pre_execution_passes'] = bool(detail.get('pre_execution_passed'))
    flags['portfolio_attempts'] = bool(detail.get('portfolio_attempted'))
    flags['portfolio_approved'] = bool(detail.get('portfolio_approved'))
    flags['safety_approved'] = bool(detail.get('safety_approved'))
    flags['entries'] = detail.get('status') == 'OPENED'

    blocking_stage = None
    blocking_reasons = []
    for stage, stage_reasons in STAGE_REASONS:
        # DUPLICATE_POSITION has two distinct production checks.
        if stage == 'duplicate_symbol_pass' and detail.get('entry_attempted'):
            continue
        if stage == 'pre_execution_passes' and not detail.get('entry_attempted'):
            continue
        matches = [reason for reason in reasons if reason in stage_reasons]
        if matches:
            blocking_stage, blocking_reasons = stage, matches
            break
    if blocking_stage is None and reasons:
        if any(reason.startswith('SAFETY_OVERRIDE:') for reason in reasons):
            blocking_stage = 'safety_approved'
        else:
            blocking_stage = 'unmapped_runtime_block'
        blocking_reasons = list(reasons)
    rejection_class = (
        'FILTERED' if blocking_stage in EARLY_FILTER_STAGES
        else 'ENTRY_BLOCKED' if reasons else None
    )

    entry = _finite(decision.get('entry_price')) if decision else None
    spread_cost = _finite(features.get('spread_pct'))
    observed_spread = _finite(quality.get('spread_pct'))
    signal_score = _finite((decision or {}).get('signal_score'))
    raw_components = features.get('score_breakdown', {})
    components = {}
    if isinstance(raw_components, dict):
        for name, value in raw_components.items():
            row = dict(value) if isinstance(value, dict) else {}
            contribution = _finite(row.get('contribution'))
            row.update({
                'penalty': (-contribution if contribution is not None and contribution < 0 else 0.0),
                'clamp': (
                    'MAX' if row.get('clamped_max') else
                    'MIN' if row.get('clamped_min') else 'NONE'
                ),
                'final_contribution': contribution,
            })
            components[name] = row
    contributions = [
        _finite(row.get('contribution')) for row in components.values()
    ]
    score_before_penalties = sum(
        value for value in contributions if value is not None and value >= 0
    )
    total_penalties = sum(
        -value for value in contributions if value is not None and value < 0
    )
    expected_net_edge = _finite((decision or {}).get('expected_net_edge_pct'))
    entry_slippage = config.SCALP_ENTRY_SLIPPAGE_BPS / 10_000
    exit_slippage = config.SCALP_EXIT_SLIPPAGE_BPS / 10_000
    other_friction = (2 * config.SCALP_COMMISSION_PER_SHARE / entry) if entry else 0.0
    relative_volume = _finite(market_data.get('relative_volume'))
    volume_source = market_data.get('relative_volume_source') or (
        'UNAVAILABLE' if relative_volume is None else 'UNSPECIFIED'
    )
    micro = features.get('micro_bar_freshness') if isinstance(
        features.get('micro_bar_freshness'), dict) else None
    if micro is None:
        micro = micro_bar_freshness(
            market_data.get('candles', []) or [], now=now,
            provider_status=market_data.get(
                'micro_bar_provider_status',
                'OK' if market_data.get('candles') else 'UNAVAILABLE',
            ),
        ).to_dict()
    if 'VOLUME_DATA_STALE' in reason_set:
        volume_status = 'DATA_STALE'
    elif relative_volume is None or 'VOLUME_DATA_UNAVAILABLE' in reason_set:
        volume_status = 'UNAVAILABLE'
    elif relative_volume >= config.SCALP_MIN_VOLUME_EXPANSION:
        volume_status = 'PASS'
    else:
        volume_status = 'INSUFFICIENT'
    volume_provenance = features.get('feature_provenance', {}).get(
        'volume_expansion', features.get('feature_provenance', {}).get(
            'relative_volume', {}
        )
    ) if isinstance(features.get('feature_provenance'), dict) else {}

    final = (
        'SHADOW_ENTRY' if flags['entries']
        else 'FILTERED' if rejection_class == 'FILTERED'
        else 'BLOCKED' if reasons
        else 'NO_ENTRY'
    )
    return {
        'cycle_id': cycle_id,
        'strategy_id': 'SCALP',
        'timestamp': now.astimezone(timezone.utc).isoformat(),
        'symbol': symbol.upper(),
        'episode_id': detail.get('episode_id') or (decision or {}).get('episode_id'),
        'setup_type': setup_type,
        'setup_evidence': list((decision or {}).get(
            'setup_evidence', detail.get('setup_evidence', [])
        )),
        'setup_detected_anywhere': setup_detected,
        'episode_lifecycle': dict(detail.get('episode_lifecycle', {})),
        'latency': dict(detail.get('latency', {})),
        'stage_flags': flags,
        'rejection_reasons': list(reasons),
        'blocking_stage': blocking_stage,
        'blocking_reasons': blocking_reasons,
        'rejection_class': rejection_class,
        'freshness': {
            'provider_status': quality.get('provider_status', 'UNAVAILABLE'),
            'quote_status': quality.get('quote_status', 'UNAVAILABLE'),
            'quote_age_seconds': _finite(quality.get('quote_age_seconds')),
            'maximum_age_seconds': config.SCALP_MAX_QUOTE_AGE_SECONDS,
            'passed_in_funnel': flags['quote_fresh'],
            'provenance': dict(quality.get('provenance', {})),
        },
        'spread': {
            'observed_pct': observed_spread,
            'maximum_pct': config.SCALP_MAX_SPREAD_PCT,
            'passed': (observed_spread is not None
                       and observed_spread <= config.SCALP_MAX_SPREAD_PCT),
            'passed_in_funnel': flags['spread_pass'],
        },
        'execution_liquidity': {
            'inputs': ['bid', 'ask', 'spread', 'quote_age', 'quote_quality'],
            'status': 'PASS' if flags['execution_liquidity_pass'] else 'FAIL',
            'spread_pct': observed_spread,
            'quote_status': quality.get('quote_status', 'UNAVAILABLE'),
            'passed_in_funnel': flags['execution_liquidity_pass'],
        },
        'volume_expansion': {
            'input': 'relative_volume', 'legacy_field_alias': 'relative_volume',
            'observed': relative_volume,
            'current_volume': _finite(volume_provenance.get('current_volume')),
            'baseline_volume': _finite(volume_provenance.get('baseline_volume')),
            'baseline_bar_count': volume_provenance.get('baseline_bar_count'),
            'bar_timeframe_seconds': _finite(volume_provenance.get('bar_timeframe_seconds')),
            'formula': volume_provenance.get('formula'),
            'source_timestamp': volume_provenance.get('source_timestamp'),
            'freshness_status': volume_provenance.get('status'),
            'minimum': config.SCALP_MIN_VOLUME_EXPANSION,
            'status': volume_status, 'source': volume_source,
            'passed': volume_status == 'PASS',
            'passed_in_funnel': flags['volume_expansion_pass'],
            'volume_from_quotes': False,
        },
        'micro_bars': {
            **micro,
            'passed_in_funnel': flags['micro_bars_pass'],
        },
        'liquidity': {
            'legacy_alias_for': 'volume_expansion',
            'input': 'relative_volume', 'observed': relative_volume,
            'current_volume': _finite(volume_provenance.get('current_volume')),
            'baseline_volume': _finite(volume_provenance.get('baseline_volume')),
            'baseline_bar_count': volume_provenance.get('baseline_bar_count'),
            'bar_timeframe_seconds': _finite(
                volume_provenance.get('bar_timeframe_seconds')
            ),
            'completed_bars_only': volume_provenance.get('completed_bars_only'),
            'same_time_of_day_normalized': volume_provenance.get(
                'same_time_of_day_normalized'
            ),
            'formula': volume_provenance.get('formula'),
            'source_timestamp': volume_provenance.get('source_timestamp'),
            'freshness_status': volume_provenance.get('status'),
            'minimum': config.SCALP_MIN_VOLUME_EXPANSION,
            'status': volume_status, 'source': volume_source,
            'passed': volume_status == 'PASS',
            'passed_in_funnel': flags['liquidity_pass'],
            'fallback_used': volume_source == 'COMPLETED_MICRO_BAR_RATIO',
        },
        'signal': {
            'score': signal_score,
            'score_margin': (signal_score - config.SCALP_MIN_SIGNAL_SCORE
                             if signal_score is not None else None),
            'data_status': features.get('signal_data_status', 'UNAVAILABLE'),
            'feature_provenance': features.get('feature_provenance', {}),
            'components': components,
            'score_before_penalties': score_before_penalties,
            'total_penalties': total_penalties,
            'minimum': config.SCALP_MIN_SIGNAL_SCORE,
            'passed': (signal_score is not None
                       and signal_score >= config.SCALP_MIN_SIGNAL_SCORE),
            'passed_in_funnel': flags['signal_score_pass'],
        },
        'friction': {
            'expected_move_pct': _finite((decision or {}).get('expected_move_pct')),
            'spread_cost_pct': spread_cost,
            'entry_slippage_pct': entry_slippage,
            'exit_slippage_pct': exit_slippage,
            'other_friction_pct': other_friction,
            'estimated_round_trip_cost_pct': _finite((decision or {}).get('estimated_cost_pct')),
            'expected_net_edge_pct': expected_net_edge,
            'minimum_net_edge_pct': config.SCALP_MIN_EXPECTED_NET_EDGE,
            'passed': (expected_net_edge is not None
                       and expected_net_edge >= config.SCALP_MIN_EXPECTED_NET_EDGE),
            'passed_in_funnel': flags['expected_edge_pass'],
        },
        'geometry': {
            'entry': entry, 'stop': _finite((decision or {}).get('stop')),
            'target': _finite((decision or {}).get('target')),
            'entry_extension_pct': _finite(features.get('entry_extension')),
            'entry_extension_threshold_pct': config.SCALP_MAX_EXTENSION_PCT,
            'entry_extension_reference': dict(
                features.get('entry_extension_reference', {})
            ),
            'risk_pct': _finite((decision or {}).get('risk_pct')),
            'reward_pct': _finite((decision or {}).get('reward_pct')),
            'gross_rr': _finite((decision or {}).get('risk_reward_ratio')),
            'estimated_cost_pct': _finite((decision or {}).get('estimated_cost_pct')),
            'net_reward_pct': _finite((decision or {}).get('net_reward_pct')),
            'net_rr': _finite((decision or {}).get('net_risk_reward_ratio')),
            'minimum_gross_rr': config.SCALP_MIN_RISK_REWARD,
            'structural_evidence': list((decision or {}).get(
                'setup_evidence', detail.get('setup_evidence', [])
            )),
            'structural_levels': {
                'recent_low': _finite(features.get('recent_low')),
                'vwap': _finite(features.get('vwap')),
                'ema9': _finite(features.get('ema9')),
                'selected_stop': _finite((decision or {}).get('stop')),
            },
            'passed_in_funnel': flags['geometry_pass'],
            'structurally_valid': (
                not reason_set.intersection({
                    'INVALID_STOP', 'INVALID_TARGET', 'SCALP_RR_BELOW_MINIMUM',
                    'LOW_RISK_REWARD',
                })
                and
                entry is not None
                and _finite((decision or {}).get('stop')) is not None
                and _finite((decision or {}).get('target')) is not None
                and _finite((decision or {}).get('risk_pct')) is not None
                and _finite((decision or {}).get('risk_pct'))
                    >= config.SCALP_MIN_STOP_DISTANCE_PCT
                and _finite((decision or {}).get('risk_reward_ratio')) is not None
                and _finite((decision or {}).get('risk_reward_ratio'))
                    >= config.SCALP_MIN_RISK_REWARD
            ),
            'rejection_reasons': [reason for reason in reasons if reason in {
                'INVALID_STOP', 'INVALID_TARGET', 'SCALP_RR_BELOW_MINIMUM',
                'LOW_RISK_REWARD',
            }],
        },
        'risk': {
            'attempted': flags['risk_attempts'], 'approved': flags['risk_approved'],
            'details': detail.get('risk'),
        },
        'pre_execution': {
            'attempted': flags['pre_execution_attempts'],
            'passed': flags['pre_execution_passes'],
            'failure_reason': (blocking_reasons[0]
                               if blocking_stage == 'pre_execution_passes'
                               and blocking_reasons else None),
        },
        'portfolio': {
            'attempted': flags['portfolio_attempts'],
            'approved': flags['portfolio_approved'],
            'reasons': list(detail.get('portfolio_reasons', [])),
        },
        'safety': {
            'approved': flags['safety_approved'],
            'reasons': list(detail.get('safety_reasons', [])),
        },
        'market_observation': {
            'price': _finite(features.get('price')),
            'realized_volatility': _finite(features.get('realized_volatility')),
        },
        'final': final,
    }


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def cycle_diagnostics(*, cycle_id, traces, source, now, exits=0, refresh=None,
                      loop_timing=None, position_lifecycle=None):
    funnel = {
        name: sum(bool(trace['stage_flags'].get(name)) for trace in traces)
        for name in STAGE_NAMES
    }
    funnel['micro_signals_detected_anywhere'] = sum(
        trace['setup_detected_anywhere'] for trace in traces
    )
    funnel['pre_execution_failures'] = (
        funnel['pre_execution_attempts'] - funnel['pre_execution_passes']
    )
    funnel['risk_rejected'] = funnel['risk_attempts'] - funnel['risk_approved']
    funnel['portfolio_blocked'] = funnel['portfolio_attempts'] - funnel['portfolio_approved']
    reasons = Counter(
        reason for trace in traces for reason in trace['rejection_reasons']
    )
    funnel['duplicate_exposure_blocked'] = reasons.get('DUPLICATE_POSITION', 0)
    filtered = Counter(
        reason for trace in traces if trace['rejection_class'] == 'FILTERED'
        for reason in trace['blocking_reasons']
    )
    blocked = Counter(
        reason for trace in traces if trace['rejection_class'] == 'ENTRY_BLOCKED'
        for reason in trace['blocking_reasons']
    )
    pre_execution_failures = Counter(
        reason for trace in traces
        if trace['stage_flags']['pre_execution_attempts']
        and not trace['stage_flags']['pre_execution_passes']
        for reason in trace['blocking_reasons']
    )
    risk_rejections = Counter(
        reason for trace in traces
        if trace['stage_flags']['risk_attempts']
        and not trace['stage_flags']['risk_approved']
        for reason in trace['blocking_reasons']
    )
    portfolio_blocks = Counter(
        reason for trace in traces
        if trace['stage_flags']['portfolio_attempts']
        and not trace['stage_flags']['portfolio_approved']
        for reason in trace['blocking_reasons']
    )
    setup = defaultdict(lambda: {
        'detected_anywhere': 0, 'eligible_after_early_gates': 0,
        'entry_attempts': 0, 'entries': 0,
    })
    for setup_type in SETUP_TYPES:
        if setup_type != 'UNCLASSIFIED':
            setup[setup_type]
    for trace in traces:
        if trace['setup_detected_anywhere']:
            row = setup[trace['setup_type']]
            row['detected_anywhere'] += 1
            row['eligible_after_early_gates'] += int(trace['stage_flags']['eligible_micro_signals'])
            row['entry_attempts'] += int(trace['stage_flags']['entry_attempts'])
            row['entries'] += int(trace['stage_flags']['entries'])
    ages = [trace['freshness']['quote_age_seconds'] for trace in traces
            if trace['freshness']['quote_age_seconds'] is not None
            and trace['freshness']['quote_age_seconds'] >= 0]
    spreads = [trace['spread']['observed_pct'] for trace in traces
               if trace['spread']['observed_pct'] is not None]
    bar_ages = [trace['micro_bars']['bar_age_seconds'] for trace in traces
                if trace['micro_bars']['bar_age_seconds'] is not None]
    bar_statuses = Counter(
        trace['micro_bars']['freshness_status'] for trace in traces
    )
    bar_timeframes = sorted({
        trace['micro_bars']['bar_timeframe_seconds'] for trace in traces
        if trace['micro_bars']['bar_timeframe_seconds'] is not None
    })
    refresh = refresh or {}
    lifecycle = list(position_lifecycle or [])
    return {
        'cycle_id': cycle_id, 'strategy_id': 'SCALP', 'source': source,
        'timestamp': now.astimezone(timezone.utc).isoformat(),
        'funnel': funnel, 'exits': exits,
        'rejection_reasons': dict(sorted(reasons.items())),
        'filtered_reasons': dict(sorted(filtered.items())),
        'entry_blocked_reasons': dict(sorted(blocked.items())),
        'pre_execution_failure_reasons': dict(sorted(pre_execution_failures.items())),
        'risk_rejection_reasons': dict(sorted(risk_rejections.items())),
        'portfolio_block_reasons': dict(sorted(portfolio_blocks.items())),
        'setup_types': dict(sorted(setup.items())),
        'quote_age': {
            'count': len(ages),
            'fresh': sum(
                trace['freshness']['provider_status'] == 'OK'
                and trace['freshness']['quote_status'] == 'FRESH'
                for trace in traces
            ),
            'stale': sum(trace['freshness']['quote_status'] == 'STALE' for trace in traces),
            'unavailable': sum(
                trace['freshness']['provider_status'] != 'OK'
                or trace['freshness']['quote_status'] in {'UNAVAILABLE', 'INVALID'}
                for trace in traces
            ),
            'mean_seconds': mean(ages) if ages else None,
            'median_seconds': median(ages) if ages else None,
            'p95_seconds': _percentile(ages, .95),
            'max_seconds': max(ages) if ages else None,
            'maximum_allowed_seconds': config.SCALP_MAX_QUOTE_AGE_SECONDS,
        },
        'micro_bar_status': {
            'provider_history_ok': sum(
                trace['micro_bars']['provider_status'] == 'OK' for trace in traces
            ),
            'fresh': bar_statuses.get('FRESH', 0),
            'aging': bar_statuses.get('AGING', 0),
            'stale': bar_statuses.get('STALE', 0),
            'unavailable': bar_statuses.get('UNAVAILABLE', 0),
            'age_median_seconds': median(bar_ages) if bar_ages else None,
            'age_p95_seconds': _percentile(bar_ages, .95),
            'age_max_seconds': max(bar_ages) if bar_ages else None,
            'timeframes_seconds': bar_timeframes,
            'bar_timeframe_seconds': bar_timeframes[0] if len(bar_timeframes) == 1 else None,
            'allowed_lag_seconds': config.SCALP_MAX_MICRO_BAR_AGE_SECONDS,
            'refresh_attempts': refresh.get('attempt_count', 0),
            'refresh_successes': refresh.get('success_count', 0),
            'refresh_success': refresh.get('success_count', 0),
            'refresh_unchanged': refresh.get('unchanged_count', 0),
            'refresh_failures': refresh.get('failure_count', 0),
            'refresh_latency_seconds': refresh.get('latency_seconds'),
            'refresh_started': bool(refresh.get('refresh_started')),
            'refresh_in_progress': bool(refresh.get('refresh_in_progress')),
            'refresh_completed': bool(refresh.get('refresh_completed')),
            'refresh_timing': refresh.get('timing', {}),
            'event_loop_blocking_duration_seconds': refresh.get(
                'event_loop_blocking_duration_seconds'
            ),
            'provider_metrics': refresh.get('provider_metrics', {}),
        },
        'fast_watcher_timing': dict(loop_timing or {}),
        'position_lifecycle': lifecycle,
        'position_lifecycle_counts': {
            'monitored': len(lifecycle),
            'overdue': sum(
                row.get('lifecycle_state') == 'OVERDUE_SCALP_POSITION'
                for row in lifecycle
            ),
            'exit_pending': sum(
                row.get('exit_status') == 'OVERDUE_EXIT_PENDING'
                for row in lifecycle
            ),
            'recovery_time_exits': sum(
                row.get('exit_status') == 'RECOVERY_TIME_EXIT'
                for row in lifecycle
            ),
        },
        'spread_distribution': {
            'count': len(spreads), 'median_pct': median(spreads) if spreads else None,
            'p90_pct': _percentile(spreads, .90),
            'max_pct': max(spreads) if spreads else None,
            'pass_count': funnel['spread_pass'],
            'failure_count': reasons.get('SPREAD_TOO_WIDE', 0),
            'pass_rate': funnel['spread_pass'] / funnel['valid_bid_ask']
            if funnel['valid_bid_ask'] else None,
        },
    }


class ScalpDiagnosticsJournal:
    def __init__(self, path):
        self.journal = EventJournal(path)

    def record(self, diagnostics, traces):
        for trace in traces:
            self.journal.append_row({
                'event_id': uuid4().hex, 'record_type': 'CANDIDATE_OBSERVATION',
                'strategy_id': 'SCALP', 'cycle_id': diagnostics['cycle_id'],
                'timestamp': diagnostics['timestamp'], 'symbol': trace['symbol'],
                'episode_id': trace['episode_id'], 'payload': trace,
            })
        self.journal.append_row({
            'event_id': uuid4().hex, 'record_type': 'DISCOVERY_CYCLE',
            'strategy_id': 'SCALP', 'cycle_id': diagnostics['cycle_id'],
            'timestamp': diagnostics['timestamp'], 'symbol': None,
            'episode_id': None, 'payload': diagnostics,
        })
