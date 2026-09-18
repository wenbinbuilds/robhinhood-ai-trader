from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import numpy as np
import pytest

import config
from rl.actions import EntryAction, PositionAction
from rl.dataset import DatasetStep, HistoricalDatasetBuilder, chronological_split, walk_forward_splits
from rl.env import TradingEnvironment
from rl.observations import ObservationBuilder, FeatureNormalizer, FEATURE_NAMES, VECTOR_NAMES
from rl.policy import BaselinePolicy, PositionPolicyArbiter
from rl.registry import ModelRegistry
from rl.safety import SafetyOverride
from rl.shadow_compare import ShadowComparator
from trading_runtime.contracts import Quality

NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def candle(at, *, high=101, low=99, close=100, volume=1000, forming=False):
    return {'begins_at': at.isoformat(), 'open': 100, 'high': high, 'low': low,
            'close': close, 'volume': volume, 'is_forming': forming}


def row(at=NOW, *, symbol='ACME', episode='e1', score=.7, confirmation=0, candles=None, **updates):
    value = {'timestamp': at.isoformat(), 'quote_timestamp': at.isoformat(),
             'session_date': at.date().isoformat(), 'symbol': symbol, 'episode_id': episode,
             'price': 100.0, 'bid': 99.99, 'ask': 100.01, 'quote_status': 'FRESH',
             'vwap': 99.5, 'ema9': 99.8, 'ema20': 99.0, 'rsi14': 55,
             'macd': .2, 'macd_signal': .1, 'relative_volume': 1.5,
             'dynamic_score': score, 'live_score': .75, 'slow_score': .68,
             'confirmation_count': confirmation, 'research_entry': 99.5,
             'research_rr': 2.0, 'live_rr': 1.8, 'stop': 99.0,
             'spy_return': .002, 'qqq_return': .003, 'sector_return': .001,
             'spy_vs_vwap': .001, 'qqq_vs_vwap': .002, 'market_volatility': .01,
             'candles': candles if candles is not None else [
                 candle(at-timedelta(minutes=5*i)) for i in range(6, 0, -1)],
             'exit_price': 102.0, 'outcome_status': 'SIMULATED'}
    value.update(updates)
    return value


def steps(days=4):
    raw = []
    for day in range(days):
        at = NOW+timedelta(days=day)
        raw.extend([row(at, episode=f'e{day}', score=.70),
                    row(at+timedelta(minutes=1), episode=f'e{day}', score=.73, confirmation=2)])
    return HistoricalDatasetBuilder().build(raw)


def test_no_future_candle_or_session_high_leakage():
    at = NOW
    known = candle(at-timedelta(minutes=5), high=101)
    future = candle(at, high=999, close=999)
    vector, features, meta = ObservationBuilder().build([
        row(at, candles=[known, future], resistance=999, structure_as_of=(at+timedelta(hours=1)).isoformat())
    ], 0)
    assert meta['latest_completed_candle'] == known['begins_at']
    assert features['distance_to_resistance'] == pytest.approx(.01)
    assert meta['setup_type'] != 'BREAKOUT'
    assert 999 not in vector


def test_score_features_only_use_prefix():
    rows = [row(NOW, score=.61), row(NOW+timedelta(minutes=1), score=.66),
            row(NOW+timedelta(minutes=2), score=.99)]
    _, features, _ = ObservationBuilder().build(rows, 1)
    assert features['dynamic_score_delta_1'] == pytest.approx(.05)
    assert features['dynamic_score_slope'] == pytest.approx(.05)


def test_relative_strength_uses_same_timestamp_horizons():
    rows = [row(NOW, price=100),
            row(NOW+timedelta(minutes=5), price=102, spy_return_5m=.01,
                sector_return_5m=.005)]
    _, features, _ = ObservationBuilder().build(rows, 1)
    assert features['stock_return_5m'] == pytest.approx(.02)
    assert features['stock_minus_spy_return_5m'] == pytest.approx(.01)
    assert features['stock_minus_sector_return_5m'] == pytest.approx(.015)


def test_sessions_never_connect_overnight():
    rows = [row(NOW, score=.61), row(NOW+timedelta(days=1), score=.8)]
    _, features, _ = ObservationBuilder().build(rows, 1)
    assert features['dynamic_score_delta_1'] is None
    assert features['recent_return_1'] is None


def test_chronological_split_and_walk_forward():
    values = steps(6)
    train, validation, test = chronological_split(values, train=.5, validation=.25)
    assert max(x.session_date for x in train) < min(x.session_date for x in validation)
    assert max(x.session_date for x in validation) < min(x.session_date for x in test)
    windows = list(walk_forward_splits(values, minimum_train_sessions=2))
    assert all(max(x.session_date for x in a) < min(x.session_date for x in b) for a, b in windows)


def test_normalizer_fit_only_training_and_persists_exact_schema():
    values = steps()
    train, validation, _ = chronological_split(values)
    normalizer = FeatureNormalizer().fit([s.observation for s in train], split='train')
    with pytest.raises(ValueError): FeatureNormalizer().fit([s.observation for s in validation], split='validation')
    restored = FeatureNormalizer.from_dict(normalizer.to_dict())
    assert np.array_equal(restored.transform(train[0].observation), normalizer.transform(train[0].observation))
    assert normalizer.to_dict()['feature_names'] == list(VECTOR_NAMES)


def test_wait_and_ignore_never_create_position_state():
    env = TradingEnvironment(steps())
    _, info = env.reset(seed=9)
    _, _, terminated, _, wait = env.step(EntryAction.WAIT)
    assert not terminated and wait['action'] == 'WAIT'
    _, _, terminated, _, ignored = env.step(EntryAction.IGNORE_SETUP)
    assert terminated and ignored['terminal_reason'] == 'IGNORED'
    assert not hasattr(env, 'portfolio')


def test_enter_with_bad_data_is_blocked():
    bad = DatasetStep(NOW.isoformat(), 'ACME', 'e', NOW.date().isoformat(),
                      tuple([0.0]*len(VECTOR_NAMES)), {}, 0, 2.0, 'TARGET_HIT',
                      'SIMULATED', ('STALE_OR_FUTURE_QUOTE',))
    env = TradingEnvironment([bad]); env.reset()
    _, reward, terminated, truncated, info = env.step(EntryAction.ENTER)
    assert reward == 0 and not terminated and truncated
    assert info['terminal_reason'] == 'ENTER_BLOCKED_DATA_QUALITY'


@pytest.mark.parametrize('field,reason', [('geometry','INVALID_GEOMETRY'), ('risk','RISK_REJECTED'),
                                         ('portfolio','PORTFOLIO_REJECTED'), ('market','MARKET_DATA_NOT_FRESH')])
def test_enter_cannot_bypass_deterministic_safety(monkeypatch, field, reason):
    monkeypatch.setattr('rl.safety.read_kill_switch', lambda _: SimpleNamespace(trading_blocked=True))
    values = dict(geometry=SimpleNamespace(valid=True), risk=SimpleNamespace(approved=True),
                  portfolio=SimpleNamespace(approved=True),
                  market_snapshot=SimpleNamespace(quote_status=Quality.FRESH))
    if field == 'geometry': values['geometry'] = SimpleNamespace(valid=False)
    elif field == 'risk': values['risk'] = SimpleNamespace(approved=False)
    elif field == 'portfolio': values['portfolio'] = SimpleNamespace(approved=False)
    else: values['market_snapshot'] = SimpleNamespace(quote_status=Quality.STALE)
    result = SafetyOverride.evaluate(EntryAction.ENTER, **values)
    assert not result.approved and reason in result.reasons


def test_rl_cannot_bypass_kill_switch_or_route_live(monkeypatch):
    monkeypatch.setattr('rl.safety.read_kill_switch', lambda _: SimpleNamespace(trading_blocked=False))
    result = SafetyOverride.evaluate(EntryAction.ENTER, target='LIVE')
    assert not result.approved
    assert {'RL_LIVE_EXECUTION_PROHIBITED', 'LIVE_KILL_SWITCH_NOT_BLOCKED'} <= set(result.reasons)


def test_baseline_policy_is_unchanged():
    policy = BaselinePolicy()
    assert policy.predict_row({'dynamic_score': .72, 'confirmation_count': 2}) == EntryAction.ENTER
    assert policy.predict_row({'dynamic_score': .719999, 'confirmation_count': 2}) == EntryAction.WAIT
    assert policy.predict_row({'dynamic_score': .9, 'confirmation_count': 1}) == EntryAction.WAIT


def test_reward_is_r_normalized_and_crosses_spread():
    from rl.rewards import RiskNormalizedReward, RewardConfig
    reward, detail = RiskNormalizedReward(RewardConfig(
        entry_slippage_bps=0, exit_slippage_bps=0, transaction_cost_penalty=0,
        drawdown_penalty=0, mae_penalty=0, overtrading_penalty=0,
        holding_penalty_per_hour=0)).calculate(
            {'ask': 100, 'bid': 99.9, 'stop': 99, 'outcome_bid': 102,
             'outcome_status': 'OBSERVED'})
    assert reward == pytest.approx(2.0)
    assert detail['r_multiple'] == pytest.approx(2.0)


def test_shadow_compare_is_append_only_and_has_no_portfolio(tmp_path):
    path = tmp_path/'compare.jsonl'
    comparator = ShadowComparator(path)
    first = comparator.record(timestamp=NOW, symbol='ACME', episode_id='e',
                              baseline_action=EntryAction.ENTER, rl_action=EntryAction.WAIT)
    comparator.record(timestamp=NOW, symbol='ACME', episode_id='e',
                      baseline_action=EntryAction.ENTER, rl_action=EntryAction.ENTER)
    assert first['portfolio_mutated'] is False
    assert len(path.read_text().splitlines()) == 2


def test_position_policy_cannot_override_stop_or_eod():
    assert PositionPolicyArbiter.final_action(PositionAction.HOLD, 'STOP_HIT') == (PositionAction.EXIT, 'STOP_HIT')
    assert PositionPolicyArbiter.final_action(PositionAction.HOLD, 'END_OF_DAY_EXIT') == (PositionAction.EXIT, 'END_OF_DAY_EXIT')


class FakePolicy:
    def save(self, path): Path(str(path)+'.fake').write_text('model')


def test_model_registry_metadata_versioning_and_no_overwrite(tmp_path):
    registry = ModelRegistry(tmp_path/'models')
    metadata = registry.create_metadata(model_id='model-1', algorithm='PPO',
        training_start=NOW.isoformat(), training_end=(NOW+timedelta(days=1)).isoformat(),
        feature_schema_version='1.0', reward_version='1.0', normalization_version='1.0',
        hyperparameters={'n_steps': 8}, random_seed=17, training_metrics={'average_r': 0})
    normalizer = FeatureNormalizer().fit([steps()[0].observation])
    registry.save(FakePolicy(), metadata, normalizer)
    assert registry.load_metadata('model-1').random_seed == 17
    with pytest.raises(FileExistsError): registry.save(FakePolicy(), metadata, normalizer)
    registry.promote('model-1', 'VALIDATED')
    assert registry.load_metadata('model-1').registry_state == 'VALIDATED'


def test_same_seed_reproducible_environment():
    env1, env2 = TradingEnvironment(steps(), seed=17), TradingEnvironment(steps(), seed=17)
    one, _ = env1.reset(seed=17); two, _ = env2.reset(seed=17)
    assert np.array_equal(one, two)
    assert env1.step(EntryAction.WAIT)[1:] == env2.step(EntryAction.WAIT)[1:]


def test_environment_keeps_interleaved_symbols_in_their_own_episode():
    base = steps(1)
    values = [base[0], DatasetStep(**{**base[0].__dict__, 'symbol': 'OTHER', 'episode_id': 'other'}),
              base[1], DatasetStep(**{**base[1].__dict__, 'symbol': 'OTHER', 'episode_id': 'other'})]
    env = TradingEnvironment(values)
    _, first = env.reset()
    _, _, terminated, _, second = env.step(EntryAction.WAIT)
    assert not terminated
    assert first['episode_id'] == second['episode_id']


def test_default_configuration_is_offline_and_live_disabled():
    assert config.RL_ENABLED is False and config.RL_MODE == 'OFFLINE'
    assert config.MODE == 'SHADOW_TRADING'
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False


def test_ppo_backend_actually_trains_with_fixed_seed():
    from rl.policy import PPOPolicy
    if not PPOPolicy.available(): pytest.skip('optional PPO dependencies not installed')
    values = steps()
    normalizer = FeatureNormalizer().fit([s.observation for s in values])
    policy = PPOPolicy(seed=17, hyperparameters={'n_epochs': 1}).train(
        TradingEnvironment(values, normalizer=normalizer, seed=17), total_timesteps=32)
    observation, _ = TradingEnvironment(values, normalizer=normalizer, seed=17).reset(seed=17)
    first, _ = policy.predict(observation)
    second, _ = policy.predict(observation)
    assert first == second


def test_training_registry_and_unseen_evaluation_pipeline(tmp_path):
    from rl.policy import PPOPolicy
    from rl.training import train_ppo, evaluate_model
    if not PPOPolicy.available(): pytest.skip('optional PPO dependencies not installed')
    dataset = tmp_path/'dataset.jsonl'
    HistoricalDatasetBuilder().write(steps(5), dataset)
    trained = train_ppo(dataset, tmp_path/'models', timesteps=32, seed=17)
    report = evaluate_model(dataset, tmp_path/'models', trained['model_id'])
    assert report['split'] == 'test'
    assert report['model_metadata']['algorithm'] == 'PPO'
    assert report['model_metadata']['random_seed'] == 17


class FixedRuntime:
    def __init__(self, action): self.action, self.calls = action, 0
    def decide(self, context, quote, *, now, baseline_action):
        self.calls += 1
        return self.action, {'baseline_action': baseline_action.name, 'rl_action': self.action.name}


def test_shadow_compare_enter_proposal_cannot_change_portfolio(tmp_path, monkeypatch):
    from test_candidate_watchlist import watcher, context, quote, SequenceScorer, NOW as WATCH_NOW
    monkeypatch.setattr(config, 'RL_MODE', 'SHADOW_COMPARE')
    candidate, _, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1]))
    candidate.rl_shadow_runtime = FixedRuntime(EntryAction.ENTER)
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    assert not portfolio.has_symbol('ACME')


def test_shadow_control_ignore_expires_only_setup(tmp_path, monkeypatch):
    from test_candidate_watchlist import watcher, context, quote, SequenceScorer, NOW as WATCH_NOW
    monkeypatch.setattr(config, 'RL_MODE', 'SHADOW_CONTROL')
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1]))
    candidate.rl_shadow_runtime = FixedRuntime(EntryAction.IGNORE_SETUP)
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    assert store.snapshot()[0].candidate_state == 'EXPIRED'
    assert not portfolio.has_symbol('ACME')


def test_shadow_control_uses_only_shadow_executor_and_deduplicates(tmp_path, monkeypatch):
    from test_candidate_watchlist import watcher, context, quote, SequenceScorer, NOW as WATCH_NOW
    from execution.robinhood_executor import RobinhoodExecutor
    monkeypatch.setattr(config, 'RL_MODE', 'SHADOW_CONTROL')
    monkeypatch.setattr(RobinhoodExecutor, 'execute', lambda *a, **k: pytest.fail('real executor invoked'))
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1, 1]))
    candidate.rl_shadow_runtime = FixedRuntime(EntryAction.ENTER)
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    assert len(portfolio.snapshot().open_positions) == 1
    # Re-presenting the same episode is removed because canonical portfolio wins.
    store.replace([context(slow=.9)], now=WATCH_NOW)
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    assert len(portfolio.snapshot().open_positions) == 1


def test_rl_action_space_cannot_size_or_change_geometry():
    env = TradingEnvironment(steps())
    assert env.action_space.n == 3
    assert {action.name for action in EntryAction} == {'WAIT', 'ENTER', 'IGNORE_SETUP'}
