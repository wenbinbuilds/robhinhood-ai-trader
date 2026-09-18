"""Strategy-attributed performance and friction diagnostics."""
from collections import defaultdict
from statistics import mean, median


def scalp_summary(trades):
    rows = [_row(t) for t in trades if _row(t).get('strategy_id', _row(t).get('strategy')) in {'SCALP','SCALP_V1'}]
    pnl = [r.get('net_pnl', 0) or 0 for r in rows]
    gross = [r.get('gross_pnl', 0) or 0 for r in rows]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    rs = [(r.get('r_multiple') if r.get('r_multiple') is not None else
           r.get('net_pnl')/r.get('risk_allocated') if r.get('risk_allocated') else None)
          for r in rows]
    rs = [r for r in rs if r is not None]
    holds = [r.get('holding_seconds', (r.get('holding_time_minutes') or 0)*60) for r in rows]
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


def strategy_attribution(portfolio):
    state = portfolio.snapshot()
    result = {}
    for strategy_id, aliases in {'MOMENTUM': {'MOMENTUM','INTRADAY_MOMENTUM_V1'},
                                 'SCALP': {'SCALP','SCALP_V1'}}.items():
        open_rows = [p for p in state.open_positions if p.strategy_id in aliases or p.strategy in aliases]
        closed = [t for t in state.closed_positions if t.strategy_id in aliases or t.strategy in aliases]
        result[strategy_id] = {
            'capital_allocated': sum(p.capital_allocated or p.notional_value for p in open_rows),
            'risk_allocated': sum(p.risk_allocated or p.maximum_theoretical_loss for p in open_rows),
            'realized_pnl': sum(t.net_pnl for t in closed),
            'unrealized_pnl': sum(p.unrealized_pnl for p in open_rows),
            'trade_count': len(closed), 'open_positions': len(open_rows)}
    return result


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
    seconds = row.get('holding_seconds', (row.get('holding_time_minutes') or 0)*60)
    if seconds < 30: return '<30 sec'
    if seconds < 60: return '30-60 sec'
    if seconds < 120: return '1-2 min'
    if seconds < 300: return '2-5 min'
    return '5+ min'


def _rate(rows, reason): return sum(r.get('exit_reason') == reason for r in rows)/len(rows) if rows else None
def _row(value): return value.to_dict() if hasattr(value,'to_dict') else dict(value)
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
