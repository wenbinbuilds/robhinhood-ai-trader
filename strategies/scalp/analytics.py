"""Strategy-attributed performance and friction diagnostics."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import config
from strategies.scalp.diagnostics import STAGE_NAMES
from strategies.scalp.signals import SETUP_TYPES
from strategies.identity import strategy_of


def scalp_summary(trades):
    rows = [_row(t) for t in trades if strategy_of(_row(t)) == 'SCALP']
    pnl = [r.get('net_pnl', 0) or 0 for r in rows]
    gross = [r.get('gross_pnl', 0) or 0 for r in rows]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    rs = [(r.get('r_multiple') if r.get('r_multiple') is not None else
           r.get('net_pnl')/r.get('risk_allocated') if r.get('risk_allocated') else None)
          for r in rows]
    rs = [r for r in rs if r is not None]
    holds = [r.get('holding_time_seconds',
                   r.get('holding_seconds', (r.get('holding_time_minutes') or 0)*60))
             for r in rows]
    normal_time_exits = sum(r.get('exit_reason') == 'SCALP_TIME_EXIT' for r in rows)
    recovery_time_exits = sum(
        r.get('exit_reason') == 'SCALP_RECOVERY_TIME_EXIT' for r in rows
    )
    exceeded = [
        r for r, hold in zip(rows, holds)
        if hold > float(r.get('configured_max_hold_seconds') or config.SCALP_MAX_HOLD_SECONDS)
    ]
    overdue = [
        max(0.0, hold-float(r.get('configured_max_hold_seconds')
                            or config.SCALP_MAX_HOLD_SECONDS))
        for r, hold in zip(rows, holds)
    ]
    overdue = [value for value in overdue if value > 0]
    costs = [(r.get('estimated_spread_cost', r.get('spread_cost', 0)) or 0)
             +(r.get('estimated_slippage_cost', r.get('slippage_cost', 0)) or 0) for r in rows]
    equity = peak = drawdown = 0.0
    for value in pnl: equity += value; peak=max(peak,equity); drawdown=max(drawdown,peak-equity)
    hours = _elapsed_hours(rows)
    return {'strategy_id': 'SCALP', 'trades': len(rows), 'wins': len(wins),
        'losses': len(losses), 'win_rate': len(wins)/len(rows) if rows else None,
        'gross_pnl': sum(gross), 'net_pnl': sum(pnl),
        'average_trade_return': mean([r.get('return_percent',0) for r in rows]) if rows else None,
        'average_r': mean(rs) if rs else None, 'expectancy': mean(rs) if rs else None,
        'profit_factor': sum(wins)/abs(sum(losses)) if losses else None,
        'maximum_drawdown': drawdown, 'average_holding_seconds': mean(holds) if holds else None,
        'median_holding_seconds': median(holds) if holds else None,
        'p90_holding_seconds': _percentile(holds, .90),
        'maximum_holding_seconds': max(holds) if holds else None,
        'normal_time_exits': normal_time_exits,
        'recovery_time_exits': recovery_time_exits,
        'overdue_positions_seen': sum(
            bool(r.get('crossed_max_hold_at'))
            or hold >= float(r.get('configured_max_hold_seconds')
                             or config.SCALP_MAX_HOLD_SECONDS)
            for r, hold in zip(rows, holds)
        ),
        'max_overdue_seconds': max(overdue) if overdue else 0.0,
        'positions_exceeding_configured_max': len(exceeded),
        'positions_exceeding_max_quote_unavailable': sum(
            bool({'QUOTE_UNAVAILABLE', 'STALE_QUOTE'}.intersection(
                r.get('max_hold_delay_reasons', []) or []
            )) for r in exceeded
        ),
        'positions_exceeding_max_watcher_scheduling_delay': sum(
            'WATCHER_SCHEDULING_DELAY' in (r.get('max_hold_delay_reasons', []) or [])
            for r in exceeded
        ),
        'average_spread_cost': mean([r.get('estimated_spread_cost',r.get('spread_cost',0)) or 0 for r in rows]) if rows else None,
        'average_slippage_cost': mean([r.get('estimated_slippage_cost',r.get('slippage_cost',0)) or 0 for r in rows]) if rows else None,
        'cost_as_percent_of_gross_profits': (100*sum(costs)/sum(x for x in gross if x > 0)
                                             if any(x > 0 for x in gross) else None),
        'stop_rate': _rate(rows,'STOP_HIT'), 'target_rate': _rate(rows,'TARGET_HIT'),
        'time_exit_rate': _rate(rows,'SCALP_TIME_EXIT'),
        'momentum_exit_rate': _rate(rows,'MOMENTUM_REVERSAL'),
        'trades_per_hour': len(rows)/hours if hours else None,
        'by_spread_bucket': grouped(rows, spread_bucket),
        'by_holding_time': grouped(rows, holding_bucket),
        'by_time_of_day': grouped(rows, _time_period_row),
        'by_regime': grouped(rows, lambda r: r.get('market_regime','UNKNOWN')),
        'by_setup_type': grouped(rows, lambda r: r.get('setup_type','UNCLASSIFIED')),
        'by_relative_strength_spy': grouped(rows, lambda r: _strength(r.get('relative_strength_spy'))),
        'by_relative_strength_qqq': grouped(rows, lambda r: _strength(r.get('relative_strength_qqq'))),
        'by_relative_strength_sector': grouped(rows, lambda r: _strength(r.get('relative_strength_sector')))}


def friction_sensitivity(trades, assumptions=(('low',1.0),('base',2.5),('high',5.0))):
    rows = [_row(t) for t in trades]
    result = {}
    for name, bps in assumptions:
        values = []
        for row in rows:
            entry = row.get('quoted_entry_ask') or row.get('entry_price')
            exit_bid = row.get('quoted_exit_bid') or row.get('exit_price')
            if entry and exit_bid:
                values.append(exit_bid*(1-bps/10_000)-entry*(1+bps/10_000))
        expectancy = mean(values) if values else None
        result[name] = {'slippage_bps_each_side': bps, 'trades': len(values),
                        'net_expectancy_per_share': expectancy}
    base = result.get('base',{}).get('net_expectancy_per_share')
    high = result.get('high',{}).get('net_expectancy_per_share')
    result['fragile'] = bool(base is not None and base > 0 and (high is None or high <= 0))
    return result


class _StrategyAttribution(dict):
    """Two user-facing keys with a read-only legacy MOMENTUM lookup alias."""

    def __getitem__(self, key):
        return super().__getitem__('POSITION' if key == 'MOMENTUM' else key)

    def get(self, key, default=None):
        return super().get('POSITION' if key == 'MOMENTUM' else key, default)


def strategy_attribution(portfolio):
    state = portfolio.snapshot()
    result = _StrategyAttribution()
    for display_name in ('POSITION', 'SCALP'):
        open_rows = [p for p in state.open_positions if strategy_of(p) == display_name]
        closed = [t for t in state.closed_positions if strategy_of(t) == display_name]
        result[display_name] = {
            'strategy_display_name': display_name,
            'capital_allocated': sum(p.capital_allocated or p.notional_value for p in open_rows),
            'risk_allocated': sum(p.risk_allocated or p.maximum_theoretical_loss for p in open_rows),
            'realized_pnl': sum(t.net_pnl for t in closed),
            'unrealized_pnl': sum(p.unrealized_pnl for p in open_rows),
            'trade_count': len(closed), 'open_positions': len(open_rows)}
    return result


def scalp_session_summary(path, trades, *, now=None):
    """Aggregate persisted observations; performance remains SCALP-only."""
    current = now or datetime.now(timezone.utc)
    zone = ZoneInfo(config.MARKET_TIMEZONE)
    session_date = current.astimezone(zone).date()
    rows = []
    try:
        with Path(path).open() as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                stamp = datetime.fromisoformat(str(row.get('timestamp', '')).replace('Z', '+00:00'))
                if (row.get('strategy_id') == 'SCALP' and stamp.tzinfo is not None
                        and stamp.astimezone(zone).date() == session_date):
                    rows.append(row)
    except FileNotFoundError:
        pass
    cycles = [row['payload'] for row in rows if row.get('record_type') == 'DISCOVERY_CYCLE']
    observations = [row['payload'] for row in rows if row.get('record_type') == 'CANDIDATE_OBSERVATION']
    funnel = Counter({name: 0 for name in STAGE_NAMES})
    funnel.update({
        'micro_signals_detected_anywhere': 0,
        'pre_execution_failures': 0,
        'risk_rejected': 0,
        'portfolio_blocked': 0,
        'duplicate_exposure_blocked': 0,
    })
    setup_types = defaultdict(Counter)
    for setup_type in SETUP_TYPES:
        if setup_type != 'UNCLASSIFIED':
            setup_types[setup_type].update({
                'detected_anywhere': 0,
                'eligible_after_early_gates': 0,
                'entry_attempts': 0,
                'entries': 0,
            })
    reasons = Counter()
    filtered = Counter()
    blocked = Counter()
    pre_execution_failures = Counter()
    risk_rejections = Counter()
    portfolio_blocks = Counter()
    for cycle in cycles:
        funnel.update(cycle.get('funnel', {}))
        reasons.update(cycle.get('rejection_reasons', {}))
        filtered.update(cycle.get('filtered_reasons', {}))
        blocked.update(cycle.get('entry_blocked_reasons', {}))
        pre_execution_failures.update(cycle.get('pre_execution_failure_reasons', {}))
        risk_rejections.update(cycle.get('risk_rejection_reasons', {}))
        portfolio_blocks.update(cycle.get('portfolio_block_reasons', {}))
        for setup, counts in cycle.get('setup_types', {}).items():
            setup_types[setup].update({
                name: int(counts.get(name, 0))
                for name in (
                    'detected_anywhere', 'eligible_after_early_gates',
                    'entry_attempts', 'entries',
                )
            })

    def rate(count, denominator):
        return {'count': int(count), 'denominator': int(denominator),
                'percent': 100 * count / denominator if denominator else None}

    rates = {
        'provider_ok': rate(funnel['provider_ok'], funnel['universe_observations']),
        'duplicate_symbol_pass': rate(funnel['duplicate_symbol_pass'], funnel['provider_ok']),
        'runtime_safety_pass': rate(funnel['runtime_safety_pass'], funnel['duplicate_symbol_pass']),
        'fresh_quote': rate(funnel['quote_fresh'], funnel['runtime_safety_pass']),
        'market_open_pass': rate(funnel['market_open_pass'], funnel['quote_fresh']),
        'valid_bid_ask': rate(funnel['valid_bid_ask'], funnel['market_open_pass']),
        'spread_pass': rate(funnel['spread_pass'], funnel['valid_bid_ask']),
        'micro_bars_pass': rate(funnel['micro_bars_pass'], funnel['spread_pass']),
        'liquidity_pass': rate(funnel['liquidity_pass'], funnel['micro_bars_pass']),
        'eligible_micro_signal': rate(funnel['eligible_micro_signals'], funnel['liquidity_pass']),
        'episode_open_pass': rate(funnel['episode_open_pass'], funnel['eligible_micro_signals']),
        'signal_score_pass': rate(funnel['signal_score_pass'], funnel['episode_open_pass']),
        'extension_pass': rate(funnel['extension_pass'], funnel['signal_score_pass']),
        'stop_pass': rate(funnel['stop_pass'], funnel['extension_pass']),
        'expected_edge_pass': rate(funnel['expected_edge_pass'], funnel['stop_pass']),
        'target_pass': rate(funnel['target_pass'], funnel['expected_edge_pass']),
        'scalp_rr_pass': rate(funnel['scalp_rr_pass'], funnel['target_pass']),
        'geometry_pass': rate(funnel['geometry_pass'], funnel['scalp_rr_pass']),
        'overtrading_pass': rate(funnel['overtrading_pass'], funnel['geometry_pass']),
        'entry_attempt': rate(funnel['entry_attempts'], funnel['overtrading_pass']),
        'risk_attempt': rate(funnel['risk_attempts'], funnel['entry_attempts']),
        'risk_approval': rate(funnel['risk_approved'], funnel['risk_attempts']),
        'pre_execution_attempt': rate(funnel['pre_execution_attempts'], funnel['risk_approved']),
        'pre_execution_pass': rate(funnel['pre_execution_passes'], funnel['pre_execution_attempts']),
        'portfolio_attempt': rate(funnel['portfolio_attempts'], funnel['pre_execution_passes']),
        'portfolio_approval': rate(funnel['portfolio_approved'], funnel['portfolio_attempts']),
        'safety_approval': rate(funnel['safety_approved'], funnel['portfolio_approved']),
        'shadow_entry': rate(funnel['entries'], funnel['safety_approved']),
    }
    ages = [item['freshness']['quote_age_seconds'] for item in observations
            if item.get('freshness', {}).get('quote_age_seconds') is not None
            and item['freshness']['quote_age_seconds'] >= 0]
    spreads = [item['spread']['observed_pct'] for item in observations
               if item.get('spread', {}).get('observed_pct') is not None]
    liquidity_values = [item.get('liquidity', {}).get('observed') for item in observations]
    liquidity_values = [float(value) for value in liquidity_values if value is not None]
    score_values = [item.get('signal', {}).get('score') for item in observations]
    score_values = [float(value) for value in score_values if value is not None]
    eligible_observations = [
        item for item in observations
        if item.get('stage_flags', {}).get('eligible_micro_signals')
    ]
    eligible_score_values = [
        float(item['signal']['score']) for item in eligible_observations
        if item.get('signal', {}).get('score') is not None
    ]
    refresh_cycle_ids = {
        cycle.get('cycle_id') for cycle in cycles
        if (cycle.get('micro_bar_status', {}).get('refresh_attempts', 0)
            or cycle.get('micro_bar_status', {}).get('refresh_in_progress')
            or cycle.get('micro_bar_status', {}).get('refresh_completed'))
    }
    normal_observations = [item for item in observations
                           if item.get('cycle_id') not in refresh_cycle_ids]
    refresh_observations = [item for item in observations
                            if item.get('cycle_id') in refresh_cycle_ids]

    by_symbol = {}
    for symbol in sorted({item.get('symbol') for item in observations if item.get('symbol')}):
        group = [item for item in observations if item.get('symbol') == symbol]
        rv = [item.get('liquidity', {}).get('observed') for item in group]
        rv = [float(value) for value in rv if value is not None]
        scores = [item.get('signal', {}).get('score') for item in group]
        scores = [float(value) for value in scores if value is not None]
        prices = [item.get('market_observation', {}).get('price')
                  or item.get('geometry', {}).get('entry') for item in group]
        prices = [float(value) for value in prices if value is not None]
        volatilities = [item.get('market_observation', {}).get('realized_volatility')
                        for item in group]
        volatilities = [float(value) for value in volatilities if value is not None]
        by_symbol[symbol] = {
            'observations': len(group),
            'price_median': median(prices) if prices else None,
            'spread_median_pct': _percentile([
                float(item['spread']['observed_pct']) for item in group
                if item.get('spread', {}).get('observed_pct') is not None
            ], .5),
            'relative_volume_median': median(rv) if rv else None,
            'relative_volume_p90': _percentile(rv, .90),
            'liquidity_pass_count': sum(
                item.get('liquidity', {}).get('status') == 'PASS' for item in group
            ),
            'liquidity_pass_rate': sum(
                item.get('liquidity', {}).get('status') == 'PASS' for item in group
            ) / len(group) if group else None,
            'setup_detections': sum(item.get('setup_detected_anywhere') for item in group),
            'eligible_signals': sum(item.get('stage_flags', {}).get(
                'eligible_micro_signals') for item in group),
            'signal_score_distribution': _distribution(scores),
            'realized_volatility_median': median(volatilities) if volatilities else None,
        }

    by_setup = {}
    for setup_type in SETUP_TYPES:
        group = [item for item in observations if item.get('setup_type') == setup_type]
        scores = [item.get('signal', {}).get('score') for item in group]
        scores = [float(value) for value in scores if value is not None]
        by_setup[setup_type] = {
            'detected': sum(item.get('setup_detected_anywhere') for item in group),
            'eligible': sum(item.get('stage_flags', {}).get('eligible_micro_signals')
                            for item in group),
            'mean_score': mean(scores) if scores else None,
            'median_score': median(scores) if scores else None,
            'p90_score': _percentile(scores, .90),
            'max_score': max(scores) if scores else None,
            'at_or_above_0_70': sum(value >= config.SCALP_MIN_SIGNAL_SCORE
                                    for value in scores),
            'entry_attempts': sum(item.get('stage_flags', {}).get('entry_attempts')
                                  for item in group),
        }

    eligible_by_setup = {}
    for setup_type in SETUP_TYPES:
        detected = [item for item in observations
                    if item.get('setup_type') == setup_type]
        group = [item for item in detected
                 if item.get('stage_flags', {}).get('eligible_micro_signals')]
        scores = [float(item['signal']['score']) for item in group
                  if item.get('signal', {}).get('score') is not None]
        eligible_by_setup[setup_type] = {
            'detected': sum(item.get('setup_detected_anywhere') for item in detected),
            'eligible': len(group),
            'average_score': mean(scores) if scores else None,
            'median_score': median(scores) if scores else None,
            'max_score': max(scores) if scores else None,
            'percent_at_or_above_0_70': (
                100 * sum(value >= config.SCALP_MIN_SIGNAL_SCORE for value in scores)
                / len(scores) if scores else None
            ),
        }

    component_rows = defaultdict(list)
    for item in observations:
        components = item.get('signal', {}).get('components') or _legacy_components(item)
        for name, component in components.items():
            component_rows[name].append(component)
    component_analysis = {}
    for name, group in sorted(component_rows.items()):
        raw = [float(row['raw']) for row in group if row.get('raw') is not None]
        normalized = [float(row['normalized']) for row in group
                      if row.get('normalized') is not None]
        contribution = [float(row['contribution']) for row in group
                        if row.get('contribution') is not None]
        component_analysis[name] = {
            'observations': len(group),
            'mean_raw': mean(raw) if raw else None,
            'mean_normalized': mean(normalized) if normalized else None,
            'mean_weighted_contribution': mean(contribution) if contribution else None,
            'contribution_variance': _variance(contribution),
            'clamped_min_percent': 100*sum(bool(row.get('clamped_min')) for row in group)/len(group),
            'clamped_max_percent': 100*sum(bool(row.get('clamped_max')) for row in group)/len(group),
            'missing_percent': 100*sum(bool(row.get('missing')) for row in group)/len(group),
            'fallback_percent': 100*sum(bool(row.get('fallback_used')) for row in group)/len(group),
        }

    eligible_score_rejections = [
        item for item in observations
        if item.get('stage_flags', {}).get('eligible_micro_signals')
        and 'SIGNAL_SCORE_BELOW_THRESHOLD' in item.get('rejection_reasons', [])
    ]
    eligible_near_misses = [
        _eligible_detail(item) for item in eligible_score_rejections
    ]
    top_rejected = sorted(
        eligible_near_misses,
        key=lambda item: (
            -(item.get('score') if item.get('score') is not None else float('-inf')),
            item.get('timestamp') or '', item.get('symbol') or '',
        ),
    )[:20]
    momentum_bursts = [
        item for item in eligible_near_misses
        if item.get('setup_type') == 'MOMENTUM_BURST'
    ]
    margins = [
        (item.get('signal', {}).get('score_margin')
         if item.get('signal', {}).get('score_margin') is not None
         else float(item['signal']['score'])-config.SCALP_MIN_SIGNAL_SCORE
         if item.get('signal', {}).get('score') is not None else None)
        for item in eligible_score_rejections
    ]
    margins = [float(value) for value in margins if value is not None]
    counterfactuals = {'relative_volume': {}, 'signal_score': {}}
    for threshold in (1.20, 1.10, 1.00):
        group = [
            item for item in observations
            if item.get('stage_flags', {}).get('micro_bars_pass')
            and item.get('setup_detected_anywhere')
            and item.get('liquidity', {}).get('observed') is not None
            and item['liquidity']['observed'] >= threshold
        ]
        counterfactuals['relative_volume'][f'{threshold:.2f}'] = _quality_summary(group)
    for threshold in (.70, .65, .60):
        group = [
            item for item in observations
            if item.get('stage_flags', {}).get('eligible_micro_signals')
            and item.get('signal', {}).get('score') is not None
            and item['signal']['score'] >= threshold
        ]
        counterfactuals['signal_score'][f'{threshold:.2f}'] = _quality_summary(group)
    closed = []
    for trade in trades:
        row = _row(trade)
        if strategy_of(row) != 'SCALP':
            continue
        stamp_value = row.get('exit_timestamp', row.get('exit_time'))
        try:
            stamp = datetime.fromisoformat(str(stamp_value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is not None and stamp.astimezone(zone).date() == session_date:
            closed.append(row)
    lifecycle_rows = [
        item for cycle in cycles for item in cycle.get('position_lifecycle', [])
    ]
    overdue_by_episode = {
        item.get('episode_id'): item for item in lifecycle_rows
        if item.get('episode_id')
        and (item.get('lifecycle_state') == 'OVERDUE_SCALP_POSITION'
             or float(item.get('overdue_by_seconds') or 0) > 0)
    }
    session_holds = [
        float(item.get('holding_time_seconds', item.get(
            'holding_seconds', (item.get('holding_time_minutes') or 0)*60
        ))) for item in closed
    ]
    exceeding = [
        item for item, hold in zip(closed, session_holds)
        if hold > float(item.get('configured_max_hold_seconds')
                        or config.SCALP_MAX_HOLD_SECONDS)
    ]
    for item, hold in zip(closed, session_holds):
        if (item.get('crossed_max_hold_at')
                or hold >= float(item.get('configured_max_hold_seconds')
                                 or config.SCALP_MAX_HOLD_SECONDS)):
            overdue_by_episode.setdefault(item.get('episode_id'), item)
    lifecycle_max_overdue = max(
        [float(item.get('overdue_by_seconds') or 0) for item in lifecycle_rows]
        + [max(0.0, hold-float(item.get('configured_max_hold_seconds')
                               or config.SCALP_MAX_HOLD_SECONDS))
           for item, hold in zip(closed, session_holds)]
        + [0.0]
    )
    by_episode = {row.get('episode_id'): row for row in closed}
    entries = []
    for item in observations:
        if item.get('final') != 'SHADOW_ENTRY':
            continue
        trade = by_episode.get(item.get('episode_id'), {})
        entries.append({
            'symbol': item.get('symbol'), 'episode_id': item.get('episode_id'),
            'setup_type': item.get('setup_type'),
            'expected_net_edge_pct': item.get('friction', {}).get('expected_net_edge_pct'),
            'spread_pct': item.get('spread', {}).get('observed_pct'),
            'entry_slippage_bps': config.SCALP_ENTRY_SLIPPAGE_BPS,
            'exit_slippage_bps': config.SCALP_EXIT_SLIPPAGE_BPS,
            'holding_seconds': trade.get(
                'holding_time_seconds', trade.get('holding_seconds')
            ),
            'exit_reason': trade.get('exit_reason'),
            'gross_pnl': trade.get('gross_pnl'),
            'friction': ((trade.get('estimated_spread_cost', 0) or 0)
                         + (trade.get('estimated_slippage_cost', 0) or 0)) if trade else None,
            'net_pnl': trade.get('net_pnl'),
        })
    return {
        'strategy_id': 'SCALP', 'session_date': session_date.isoformat(),
        'cycles': len(cycles), 'candidate_observations': len(observations),
        'unique_symbols': len({item.get('symbol') for item in observations if item.get('symbol')}),
        'unique_episodes': len({item.get('episode_id') for item in observations if item.get('episode_id')}),
        'funnel': dict(funnel), 'rates': rates,
        'rejection_reasons_ranked': [
            {'reason': reason, 'observations': count}
            for reason, count in reasons.most_common()
        ],
        'filtered_reasons': dict(filtered),
        'entry_blocked_reasons': dict(blocked),
        'pre_execution_failure_reasons': dict(pre_execution_failures),
        'risk_rejection_reasons': dict(risk_rejections),
        'portfolio_block_reasons': dict(portfolio_blocks),
        'setup_types': {
            setup: dict(counts) for setup, counts in sorted(setup_types.items())
        },
        'quote_age': {
            'count': len(ages),
            'fresh': sum(item.get('freshness', {}).get('quote_status') == 'FRESH'
                         for item in observations),
            'stale': sum(item.get('freshness', {}).get('quote_status') == 'STALE'
                         for item in observations),
            'unavailable': sum(
                item.get('freshness', {}).get('provider_status') != 'OK'
                or item.get('freshness', {}).get('quote_status') in {'UNAVAILABLE', 'INVALID'}
                for item in observations
            ),
            'mean_seconds': mean(ages) if ages else None,
            'median_seconds': median(ages) if ages else None,
            'p95_seconds': _percentile(ages, .95),
            'max_seconds': max(ages) if ages else None,
        },
        'quote_age_distribution': _age_distribution(ages),
        'quote_age_by_history_refresh': {
            'normal_cycles': _observation_age_summary(normal_observations),
            'history_refresh_cycles': _observation_age_summary(refresh_observations),
        },
        'history_refreshes': [
            {
                'cycle_id': cycle.get('cycle_id'), 'timestamp': cycle.get('timestamp'),
                **cycle.get('micro_bar_status', {}).get('refresh_timing', {}),
                'reported_latency_seconds': cycle.get('micro_bar_status', {}).get(
                    'refresh_latency_seconds'),
                'event_loop_blocking_duration_seconds': cycle.get(
                    'micro_bar_status', {}).get('event_loop_blocking_duration_seconds'),
                'fast_watcher_loop_delay_seconds': cycle.get(
                    'fast_watcher_timing', {}).get('fast_watcher_loop_delay_seconds'),
            }
            for cycle in cycles if cycle.get('cycle_id') in refresh_cycle_ids
        ],
        'relative_volume_distribution': _distribution(
            liquidity_values, thresholds=(.75, 1.0, 1.1, 1.2, 1.3, 1.5),
        ),
        'liquidity_by_symbol': by_symbol,
        'signal_score_distribution': _distribution(
            score_values, thresholds=(.40, .50, .60, .65, .70, .75, .80),
        ),
        'eligible_signal_score_distribution': _distribution(
            eligible_score_values,
            thresholds=(.50, .60, .65, .68, .70, .75),
        ),
        'score_components': component_analysis,
        'score_by_setup': by_setup,
        'eligible_score_by_setup': eligible_by_setup,
        'score_margin_buckets': {
            'below_-0_20': sum(value < -.20 for value in margins),
            '-0_20_to_-0_10': sum(-.20 <= value < -.10 for value in margins),
            '-0_10_to_-0_05': sum(-.10 <= value < -.05 for value in margins),
            '-0_05_to_0': sum(-.05 <= value < 0 for value in margins),
            'at_or_above_0': sum(value >= 0 for value in margins),
        },
        'eligible_momentum_burst_score_rejections': momentum_bursts,
        'eligible_signal_score_near_misses': eligible_near_misses,
        'top_20_rejected_scalp_candidates_by_score': top_rejected,
        'double_penalty_audit': {
            'volume': ['volume-expansion strategy gate', 'volume-expansion score',
                       'setup classification for MICRO_BREAKOUT'],
            'spread': ['spread hard gate', 'spread-quality score',
                       'round-trip cost and expected net edge'],
            'extension': ['score penalty', 'ENTRY_OVEREXTENDED hard gate'],
            'momentum': ['setup classification', 'micro-momentum score',
                         'expected-move projection'],
            'vwap': ['setup classification', 'VWAP-alignment score',
                     'structural stop selection'],
            'relative_strength': ['relative-strength score only'],
        },
        'offline_counterfactuals': counterfactuals,
        'universe_quality': by_symbol,
        'spread_distribution': {
            'count': len(spreads), 'median_pct': median(spreads) if spreads else None,
            'p90_pct': _percentile(spreads, .90),
            'max_pct': max(spreads) if spreads else None,
            'pass_count': funnel['spread_pass'],
            'failure_count': reasons['SPREAD_TOO_WIDE'],
            'pass_rate': rates['spread_pass'],
        },
        'shadow_entries': funnel['entries'], 'exits': len(closed),
        'entry_quality': entries,
        'normal_time_exits': sum(
            item.get('exit_reason') == 'SCALP_TIME_EXIT' for item in closed
        ),
        'recovery_time_exits': sum(
            item.get('exit_reason') == 'SCALP_RECOVERY_TIME_EXIT' for item in closed
        ),
        'overdue_positions_seen': len(overdue_by_episode),
        'max_overdue_seconds': lifecycle_max_overdue,
        'average_holding_seconds': mean(session_holds) if session_holds else None,
        'median_holding_seconds': median(session_holds) if session_holds else None,
        'p90_holding_seconds': _percentile(session_holds, .90),
        'maximum_holding_seconds': max(session_holds) if session_holds else None,
        'positions_exceeding_configured_max': len(exceeding),
        'positions_exceeding_max_quote_unavailable': len({
            item.get('episode_id') for item in lifecycle_rows
            if item.get('exit_reason') in {'QUOTE_UNAVAILABLE', 'STALE_QUOTE'}
            and float(item.get('overdue_by_seconds') or 0) > 0
        } | {
            item.get('episode_id') for item in exceeding
            if {'QUOTE_UNAVAILABLE', 'STALE_QUOTE'}.intersection(
                item.get('max_hold_delay_reasons', []) or []
            )
        }),
        'positions_exceeding_max_watcher_scheduling_delay': sum(
            'WATCHER_SCHEDULING_DELAY' in (item.get('max_hold_delay_reasons', []) or [])
            for item in exceeding
        ),
        'max_hold_sla': [{
            'symbol': item.get('symbol'), 'episode_id': item.get('episode_id'),
            'crossed_max_hold_at': item.get('crossed_max_hold_at'),
            'exit_decision_at': item.get('exit_decision_at'),
            'position_closed_at': item.get('exit_timestamp'),
            'decision_delay_seconds': item.get('max_hold_decision_delay_seconds'),
            'close_delay_seconds': item.get('max_hold_close_delay_seconds'),
            'delay_reasons': item.get('max_hold_delay_reasons', []),
        } for item in closed if item.get('crossed_max_hold_at')],
        'counting_note': 'candidate observations may repeat; symbols and episodes are separately deduplicated',
    }


def grouped(rows, classifier):
    groups = defaultdict(list)
    for row in rows: groups[classifier(row)].append(row)
    return {key: {'trades': len(group),
                  'net_expectancy': mean([x.get('net_pnl',0) or 0 for x in group]),
                  'win_rate': sum((x.get('net_pnl',0) or 0)>0 for x in group)/len(group),
                  'average_mfe': mean([x.get('mfe',x.get('maximum_favorable_excursion',0)) or 0 for x in group]),
                  'average_mae': mean([x.get('mae',x.get('maximum_adverse_excursion',0)) or 0 for x in group]),
                  'average_cost': mean([(x.get('estimated_cost',0) or 0) for x in group])}
            for key, group in groups.items()}


def spread_bucket(row):
    bid, ask = row.get('quoted_entry_bid'), row.get('quoted_entry_ask')
    spread = (ask-bid)/((ask+bid)/2) if bid and ask else None
    if spread is None: return 'UNKNOWN'
    if spread < .0005: return '<0.05%'
    if spread < .001: return '0.05-0.10%'
    if spread < .002: return '0.10-0.20%'
    return '>0.20%'


def holding_bucket(row):
    seconds = row.get('holding_time_seconds',
                      row.get('holding_seconds', (row.get('holding_time_minutes') or 0)*60))
    if seconds < 30: return '<30 sec'
    if seconds < 60: return '30-60 sec'
    if seconds < 120: return '1-2 min'
    if seconds < 300: return '2-5 min'
    return '5+ min'


def _rate(rows, reason): return sum(r.get('exit_reason') == reason for r in rows)/len(rows) if rows else None
def _row(value): return value.to_dict() if hasattr(value,'to_dict') else dict(value)
def _percentile(values, percentile):
    if not values: return None
    ordered = sorted(values); index = (len(ordered)-1)*percentile
    low, high = int(index), min(int(index)+1, len(ordered)-1)
    return ordered[low] + (ordered[high]-ordered[low])*(index-low)


def _distribution(values, thresholds=()):
    values = [float(value) for value in values if value is not None]
    result = {
        'count': len(values),
        'min': min(values) if values else None,
        'p10': _percentile(values, .10), 'p25': _percentile(values, .25),
        'median': median(values) if values else None,
        'p75': _percentile(values, .75), 'p90': _percentile(values, .90),
        'p95': _percentile(values, .95), 'max': max(values) if values else None,
    }
    result['thresholds'] = {
        f'at_or_above_{threshold:.2f}': {
            'count': sum(value >= threshold for value in values),
            'percent': 100*sum(value >= threshold for value in values)/len(values)
            if values else None,
        } for threshold in thresholds
    }
    return result


def _age_distribution(values):
    values = [float(value) for value in values]
    return {
        **_distribution(values),
        'p99': _percentile(values, .99),
        'percent_le_1s': 100*sum(value <= 1 for value in values)/len(values) if values else None,
        'percent_le_2s': 100*sum(value <= 2 for value in values)/len(values) if values else None,
        'percent_le_3s': 100*sum(value <= 3 for value in values)/len(values) if values else None,
        'percent_le_5s': 100*sum(value <= 5 for value in values)/len(values) if values else None,
        'percent_le_30s': 100*sum(value <= 30 for value in values)/len(values) if values else None,
        'percent_gt_60s': 100*sum(value > 60 for value in values)/len(values) if values else None,
        'percent_gt_120s': 100*sum(value > 120 for value in values)/len(values) if values else None,
        'percent_gt_300s': 100*sum(value > 300 for value in values)/len(values) if values else None,
    }


def _observation_age_summary(rows):
    ages = [float(item['freshness']['quote_age_seconds']) for item in rows
            if item.get('freshness', {}).get('quote_age_seconds') is not None]
    return {
        'observations': len(rows),
        'fresh_quote_pass_rate': sum(
            item.get('freshness', {}).get('quote_status') == 'FRESH' for item in rows
        ) / len(rows) if rows else None,
        'stale_count': sum(item.get('freshness', {}).get('quote_status') == 'STALE'
                           for item in rows),
        'median_seconds': median(ages) if ages else None,
        'p95_seconds': _percentile(ages, .95),
    }


def _variance(values):
    return mean([(value-mean(values))**2 for value in values]) if values else None


def _legacy_components(item):
    """Reconstruct old persisted component math without changing decisions."""
    signal = item.get('signal', {})
    score = signal.get('score')
    provenance = signal.get('feature_provenance', {})
    if score is None or not provenance:
        return {}
    spread = item.get('spread', {}).get('observed_pct')
    entry = item.get('geometry', {}).get('entry')
    price = entry/(1+spread/2) if entry and spread is not None else None
    specs = {
        'micro_momentum': (provenance.get('return_3', {}).get('value'), .25, .003, 0),
        'vwap_alignment': (provenance.get('price_vs_vwap', {}).get('value'), .20, None, 0),
        'ema_slope': (provenance.get('ema9_slope', {}).get('value'), .15, .001, price),
        'relative_strength': (provenance.get('relative_strength_spy', {}).get('value'), .15, .002, 0),
        'volume_expansion': (provenance.get('breakout_volume_expansion', {}).get('value'), .15, .5, 1),
        'spread_quality': (spread, .10, config.SCALP_MAX_SPREAD_PCT, None),
    }
    result = {}
    for name, (raw, weight, scale, offset) in specs.items():
        if raw is None:
            normalized = None
        elif name == 'vwap_alignment':
            normalized = 1.0 if raw > 0 else 0.0
        elif name == 'ema_slope':
            normalized = min(1.0, max(0.0, raw/(price or 1)/scale))
        elif name == 'spread_quality':
            normalized = min(1.0, max(0.0, 1-raw/scale))
        else:
            normalized = min(1.0, max(0.0, (raw-offset)/scale))
        result[name] = {
            'raw': raw, 'normalized': normalized, 'weight': weight,
            'contribution': normalized*weight if normalized is not None else None,
            'clamped_min': normalized == 0, 'clamped_max': normalized == 1,
            'missing': raw is None, 'fallback_used': False,
            'reconstructed_from_persisted_provenance': True,
        }
    positive = sum(row['contribution'] or 0 for row in result.values())
    penalty = min(1.0, max(0.0, (positive-float(score))/.20))
    result['entry_extension_penalty'] = {
        'raw': None, 'normalized': penalty, 'weight': -.20,
        'contribution': -.20*penalty, 'clamped_min': penalty == 0,
        'clamped_max': penalty == 1, 'missing': True, 'fallback_used': True,
        'reconstructed_from_rounded_score': True,
    }
    return result


def _eligible_detail(item):
    score = item.get('signal', {}).get('score')
    edge = item.get('friction', {}).get('expected_net_edge_pct')
    gross_rr = item.get('geometry', {}).get('gross_rr')
    extension = item.get('geometry', {}).get('entry_extension_pct')
    risk_pct = item.get('geometry', {}).get('risk_pct')
    target = item.get('geometry', {}).get('target')
    entry = item.get('geometry', {}).get('entry')
    net_rr = item.get('geometry', {}).get('net_rr')
    extension_pass = (
        extension is not None and extension <= config.SCALP_MAX_EXTENSION_PCT
    )
    stop_valid = (
        risk_pct is not None and risk_pct >= config.SCALP_MIN_STOP_DISTANCE_PCT
    )
    net_edge_pass = edge is not None and edge >= config.SCALP_MIN_EXPECTED_NET_EDGE
    target_valid = target is not None and entry is not None and target > entry
    gross_rr_pass = gross_rr is not None and gross_rr >= config.SCALP_MIN_RISK_REWARD
    downstream_feasible = all((
        extension_pass, stop_valid, net_edge_pass, target_valid, gross_rr_pass,
    ))
    return {
        'symbol': item.get('symbol'), 'timestamp': item.get('timestamp'),
        'setup_type': item.get('setup_type'),
        'quote_age_seconds': item.get('freshness', {}).get('quote_age_seconds'),
        'spread_pct': item.get('spread', {}).get('observed_pct'),
        'volume_expansion': item.get('volume_expansion', {}).get('observed'),
        'score_components': item.get('signal', {}).get('components') or _legacy_components(item),
        'score': score,
        'final_score': score,
        'threshold': config.SCALP_MIN_SIGNAL_SCORE,
        'distance_from_0_70': (float(score)-config.SCALP_MIN_SIGNAL_SCORE
                               if score is not None else None),
        'expected_move_pct': item.get('friction', {}).get('expected_move_pct'),
        'estimated_round_trip_cost_pct': item.get('friction', {}).get(
            'estimated_round_trip_cost_pct'),
        'expected_net_edge_pct': item.get('friction', {}).get('expected_net_edge_pct'),
        'entry': item.get('geometry', {}).get('entry'),
        'stop': item.get('geometry', {}).get('stop'),
        'target': target,
        'gross_rr': gross_rr,
        'net_rr': net_rr,
        'offline_net_edge': edge,
        'offline_net_rr': net_rr,
        'downstream_feasible': downstream_feasible,
        'final_rejection': (
            (item.get('blocking_reasons') or item.get('rejection_reasons') or [None])[0]
        ),
        'offline_downstream_counterfactual': {
            'extension_pass': extension_pass,
            'valid_stop': stop_valid,
            'expected_move_pct': item.get('friction', {}).get('expected_move_pct'),
            'estimated_friction_pct': item.get('friction', {}).get(
                'estimated_round_trip_cost_pct'),
            'net_edge': edge,
            'target': target,
            'gross_rr': gross_rr,
            'net_rr': net_rr,
            'net_edge_pass': net_edge_pass,
            'target_pass': target_valid,
            'gross_rr_pass': gross_rr_pass,
            'geometry_pass': downstream_feasible,
            'no_entry_was_attempted': True,
        },
    }


def _quality_summary(rows):
    edges = [item.get('friction', {}).get('expected_net_edge_pct') for item in rows]
    edges = [float(value) for value in edges if value is not None]
    gross_rr = [item.get('geometry', {}).get('gross_rr') for item in rows]
    gross_rr = [float(value) for value in gross_rr if value is not None]
    spreads = [item.get('spread', {}).get('observed_pct') for item in rows]
    spreads = [float(value) for value in spreads if value is not None]
    return {
        'observations': len(rows),
        'unique_episodes': len({item.get('episode_id') for item in rows
                                if item.get('episode_id')}),
        'expected_net_edge_median': median(edges) if edges else None,
        'expected_net_edge_pass_rate': sum(
            value >= config.SCALP_MIN_EXPECTED_NET_EDGE for value in edges
        ) / len(edges) if edges else None,
        'gross_rr_median': median(gross_rr) if gross_rr else None,
        'gross_rr_pass_rate': sum(
            value >= config.SCALP_MIN_RISK_REWARD for value in gross_rr
        ) / len(gross_rr) if gross_rr else None,
        'spread_median_pct': median(spreads) if spreads else None,
        'future_outcome': 'UNAVAILABLE_NO_LOOKAHEAD_LABEL',
    }
def _elapsed_hours(rows):
    if len(rows) < 2: return None
    from datetime import datetime
    times = [datetime.fromisoformat(str(r.get('exit_time',r.get('exit_timestamp'))).replace('Z','+00:00')) for r in rows if r.get('exit_time') or r.get('exit_timestamp')]
    return max((max(times)-min(times)).total_seconds()/3600, 1/60) if len(times)>=2 else None


def _strength(value):
    return 'UNAVAILABLE' if value is None else 'OUTPERFORMING' if value > 0 else 'NOT_OUTPERFORMING'


def _time_period_row(row):
    if row.get('time_period'): return row['time_period']
    value = row.get('exit_time', row.get('exit_timestamp'))
    if not value: return 'UNKNOWN'
    from datetime import datetime
    from strategies.scalp.simulation import time_period
    return time_period(datetime.fromisoformat(str(value).replace('Z','+00:00')))
