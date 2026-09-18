"""Append-only hypothetical comparisons; this module has no executor."""
from pathlib import Path
from datetime import datetime, timezone
import json

from rl.actions import EntryAction
from rl.observations import ObservationBuilder


class ShadowComparator:
    def __init__(self, path): self.path = Path(path)

    def record(self, *, timestamp, symbol, episode_id, baseline_action, rl_action,
               baseline_score=None, confidence=None, outcome_status='UNAVAILABLE', outcome=None):
        action_name = lambda value: value if isinstance(value, str) else EntryAction(value).name
        row = {'timestamp': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
               'symbol': symbol, 'episode_id': episode_id,
               'baseline_action': action_name(baseline_action),
               'rl_action': action_name(rl_action),
               'baseline_score': baseline_score, 'rl_policy_confidence': confidence,
               'disagreement': int(baseline_action) != int(rl_action),
               'outcome_status': outcome_status, 'future_outcome': outcome,
               'portfolio_mutated': False, 'mode': 'SHADOW_COMPARE'}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as handle: handle.write(json.dumps(row, sort_keys=True)+'\n')
        return row


class RuntimeShadowCompare:
    """Inference side-channel. It can return actions but owns no mutable trade state."""
    def __init__(self, policy, normalizer, comparator):
        self.policy, self.normalizer, self.comparator = policy, normalizer, comparator
        self.builder, self.history = ObservationBuilder(), {}

    def decide(self, context, quote, *, now, baseline_action):
        row = dict(context.canonical_market_data)
        row.update(timestamp=now.isoformat(), session_date=now.date().isoformat(),
                   symbol=context.symbol, episode_id=context.episode_id,
                   price=quote.mark_price, bid=quote.bid, ask=quote.ask,
                   quote_timestamp=quote.timestamp.isoformat(), quote_status='FRESH',
                   slow_score=context.slow_context_score, live_score=context.live_market_score,
                   dynamic_score=context.dynamic_score,
                   confirmation_count=context.consecutive_qualifying_updates,
                   setup_age=context.context_age_seconds,
                   research_entry=context.analysis_price, research_rr=context.risk_reward_ratio,
                   support=context.intraday_support_reference,
                   resistance=context.intraday_resistance_reference)
        rows = self.history.setdefault(context.episode_id, [])
        rows.append(row)
        vector, _, meta = self.builder.build(rows, len(rows)-1)
        action, _ = self.policy.predict(self.normalizer.transform(vector), deterministic=True)
        record = self.comparator.record(
            timestamp=now, symbol=context.symbol, episode_id=context.episode_id,
            baseline_action=baseline_action, rl_action=action,
            baseline_score=context.dynamic_score, outcome_status='UNAVAILABLE')
        record['feature_metadata'] = meta
        return EntryAction(action), record
