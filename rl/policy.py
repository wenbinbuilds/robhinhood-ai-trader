"""Baseline and one initial RL backend: Stable-Baselines3 PPO."""
from abc import ABC, abstractmethod
from pathlib import Path
import random
import numpy as np

import config
from rl.actions import EntryAction, PositionAction


class RLPolicy(ABC):
    @abstractmethod
    def train(self, environment, **kwargs): ...
    @abstractmethod
    def predict(self, observation, *, deterministic=True): ...
    @abstractmethod
    def save(self, path): ...
    @classmethod
    @abstractmethod
    def load(cls, path): ...


class BaselinePolicy:
    """The existing .72/two-confirmation preference rule, unchanged."""
    def predict_row(self, row):
        score = row.get('dynamic_score')
        return EntryAction.ENTER if isinstance(score, (int, float)) \
            and score >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD \
            and int(row.get('confirmation_count') or 0) >= config.FAST_ENTRY_CONFIRMATION_UPDATES \
            else EntryAction.WAIT

    def predict_step(self, step):
        return EntryAction(step.baseline_action)


class PPOPolicy(RLPolicy):
    algorithm = 'PPO'

    def __init__(self, model=None, *, seed=config.RL_RANDOM_SEED, hyperparameters=None):
        self.model, self.seed = model, seed
        self.hyperparameters = dict(hyperparameters or {})

    @staticmethod
    def available():
        try:
            import stable_baselines3  # noqa: F401
            return True
        except ImportError:
            return False

    def train(self, environment, *, total_timesteps=10_000, **kwargs):
        try:
            import torch
            from stable_baselines3 import PPO
        except ImportError as exc:
            raise RuntimeError('PPO requires pip install -r requirements.txt') from exc
        random.seed(self.seed); np.random.seed(self.seed); torch.manual_seed(self.seed)
        parameters = {'n_steps': min(128, max(8, len(environment.steps))), 'batch_size': 8,
                      'learning_rate': 3e-4, 'gamma': .99, 'verbose': 0,
                      **self.hyperparameters, **kwargs}
        if parameters['batch_size'] > parameters['n_steps']:
            parameters['batch_size'] = parameters['n_steps']
        self.hyperparameters = parameters
        self.model = PPO('MlpPolicy', environment, seed=self.seed, **parameters)
        self.model.learn(total_timesteps=total_timesteps)
        return self

    def predict(self, observation, *, deterministic=True):
        if self.model is None: raise ValueError('policy is not trained')
        action, state = self.model.predict(observation, deterministic=deterministic)
        return EntryAction(int(action)), state

    def save(self, path):
        if self.model is None: raise ValueError('policy is not trained')
        self.model.save(str(path))

    @classmethod
    def load(cls, path):
        try:
            from stable_baselines3 import PPO
        except ImportError as exc:
            raise RuntimeError('PPO requires pip install -r requirements.txt') from exc
        return cls(PPO.load(str(path)))


class PositionPolicyArbiter:
    """An optional early-exit proposal can never postpone mandatory exits."""
    MANDATORY = {'STOP_HIT', 'END_OF_DAY_EXIT', 'MISSED_EOD_RECOVERY_EXIT', 'HARD_RISK_EXIT'}

    @classmethod
    def final_action(cls, proposal: PositionAction, deterministic_reason=None):
        if deterministic_reason in cls.MANDATORY:
            return PositionAction.EXIT, deterministic_reason
        if deterministic_reason == 'TARGET_HIT':
            return PositionAction.EXIT, deterministic_reason
        return proposal, 'RL_EARLY_EXIT' if proposal == PositionAction.EXIT else None
