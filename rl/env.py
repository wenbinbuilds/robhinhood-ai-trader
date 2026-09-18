"""Deterministic Gymnasium-style entry-timing environment."""
from collections import defaultdict
import numpy as np

from rl.actions import EntryAction
from rl.observations import VECTOR_NAMES, FeatureNormalizer
from rl.rewards import RiskNormalizedReward

try:
    import gymnasium as gym
    from gymnasium import spaces
    BaseEnv = gym.Env
except ImportError:  # API-compatible core remains testable without optional RL extras.
    gym = None
    class BaseEnv: pass
    class _Discrete:
        def __init__(self, n): self.n = n
        def contains(self, x): return isinstance(x, (int, np.integer)) and 0 <= int(x) < self.n
    class _Box:
        def __init__(self, low, high, shape, dtype): self.low, self.high, self.shape, self.dtype = low, high, shape, dtype
    class spaces: Discrete = _Discrete; Box = _Box


class TradingEnvironment(BaseEnv):
    metadata = {'render_modes': []}

    def __init__(self, steps, *, normalizer: FeatureNormalizer | None = None,
                 reward_function=None, seed=17):
        self.steps = list(steps)
        if not self.steps:
            raise ValueError('environment requires historical steps')
        self.normalizer = normalizer
        self.reward_function = reward_function or RiskNormalizedReward()
        self.action_space = spaces.Discrete(3)
        self.observation_space = spaces.Box(-np.inf, np.inf, (len(VECTOR_NAMES),), np.float32)
        self._seed, self.index, self.done = seed, 0, False
        self.entries_by_session = defaultdict(int)
        grouped = defaultdict(list)
        for step in self.steps: grouped[step.episode_id].append(step)
        self.episodes = sorted(
            (sorted(group, key=lambda step: step.timestamp) for group in grouped.values()),
            key=lambda group: group[0].timestamp)
        self._episode_cursor = -1

    def reset(self, *, seed=None, options=None):
        if seed is not None: self._seed = seed
        if gym is not None: super().reset(seed=self._seed)
        self._episode_cursor = (self._episode_cursor + 1) % len(self.episodes)
        self._active_steps = self.episodes[self._episode_cursor]
        self.index = 0
        self.done = False
        return self._observation(), self._info('RESET')

    def step(self, action):
        if self.done: raise RuntimeError('episode is terminated; call reset')
        if not self.action_space.contains(action): raise ValueError('invalid action')
        action = EntryAction(int(action))
        current = self._active_steps[self.index]
        reward, terminated, truncated, reason, reward_meta = 0.0, False, False, None, {}
        if action == EntryAction.ENTER:
            if current.quality_flags:
                reason, truncated = 'ENTER_BLOCKED_DATA_QUALITY', True
            else:
                self.entries_by_session[current.session_date] += 1
                # Outcome data is reward-only and never present in observation.
                reward = float(current.reward)
                reward_meta = {'outcome_status': current.outcome_status}
                reason, terminated = current.terminal_reason or 'ENTERED', True
        elif action == EntryAction.IGNORE_SETUP:
            reason, terminated = 'IGNORED', True
        if not terminated and not truncated:
            self.index += 1
            if self.index >= len(self._active_steps):
                self.index = len(self._active_steps)-1
                terminated, reason = True, 'EPISODE_EXHAUSTED'
        self.done = terminated or truncated
        return self._observation(), reward, terminated, truncated, self._info(reason, action, reward_meta)

    def _observation(self):
        value = np.asarray(self._active_steps[self.index].observation, dtype=np.float32)
        return self.normalizer.transform(value) if self.normalizer else value

    def _info(self, reason, action=None, extra=None):
        step = self._active_steps[self.index]
        return {'timestamp': step.timestamp, 'symbol': step.symbol, 'episode_id': step.episode_id,
                'action': None if action is None else action.name, 'terminal_reason': reason,
                'quality_flags': list(step.quality_flags), **(extra or {})}
