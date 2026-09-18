"""Reproducible chronological RL dataset and leakage-safe splits."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib
import json

from rl.actions import EntryAction
from rl.observations import ObservationBuilder, VECTOR_NAMES
from rl.rewards import RiskNormalizedReward


@dataclass(frozen=True)
class DatasetStep:
    timestamp: str
    symbol: str
    episode_id: str
    session_date: str
    observation: tuple[float, ...]
    feature_metadata: Mapping[str, Any]
    baseline_action: int
    reward: float = 0.0
    terminal_reason: str | None = None
    outcome_status: str = 'UNAVAILABLE'
    quality_flags: tuple[str, ...] = ()
    next_state_metadata: Mapping[str, Any] | None = None
    outcome_metadata: Mapping[str, Any] | None = None

    def to_dict(self):
        return {**self.__dict__, 'observation': list(self.observation),
                'quality_flags': list(self.quality_flags)}

    @classmethod
    def from_dict(cls, row):
        return cls(**{**row, 'observation': tuple(row['observation']),
                      'quality_flags': tuple(row.get('quality_flags', ()))})


class HistoricalDatasetBuilder:
    schema_version = '1.0'

    def __init__(self, observation_builder=None):
        self.observation_builder = observation_builder or ObservationBuilder()
        self.reward_function = RiskNormalizedReward()

    def build(self, rows: Sequence[Mapping[str, Any]]):
        grouped = {}
        for row in rows:
            self._validate_row(row)
            at = _time(row['timestamp'])
            session = row.get('session_date') or at.date().isoformat()
            key = (str(row['symbol']).upper(), str(row.get('episode_id') or ''), session)
            grouped.setdefault(key, []).append(dict(row, session_date=session))
        steps = []
        for (symbol, supplied_episode, session), group in sorted(grouped.items()):
            group.sort(key=lambda r: _time(r['timestamp']))
            episode = supplied_episode or self._episode_id(symbol, session, group[0]['timestamp'])
            for i, row in enumerate(group):
                vector, _, meta = self.observation_builder.build(group, i)
                flags = self.quality_flags(row, group, i)
                baseline = (EntryAction.ENTER if _n(row.get('dynamic_score')) is not None
                            and _n(row.get('dynamic_score')) >= .72
                            and int(row.get('confirmation_count') or 0) >= 2
                            and not flags else EntryAction.WAIT)
                reward, reward_meta = self.reward_function.calculate(row)
                next_meta = ({'timestamp': _time(group[i+1]['timestamp']).isoformat(),
                              'same_episode': True} if i+1 < len(group)
                             else {'timestamp': None, 'same_episode': False})
                outcome_meta = {name: row.get(name) for name in
                    ('mfe_r', 'mae_r', 'holding_seconds', 'exit_price', 'terminal_reason')}
                steps.append(DatasetStep(
                    _time(row['timestamp']).isoformat(), symbol, episode, session,
                    tuple(float(x) for x in vector), meta, int(baseline),
                    reward, row.get('terminal_reason'),
                    str(reward_meta.get('outcome_status', row.get('outcome_status', 'UNAVAILABLE'))),
                    tuple(flags), next_meta, outcome_meta))
        steps.sort(key=lambda x: (x.timestamp, x.symbol, x.episode_id))
        return steps

    @staticmethod
    def quality_flags(row, group, index):
        flags = []
        if row.get('quote_status') not in (None, 'FRESH'):
            flags.append('QUOTE_' + str(row.get('quote_status')))
        if row.get('quote_timestamp'):
            age = (_time(row['timestamp'])-_time(row['quote_timestamp'])).total_seconds()
            if age < 0 or age > 15:
                flags.append('STALE_OR_FUTURE_QUOTE')
        elif row.get('bid') is None or row.get('ask') is None:
            flags.append('QUOTE_UNAVAILABLE')
        if row.get('spread') is None and (row.get('bid') is None or row.get('ask') is None):
            flags.append('SPREAD_UNKNOWN')
        if index and _time(row['timestamp']) <= _time(group[index-1]['timestamp']):
            flags.append('BROKEN_TIMESTAMP_SEQUENCE')
        if index and (_time(row['timestamp'])-_time(group[index-1]['timestamp'])).total_seconds() > 120:
            flags.append('LARGE_MONITORING_GAP')
        completed = [c for c in row.get('candles', []) or [] if not c.get('is_forming')]
        for candle in completed:
            try:
                if _time(candle['begins_at']) + timedelta(minutes=5) > _time(row['timestamp']):
                    flags.append('FUTURE_OR_UNCLOSED_CANDLE')
                values = [float(candle[x]) for x in ('open','high','low','close','volume')]
                if values[1] < values[2] or values[4] < 0:
                    flags.append('INVALID_CANDLE')
            except (KeyError, TypeError, ValueError):
                flags.append('INVALID_CANDLE')
        if len(completed) < 6:
            flags.append('INSUFFICIENT_WARMUP')
        return list(dict.fromkeys(flags))

    def write(self, steps, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w') as handle:
            for step in steps:
                handle.write(json.dumps(step.to_dict(), sort_keys=True, allow_nan=False)+'\n')
        return {'path': str(path), 'steps': len(steps), 'schema_version': self.schema_version,
                'feature_count': len(VECTOR_NAMES)}

    @staticmethod
    def read(path):
        with Path(path).open() as handle:
            return [DatasetStep.from_dict(json.loads(line)) for line in handle if line.strip()]

    @staticmethod
    def _validate_row(row):
        if not row.get('timestamp') or not row.get('symbol'):
            raise ValueError('historical row requires timestamp and symbol')
        _time(row['timestamp'])

    @staticmethod
    def _episode_id(symbol, session, at):
        return symbol + '-' + hashlib.sha256(f'{session}:{at}'.encode()).hexdigest()[:24]


def chronological_split(steps, train=.70, validation=.15):
    if not 0 < train < 1 or not 0 <= validation < 1 or train+validation >= 1:
        raise ValueError('invalid split fractions')
    dates = sorted({step.session_date for step in steps})
    if len(dates) < 3:
        raise ValueError('at least three sessions are required for train/validation/test')
    train_end = max(1, int(len(dates)*train))
    validation_end = max(train_end+1, int(len(dates)*(train+validation)))
    validation_end = min(validation_end, len(dates)-1)
    train_dates, validation_dates = set(dates[:train_end]), set(dates[train_end:validation_end])
    test_dates = set(dates[validation_end:])
    return ([s for s in steps if s.session_date in train_dates],
            [s for s in steps if s.session_date in validation_dates],
            [s for s in steps if s.session_date in test_dates])


def walk_forward_splits(steps, minimum_train_sessions=2, test_sessions=1):
    dates = sorted({step.session_date for step in steps})
    for end in range(minimum_train_sessions, len(dates), test_sessions):
        train_dates = set(dates[:end])
        test_dates = set(dates[end:end+test_sessions])
        if test_dates:
            yield ([s for s in steps if s.session_date in train_dates],
                   [s for s in steps if s.session_date in test_dates])


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamps must be timezone-aware')
    return result.astimezone(timezone.utc)


def _n(value):
    try: return float(value)
    except (TypeError, ValueError): return None
