"""Explicit offline training/evaluation entry points; never called by market loops."""
from dataclasses import asdict
from pathlib import Path
import json

import config
from rl.dataset import HistoricalDatasetBuilder, chronological_split, walk_forward_splits
from rl.env import TradingEnvironment
from rl.evaluation import evaluate_policy, comparison
from rl.observations import FeatureNormalizer
from rl.policy import PPOPolicy, BaselinePolicy
from rl.registry import ModelRegistry


def train_ppo(dataset_path, registry_path, *, timesteps=10_000, seed=config.RL_RANDOM_SEED):
    steps = HistoricalDatasetBuilder.read(dataset_path)
    train, validation, test = chronological_split(steps)
    normalizer = FeatureNormalizer().fit([s.observation for s in train], split='train')
    environment = TradingEnvironment(train, normalizer=normalizer, seed=seed)
    policy = PPOPolicy(seed=seed).train(environment, total_timesteps=timesteps)
    baseline_metrics, baseline_records = evaluate_policy(validation, BaselinePolicy())
    validation_metrics, validation_records = evaluate_policy(validation, policy, normalizer=normalizer)
    registry = ModelRegistry(registry_path)
    metadata = registry.create_metadata(
        algorithm='PPO', training_start=min(s.timestamp for s in train),
        training_end=max(s.timestamp for s in train), feature_schema_version=config.RL_FEATURE_SCHEMA_VERSION,
        reward_version=config.RL_REWARD_VERSION, normalization_version=config.RL_NORMALIZATION_VERSION,
        hyperparameters=policy.hyperparameters, random_seed=seed,
        training_metrics={'train_steps': len(train), 'validation': validation_metrics,
                          'baseline_validation': baseline_metrics,
                          'validation_disagreements': comparison(baseline_records, validation_records)['disagreements']})
    directory = registry.save(policy, metadata, normalizer)
    return {'model_id': metadata.model_id, 'directory': str(directory),
            'training': metadata.training_metrics, 'held_out_test_steps': len(test),
            'note': 'test split was not used during training or model selection'}


def load_model(registry_path, model_id):
    root = Path(registry_path)/model_id
    policy = PPOPolicy.load(root/'model')
    normalizer = FeatureNormalizer.from_dict(json.loads((root/'normalization.json').read_text()))
    return policy, normalizer, ModelRegistry(registry_path).load_metadata(model_id)


def evaluate_model(dataset_path, registry_path, model_id, *, split='test'):
    steps = HistoricalDatasetBuilder.read(dataset_path)
    train, validation, test = chronological_split(steps)
    selected = {'train': train, 'validation': validation, 'test': test}[split]
    policy, normalizer, metadata = load_model(registry_path, model_id)
    baseline_metrics, baseline_records = evaluate_policy(selected, BaselinePolicy())
    rl_metrics, rl_records = evaluate_policy(selected, policy, normalizer=normalizer)
    compared = comparison(baseline_records, rl_records)
    return {'model_id': model_id, 'split': split, 'baseline': baseline_metrics, 'rl': rl_metrics,
            'decisions': compared['decisions'], 'disagreements': compared['disagreements'],
            'model_metadata': asdict(metadata)}


def walk_forward_report(dataset_path, policy_factory):
    steps = HistoricalDatasetBuilder.read(dataset_path)
    reports = []
    for window, (train, test) in enumerate(walk_forward_splits(steps), 1):
        normalizer = FeatureNormalizer().fit([s.observation for s in train], split='train')
        policy = policy_factory(TradingEnvironment(train, normalizer=normalizer))
        metrics, _ = evaluate_policy(test, policy, normalizer=normalizer)
        reports.append({'window': window, 'training_start': train[0].session_date,
                        'training_end': train[-1].session_date, 'test_start': test[0].session_date,
                        'test_end': test[-1].session_date, 'metrics': metrics})
    return reports
