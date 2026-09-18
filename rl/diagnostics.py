"""Descriptive policy diagnostics; outputs are associations, not causal claims."""
from collections import defaultdict
import numpy as np

from rl.observations import VECTOR_NAMES


def feature_distributions(steps):
    values = np.asarray([s.observation for s in steps], dtype=float)
    return {name: {'mean': float(np.mean(values[:, i])), 'std': float(np.std(values[:, i])),
                   'min': float(np.min(values[:, i])), 'max': float(np.max(values[:, i]))}
            for i, name in enumerate(VECTOR_NAMES)} if len(values) else {}


def action_frequencies(records, key):
    groups = defaultdict(lambda: defaultdict(int))
    for row in records: groups[str(row.get(key, 'UNKNOWN'))][row['action']] += 1
    return {group: dict(counts) for group, counts in groups.items()}


def local_sensitivity(policy, observation, feature_indices, *, epsilon=.01):
    """One-at-a-time sensitivity; explicitly not causal feature importance."""
    baseline, _ = policy.predict(np.asarray(observation, dtype=np.float32), deterministic=True)
    results = {}
    for index in feature_indices:
        changed = np.asarray(observation, dtype=np.float32).copy()
        changed[index] += epsilon
        action, _ = policy.predict(changed, deterministic=True)
        results[VECTOR_NAMES[index]] = {'baseline_action': int(baseline),
                                        'perturbed_action': int(action), 'epsilon': epsilon}
    return results
