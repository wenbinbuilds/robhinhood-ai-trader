"""Offline baseline/RL comparisons and walk-forward summaries."""
from collections import defaultdict
from statistics import mean, median
import math
import numpy as np

from rl.actions import EntryAction
from rl.observations import FEATURE_NAMES


def evaluate_policy(steps, policy, *, normalizer=None):
    records = []
    for step in steps:
        vector = np.asarray(step.observation, dtype=np.float32)
        if normalizer: vector = normalizer.transform(vector)
        if hasattr(policy, 'predict_step'):
            action = policy.predict_step(step)
        else:
            action, _ = policy.predict(vector, deterministic=True)
        records.append({'timestamp': step.timestamp, 'symbol': step.symbol,
                        'episode_id': step.episode_id, 'action': EntryAction(action).name,
                        'r': step.reward if int(action) == int(EntryAction.ENTER) else 0.0,
                        'setup_type': step.feature_metadata.get('setup_type', 'UNCLASSIFIED'),
                        'market_regime': step.feature_metadata.get('market_regime', 'UNKNOWN'),
                        'outcome_status': step.outcome_status,
                        'score_bucket': _score_bucket(step.observation[FEATURE_NAMES.index('dynamic_score')]),
                        'holding_seconds': (step.outcome_metadata or {}).get('holding_seconds'),
                        'mfe_r': (step.outcome_metadata or {}).get('mfe_r'),
                        'mae_r': (step.outcome_metadata or {}).get('mae_r'),
                        'quality_flags': list(step.quality_flags)})
    return metric_summary(records), records


def metric_summary(records):
    entries = [r for r in records if r['action'] == 'ENTER' and not r['quality_flags']]
    returns = [r['r'] for r in entries if r['outcome_status'] != 'UNAVAILABLE']
    wins, losses = [x for x in returns if x > 0], [x for x in returns if x < 0]
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for value in returns:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak-equity)
    grouped = lambda key: {name: _group(values) for name, values in _groups(entries, key).items()}
    return {'entries': len(entries), 'observed_entries': len(returns),
            'win_rate': len(wins)/len(returns) if returns else None,
            'average_r': mean(returns) if returns else None,
            'median_r': median(returns) if returns else None,
            'expectancy': mean(returns) if returns else None,
            'profit_factor': sum(wins)/abs(sum(losses)) if losses else None,
            'maximum_drawdown_r': drawdown,
            'sharpe_like': mean(returns)/np.std(returns) if returns and np.std(returns) > 0 else None,
            'average_holding_seconds': _available_mean(entries, 'holding_seconds'),
            'average_mfe_r': _available_mean(entries, 'mfe_r'),
            'average_mae_r': _available_mean(entries, 'mae_r'),
            'mfe_capture_percent': _mfe_capture(entries),
            'by_setup_type': grouped('setup_type'), 'by_market_regime': grouped('market_regime'),
            'by_score_bucket': grouped('score_bucket'),
            'overtrading_rate': _overtrading(entries),
            'limitations': ([] if entries and all(r['holding_seconds'] is not None and r['mfe_r'] is not None and r['mae_r'] is not None for r in entries)
                            else ['holding time, MFE and MAE unavailable for some source outcomes'])}


def comparison(baseline_records, rl_records):
    by_key = {(r['episode_id'], r['timestamp']): r for r in baseline_records}
    rows = []
    for rl in rl_records:
        base = by_key.get((rl['episode_id'], rl['timestamp']))
        if base:
            rows.append({**rl, 'baseline_action': base['action'], 'rl_action': rl['action'],
                         'disagreement': base['action'] != rl['action']})
    return {'decisions': len(rows), 'disagreements': sum(r['disagreement'] for r in rows), 'rows': rows}


def _groups(rows, key):
    result = defaultdict(list)
    for row in rows: result[row[key]].append(row['r'])
    return result


def _group(values):
    return {'entries': len(values), 'average_r': mean(values) if values else None,
            'win_rate': sum(v > 0 for v in values)/len(values) if values else None}


def _score_bucket(value):
    if value < .60: return '<0.60'
    if value < .72: return '0.60-0.72'
    if value < .80: return '0.72-0.80'
    return '>=0.80'


def _available_mean(rows, key):
    values = [float(r[key]) for r in rows if r.get(key) is not None]
    return mean(values) if values else None


def _mfe_capture(rows):
    values = [r['r']/float(r['mfe_r']) for r in rows if r.get('mfe_r') not in (None, 0)]
    return 100*mean(values) if values else None


def _overtrading(rows):
    counts = defaultdict(int)
    for row in rows: counts[(row['episode_id'], row['timestamp'][:10])] += 1
    return sum(max(0, n-1) for n in counts.values())/len(rows) if rows else None
