import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from shadow.portfolio import ShadowPortfolio
from strategies.scalp.analytics import friction_sensitivity, scalp_summary, strategy_attribution
from strategies.scalp.execution import ScalpEntryController
from strategies.scalp.position import ScalpPositionController
from strategies.scalp.setup import ScalpSetupController
from strategies.scalp.signals import ScalpSignalEngine, completed_micro_bars
from strategies.scalp.simulation import ScalpBacktester
from watcher.models import FastQuote

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def market_data(now=NOW, *, future=False):
    closes = [99.6, 99.7, 99.8, 99.9, 100.0, 100.2]
    candles = [dict(begins_at=(now-timedelta(minutes=6-i)).isoformat(),
                    interval_seconds=60, open=value-.02, high=value+.02,
                    low=value-.05, close=value,
                    volume=1000 if i < 5 else 2500)
               for i, value in enumerate(closes)]
    if future:
        candles.append(dict(begins_at=(now+timedelta(minutes=1)).isoformat(),
                            interval_seconds=60, open=100, high=999, low=1,
                            close=999, volume=999999))
    return dict(candles=candles, vwap=99.8, ema9=100, ema20=99.8,
                previous_ema9=99.9, relative_volume=2, spy_return_3bar=0,
                qqq_return_3bar=0, sector_return_3bar=0, rsi14=60,
                macd=.2, macd_signal=.1, market_regime='TRENDING_BULL')


def quote(now=NOW, *, bid=100.23, ask=100.27, last=100.25, open_=True):
    return FastQuote('ACME', bid, ask, last, now, 'TEST_FAST', open_)


def controller(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setattr(config, 'SCALP_ENABLED', enabled)
    portfolio = ShadowPortfolio(tmp_path/'portfolio.json', tmp_path/'trades.jsonl')
    value = ScalpEntryController(portfolio, setup_path=tmp_path/'setups.json',
                                 events_path=tmp_path/'events.jsonl')
    return value, portfolio


def opened(tmp_path, monkeypatch):
    entry, portfolio = controller(tmp_path, monkeypatch)
    position, detail = entry.process('ACME', quote(), market_data(), now=NOW)
    assert position is not None, detail
    return entry, portfolio, position


def test_disabled_scalper_creates_no_entry(tmp_path, monkeypatch):
    entry, portfolio = controller(tmp_path, monkeypatch, enabled=False)
    position, detail = entry.process('ACME', quote(), market_data(), now=NOW)
    assert position is None and detail['reason'] == 'SCALP_DISABLED'
    assert not portfolio.snapshot().open_positions


def test_scalp_safety_defaults_are_disabled_and_shadow_only():
    assert config.SCALP_ENABLED is False
    assert config.SCALP_MODE == 'SHADOW'
    assert config.MODE == 'SHADOW_TRADING'


def test_scalp_modules_have_no_llm_imports():
    root = Path(__file__).resolve().parents[1]/'strategies'/'scalp'
    imports = []
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): imports.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom): imports.append(node.module or '')
    assert not any('llm' in name.lower() or 'reasoning' in name.lower() for name in imports)


def test_stale_quote_and_wide_spread_are_hard_blocks():
    signal = ScalpSignalEngine()
    stale = signal.evaluate('e', 'ACME', quote(NOW-timedelta(seconds=3)), market_data(), now=NOW)
    wide = signal.evaluate('e', 'ACME', quote(bid=100, ask=100.2), market_data(), now=NOW)
    assert 'STALE_QUOTE' in stale.rejection_reasons
    assert 'SPREAD_TOO_WIDE' in wide.rejection_reasons


def test_stale_micro_bars_block_even_with_fresh_quote():
    old = NOW-timedelta(minutes=10)
    decision = ScalpSignalEngine().evaluate('e', 'ACME', quote(), market_data(old), now=NOW)
    assert 'MICRO_BARS_STALE' in decision.rejection_reasons


def test_expected_move_must_exceed_friction(monkeypatch):
    signal = ScalpSignalEngine()
    monkeypatch.setattr(config, 'SCALP_MIN_EXPECTED_NET_EDGE', .01)
    decision = signal.evaluate('e', 'ACME', quote(), market_data(), now=NOW)
    assert 'INSUFFICIENT_NET_EDGE' in decision.rejection_reasons
    monkeypatch.setattr(config, 'SCALP_MIN_EXPECTED_NET_EDGE', .0005)
    assert signal.evaluate('e', 'ACME', quote(), market_data(), now=NOW).approved


def test_strong_scalp_production_path_exceeds_threshold_and_enters_shadow(
    tmp_path, monkeypatch
):
    decision = ScalpSignalEngine().evaluate(
        'reachable', 'ACME', quote(), market_data(), now=NOW
    )
    assert decision.signal_score >= config.SCALP_MIN_SIGNAL_SCORE
    assert sum(
        component['contribution']
        for component in decision.features.score_breakdown.values()
    ) == pytest.approx(decision.signal_score)
    entry, portfolio = controller(tmp_path, monkeypatch)
    position, detail = entry.process('ACME', quote(), market_data(), now=NOW)
    assert position is not None
    assert detail['status'] == 'OPENED'
    assert portfolio.snapshot().open_positions[0].strategy_id == 'SCALP'


def test_micro_breakout_extension_audit_distinguishes_trigger_from_production_reference():
    decision = ScalpSignalEngine().evaluate(
        'audit', 'ACME', quote(), market_data(), now=NOW
    )
    audit = decision.features.entry_extension_reference
    assert decision.setup_type == 'MICRO_BREAKOUT'
    assert audit['production_reference_type'] == 'EMA9'
    assert audit['production_reference_price'] == 100
    assert audit['structural_reference_type'] == 'PRIOR_COMPLETED_MICRO_HIGH'
    assert audit['structural_reference_price'] == pytest.approx(100.02)
    assert audit['production_matches_structural_reference'] is False


def test_ema9_continuation_extension_reference_is_structurally_aligned():
    data = market_data()
    for bar in data['candles']:
        bar['high'] = 101.5  # prevent MICRO_BREAKOUT from taking precedence
    decision = ScalpSignalEngine().evaluate('audit', 'ACME', quote(), data, now=NOW)
    audit = decision.features.entry_extension_reference
    assert decision.setup_type == 'EMA9_CONTINUATION'
    assert audit['production_reference_type'] == audit['structural_reference_type'] == 'EMA9'
    assert audit['production_matches_structural_reference'] is True


def test_vwap_reclaim_extension_audit_exposes_generic_ema9_reference():
    data = market_data()
    data['vwap'] = 100.1
    for bar in data['candles']:
        bar['high'] = 101.5
    decision = ScalpSignalEngine().evaluate('audit', 'ACME', quote(), data, now=NOW)
    audit = decision.features.entry_extension_reference
    assert decision.setup_type == 'VWAP_RECLAIM'
    assert audit['production_reference_type'] == 'EMA9'
    assert audit['structural_reference_type'] == 'VWAP'
    assert audit['structural_reference_price'] == 100.1
    assert audit['production_matches_structural_reference'] is False


def test_extension_penalty_and_hard_gate_are_independent_protections():
    decision = ScalpSignalEngine().evaluate(
        'extended', 'ACME',
        quote(bid=101, ask=101.04, last=101.02), market_data(), now=NOW,
    )
    penalty = decision.features.score_breakdown['entry_extension_penalty']
    expected = min(1.0, max(0.0, (
        decision.features.entry_extension - config.SCALP_MAX_EXTENSION_PCT
    ) / config.SCALP_MAX_EXTENSION_PCT))
    assert penalty['normalized'] == pytest.approx(expected)
    assert penalty['contribution'] == pytest.approx(-.20 * expected)
    assert decision.signal_score >= config.SCALP_MIN_SIGNAL_SCORE
    assert 'ENTRY_OVEREXTENDED' in decision.rejection_reasons
    assert decision.approved is False


def test_valid_nonextended_entry_passes_unchanged_extension_guard():
    decision = ScalpSignalEngine().evaluate(
        'valid', 'ACME', quote(), market_data(), now=NOW
    )
    assert decision.features.entry_extension <= config.SCALP_MAX_EXTENSION_PCT
    assert 'ENTRY_OVEREXTENDED' not in decision.rejection_reasons
    assert decision.approved is True


def test_volume_gate_name_is_not_execution_liquidity():
    data = market_data()
    data['relative_volume'] = 0.8
    decision = ScalpSignalEngine().evaluate('e', 'ACME', quote(), data, now=NOW)
    assert 'VOLUME_EXPANSION_BELOW_MINIMUM' in decision.rejection_reasons
    assert 'INSUFFICIENT_LIQUIDITY' not in decision.rejection_reasons


def test_entry_uses_ask_and_scalp_slippage(tmp_path, monkeypatch):
    _, _, position = opened(tmp_path, monkeypatch)
    expected = round(100.27*(1+config.SCALP_ENTRY_SLIPPAGE_BPS/10_000), 4)
    assert position.entry_price == expected
    assert position.quoted_entry_bid == 100.23 and position.quoted_entry_ask == 100.27
    assert position.estimated_entry_slippage_cost > 0
    assert position.strategy_id == 'SCALP'


def test_target_exit_uses_bid_and_exit_slippage_with_costs(tmp_path, monkeypatch):
    entry, portfolio, position = opened(tmp_path, monkeypatch)
    manager = ScalpPositionController(portfolio, setup_controller=entry.setup_controller,
                                      events=entry.events, engine=entry.engine)
    closed = manager.process_quotes({'ACME': quote(NOW+timedelta(seconds=10), bid=100.8, ask=100.82, last=100.81)},
                                    now=NOW+timedelta(seconds=10))
    trade = closed[0]
    assert trade.exit_reason == 'TARGET_HIT'
    assert trade.exit_price == round(100.8*(1-config.SCALP_EXIT_SLIPPAGE_BPS/10_000), 4)
    assert trade.gross_pnl != trade.net_pnl
    assert trade.entry_slippage_cost > 0 and trade.exit_slippage_cost > 0
    assert trade.holding_time_seconds == 10


def test_stop_overrides_all_soft_exit_logic(tmp_path, monkeypatch):
    entry, portfolio, _ = opened(tmp_path, monkeypatch)
    manager = ScalpPositionController(portfolio, setup_controller=entry.setup_controller,
                                      events=entry.events, engine=entry.engine)
    closed = manager.process_quotes(
        {'ACME': quote(NOW+timedelta(seconds=10), bid=100.5, ask=100.52, last=99.0)},
        lambda _: {'return_1': -.01, 'vwap': 101, 'ema9': 101}, now=NOW+timedelta(seconds=10))
    assert closed[0].exit_reason == 'STOP_HIT'


def test_time_exit_and_momentum_reversal(tmp_path, monkeypatch):
    entry, portfolio, _ = opened(tmp_path/'time', monkeypatch)
    manager = ScalpPositionController(portfolio, setup_controller=entry.setup_controller,
                                      events=entry.events, engine=entry.engine)
    assert manager.process_quotes({'ACME': quote(NOW+timedelta(seconds=181))},
        now=NOW+timedelta(seconds=181))[0].exit_reason == 'SCALP_TIME_EXIT'
    entry2, portfolio2, _ = opened(tmp_path/'momentum', monkeypatch)
    manager2 = ScalpPositionController(portfolio2, setup_controller=entry2.setup_controller,
                                       events=entry2.events, engine=entry2.engine)
    assert manager2.process_quotes({'ACME': quote(NOW+timedelta(seconds=30))},
        lambda _: {'return_1': -.002}, now=NOW+timedelta(seconds=30))[0].exit_reason == 'MOMENTUM_REVERSAL'


def test_eod_and_opt_in_profit_protection(tmp_path, monkeypatch):
    entry, portfolio, _ = opened(tmp_path/'eod', monkeypatch)
    manager = ScalpPositionController(portfolio, setup_controller=entry.setup_controller,
                                      events=entry.events, engine=entry.engine)
    eod = datetime(2026, 9, 18, 19, 56, tzinfo=timezone.utc)
    assert manager.process_quotes({'ACME': quote(eod)}, now=eod)[0].exit_reason == 'EOD_EXIT'
    monkeypatch.setattr(config, 'SCALP_PROFIT_PROTECTION_ENABLED', True)
    entry2, portfolio2, _ = opened(tmp_path/'protect', monkeypatch)
    manager2 = ScalpPositionController(portfolio2, setup_controller=entry2.setup_controller,
                                       events=entry2.events, engine=entry2.engine)
    assert manager2.process_quotes({'ACME': quote(NOW+timedelta(seconds=10), bid=100.55, ask=100.57)},
                                   now=NOW+timedelta(seconds=10)) == []
    closed = manager2.process_quotes({'ACME': quote(NOW+timedelta(seconds=20), bid=100.29, ask=100.31)},
                                     now=NOW+timedelta(seconds=20))
    assert closed[0].exit_reason == 'SCALP_PROFIT_PROTECTION_EXIT'


def test_same_evidence_episode_is_closed_and_new_bar_creates_new_episode(tmp_path):
    setup = ScalpSetupController(tmp_path/'setups.json')
    signal = ScalpSignalEngine(); data = market_data()
    features = signal.features('ACME', quote(), data, now=NOW)
    bars = completed_micro_bars(data['candles'], NOW)
    kind, evidence = signal.classify(features, bars)
    first = setup.episode('ACME', kind, evidence, bars[-1]['begins_at'], features, now=NOW)
    setup.close(first.episode_id, 'ACME')
    stale = setup.episode('ACME', kind, evidence, bars[-1]['begins_at'], features, now=NOW)
    assert stale.state == 'RESOLVED' and stale.episode_id == first.episode_id
    assert stale.new_episode_allowed is False
    later = NOW+timedelta(minutes=1)
    newer_data = market_data(later)
    newer_data['candles'][-1]['high'] = 101.0
    newer_data['candles'][-1]['close'] = 100.8
    newer_features = signal.features('ACME', quote(later), newer_data, now=later)
    newer_bars = completed_micro_bars(newer_data['candles'], later)
    second = setup.episode('ACME', kind, evidence, newer_bars[-1]['begins_at'], newer_features, now=later)
    assert second.episode_id != first.episode_id


def test_invalidated_structure_can_form_a_new_generation_after_reset(tmp_path):
    setup = ScalpSetupController(tmp_path/'setups.json')
    signal = ScalpSignalEngine()
    first_data = market_data(); first_features = signal.features('ACME', quote(), first_data, now=NOW)
    first_bars = completed_micro_bars(first_data['candles'], NOW)
    first = setup.episode('ACME', 'MICRO_BREAKOUT', ('a',), first_bars[-1]['begins_at'], first_features, now=NOW)
    later = NOW+timedelta(minutes=1); second_data = market_data(later)
    second_data['candles'][-1]['high'] = 101.0
    second_data['candles'][-1]['close'] = 100.8
    second_features = signal.features('ACME', quote(later), second_data, now=later)
    second_bars = completed_micro_bars(second_data['candles'], later)
    second = setup.episode('ACME', 'MICRO_BREAKOUT', ('b',), second_bars[-1]['begins_at'], second_features, now=later)
    returned = setup.episode('ACME', 'MICRO_BREAKOUT', ('a',), first_bars[-1]['begins_at'],
                             first_features, now=later)
    assert returned.state == 'FORMING'
    assert returned.episode_id not in {first.episode_id, second.episode_id}
    assert setup.closed_details[first.episode_id]['state'] == 'INVALIDATED'


@pytest.mark.parametrize('setup_type,anchor_type', [
    ('MICRO_BREAKOUT', 'PRIOR_COMPLETED_MICRO_HIGH'),
    ('MICRO_PULLBACK', 'EMA9_INTERACTION'),
    ('EMA9_CONTINUATION', 'EMA9_TREND'),
    ('VWAP_RECLAIM', 'VWAP_CROSS'),
    ('MOMENTUM_BURST', 'LATEST_COMPLETED_CLOSE'),
])
def test_setup_specific_fingerprint_fields_are_explicit(
    tmp_path, setup_type, anchor_type,
):
    signal = ScalpSignalEngine()
    features = signal.features('ACME', quote(), market_data(), now=NOW)
    controller = ScalpSetupController(tmp_path/f'{setup_type}.json')
    fields = controller.fingerprint_fields(setup_type, NOW.isoformat(), features)
    assert fields['setup_type'] == setup_type
    assert fields['anchor_type'] == anchor_type
    assert fields['completed_bar_timestamp'] == NOW.isoformat()
    assert set(fields) >= {
        'anchor_price', 'ema9', 'ema20', 'vwap', 'local_high', 'local_low',
        'volume_state', 'volume_expansion',
    }


def test_ema_and_vwap_changes_no_longer_collide_in_fingerprint(tmp_path):
    controller = ScalpSetupController(tmp_path/'setups.json')
    first = SimpleNamespace(
        recent_high=101.0, recent_low=99.0, ema9=100.0, ema20=99.8,
        vwap=99.9, volume_expansion=1.4, price=100.2,
        return_1=.002, volume_acceleration=.1,
    )
    changed = SimpleNamespace(**{
        **first.__dict__, 'ema9': 100.1, 'vwap': 100.0,
    })
    one = controller.evidence_key(
        'ACME', 'EMA9_CONTINUATION', NOW.isoformat(), first,
    )
    two = controller.evidence_key(
        'ACME', 'EMA9_CONTINUATION', NOW.isoformat(), changed,
    )
    assert one != two


def test_soft_failure_does_not_close_forming_episode(tmp_path):
    controller = ScalpSetupController(tmp_path/'setups.json')
    features = SimpleNamespace(
        recent_high=101.0, recent_low=99.0, ema9=100.0, ema20=99.8,
        vwap=99.9, volume_expansion=1.4, price=100.2,
        return_1=.002, volume_acceleration=.1,
    )
    first = controller.episode(
        'ACME', 'EMA9_CONTINUATION', ('trend',), NOW.isoformat(),
        features, now=NOW,
    )
    # A score rejection is deliberately not a state transition in the setup
    # controller. The next observation must see the same FORMING episode.
    second = controller.episode(
        'ACME', 'EMA9_CONTINUATION', ('trend',), NOW.isoformat(),
        features, now=NOW+timedelta(seconds=10),
    )
    assert first.episode_id == second.episode_id
    assert second.state == 'FORMING'
    assert first.episode_id not in controller.closed


def test_unchanged_episode_does_not_rewrite_full_state_each_quote(tmp_path):
    controller = ScalpSetupController(tmp_path/'setups.json')
    features = SimpleNamespace(
        recent_high=101.0, recent_low=99.0, ema9=100.0, ema20=99.8,
        vwap=99.9, volume_expansion=1.4, price=100.2,
        return_1=.002, volume_acceleration=.1,
    )
    writes = 0
    original_save = controller.save

    def counted_save():
        nonlocal writes
        writes += 1
        return original_save()

    controller.save = counted_save
    first = controller.episode(
        'ACME', 'EMA9_CONTINUATION', ('trend',), NOW.isoformat(),
        features, now=NOW,
    )
    controller.episode(
        'ACME', 'EMA9_CONTINUATION', ('trend',), NOW.isoformat(),
        features, now=NOW+timedelta(seconds=2),
    )
    assert writes == 1
    controller.record_latency_milestones(
        first.episode_id, 'ACME', {'first_eligible_at': NOW.isoformat()},
    )
    controller.record_latency_milestones(
        first.episode_id, 'ACME', {'first_eligible_at': NOW.isoformat()},
    )
    assert writes == 2


def test_completed_trade_is_protected_until_setup_specific_reset(tmp_path):
    controller = ScalpSetupController(tmp_path/'setups.json')
    breakout = SimpleNamespace(
        recent_high=101.0, recent_low=99.0, ema9=100.0, ema20=99.8,
        vwap=99.9, volume_expansion=1.4, price=101.2,
        return_1=.002, volume_acceleration=.1,
    )
    first = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('break',), NOW.isoformat(),
        breakout, now=NOW,
    )
    controller.transition(
        first.episode_id, 'ACME', 'READY', now=NOW, reason='SIGNAL_READY',
    )
    controller.transition(
        first.episode_id, 'ACME', 'ENTERED', now=NOW, reason='SHADOW_ENTRY',
    )
    controller.close(
        first.episode_id, 'ACME', now=NOW+timedelta(seconds=30),
        reason='TRADE_COMPLETED:TARGET_HIT',
    )
    duplicate = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('break',), NOW.isoformat(),
        breakout, now=NOW+timedelta(seconds=31),
    )
    assert duplicate.state == 'RESOLVED'
    assert duplicate.new_episode_allowed is False

    reset = SimpleNamespace(**{**breakout.__dict__, 'price': 100.9})
    reset_episode = controller.episode(
        'ACME', 'UNCLASSIFIED', (), NOW.isoformat(), reset,
        now=NOW+timedelta(seconds=40),
    )
    assert reset_episode.state == 'FORMING'
    new_breakout = SimpleNamespace(**{
        **breakout.__dict__, 'recent_high': 102.0, 'price': 102.2,
    })
    new_episode = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('new_break',),
        (NOW+timedelta(minutes=5)).isoformat(), new_breakout,
        now=NOW+timedelta(minutes=5),
    )
    assert new_episode.state == 'FORMING'
    assert new_episode.episode_id != first.episode_id


def test_overtrading_and_daily_loss_guards(tmp_path, monkeypatch):
    entry, _ = controller(tmp_path, monkeypatch)
    losses = [SimpleNamespace(symbol='ACME', net_pnl=-30,
                              estimated_slippage_cost=1) for _ in range(config.SCALP_MAX_TRADES_PER_SYMBOL)]
    monkeypatch.setattr(entry, '_session_trades', lambda now: losses)
    reasons = entry._session_limits('ACME', NOW)
    assert 'SCALP_SYMBOL_TRADE_LIMIT' in reasons
    assert 'SCALP_CONSECUTIVE_LOSS_LIMIT' in reasons
    assert 'SCALP_DAILY_LOSS_LIMIT' in reasons
    monkeypatch.setattr(config, 'SCALP_MAX_TRADES_PER_SESSION', len(losses))
    assert 'SCALP_SESSION_TRADE_LIMIT' in entry._session_limits('OTHER', NOW)


def test_momentum_and_scalp_cannot_hold_same_symbol(tmp_path, monkeypatch):
    from test_shadow_trading import coordinator as momentum, quote as slow_quote
    from shadow.execution import ShadowExecutionEngine
    monkeypatch.setattr(config, 'SCALP_ENABLED', True)
    portfolio = ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl')
    position, _ = ShadowExecutionEngine(portfolio).open_candidate(momentum(), slow_quote(as_of=NOW), now=NOW)
    assert position is not None
    entry = ScalpEntryController(portfolio, setup_path=tmp_path/'s.json', events_path=tmp_path/'e.jsonl')
    scalp, detail = entry.process('ACME', quote(), market_data(), now=NOW)
    assert scalp is None and detail['reason'] == 'EXISTING_POSITION_OTHER_STRATEGY'
    attribution = strategy_attribution(portfolio)
    assert attribution['MOMENTUM']['open_positions'] == 1
    assert attribution['SCALP']['open_positions'] == 0


def test_future_bar_high_low_never_enters_signal_features():
    data = market_data(future=True)
    features = ScalpSignalEngine().features('ACME', quote(), data, now=NOW)
    assert features.recent_high < 200 and features.recent_low > 90
    assert features.bar_count == 6


def test_restart_restores_open_scalp_and_duplicate_entry_is_blocked(tmp_path, monkeypatch):
    entry, portfolio, position = opened(tmp_path, monkeypatch)
    duplicate, detail = entry.process('ACME', quote(), market_data(), now=NOW)
    assert duplicate is None and detail['reason'] in {'DUPLICATE_POSITION', 'EPISODE_ALREADY_EXECUTED'}
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    restored = recovered.snapshot().open_positions[0]
    assert restored.trade_id == position.trade_id and restored.strategy_id == 'SCALP'
    assert restored.capital_allocated <= config.SHADOW_STARTING_CAPITAL*config.SCALP_MAX_POSITION_PERCENT*1.01


def test_duplicate_exit_does_not_double_credit(tmp_path, monkeypatch):
    entry, portfolio, position = opened(tmp_path, monkeypatch)
    manager = ScalpPositionController(portfolio, setup_controller=entry.setup_controller,
                                      events=entry.events, engine=entry.engine)
    q = quote(NOW+timedelta(seconds=10), bid=100.8, ask=100.82)
    assert len(manager.process_quotes({'ACME': q}, now=NOW+timedelta(seconds=10))) == 1
    cash = portfolio.snapshot().cash
    assert manager.process_quotes({'ACME': q}, now=NOW+timedelta(seconds=10)) == []
    assert portfolio.snapshot().cash == cash


def test_backtest_and_strategy_analytics_are_friction_aware():
    rows = [dict(market_data(), timestamp=NOW.isoformat(), quote_timestamp=NOW.isoformat(),
                 symbol='ACME', bid=100.23, ask=100.27, price=100.25, is_market_open=True),
            dict(market_data(NOW+timedelta(seconds=30)),
                 timestamp=(NOW+timedelta(seconds=30)).isoformat(),
                 quote_timestamp=(NOW+timedelta(seconds=30)).isoformat(), symbol='ACME',
                 bid=100.8, ask=100.82, price=100.81, is_market_open=True)]
    trades = ScalpBacktester().run(rows)
    assert trades and trades[0]['exit_reason'] == 'TARGET_HIT'
    summary = scalp_summary(trades)
    sensitivity = friction_sensitivity(trades)
    assert summary['trades'] == 1
    assert {'low','base','high','fragile'} <= sensitivity.keys()
    assert summary['by_spread_bucket']
    assert summary['by_holding_time']
    assert summary['by_time_of_day']
    assert summary['by_regime']
    assert summary['by_relative_strength_spy']


def test_scalp_events_and_future_policy_boundary_are_strategy_aware(tmp_path, monkeypatch):
    from strategies.scalp.policy import ScalpBaselinePolicy, ScalpPolicyComparison, ScalpAction
    entry, _, _ = opened(tmp_path, monkeypatch)
    rows = entry.events.journal.read()
    assert rows and all(row['strategy_id'] == 'SCALP' for row in rows)
    decision = rows[-2]['payload']['decision']
    assert decision['strategy_id'] == 'SCALP'
    comparison = ScalpPolicyComparison('SCALP', rows[-1]['episode_id'], NOW.isoformat(), 'ENTER')
    assert comparison.scalp_rl_action is None


def test_scalp_universe_is_independent_of_momentum_admission(tmp_path, monkeypatch):
    from strategies.scalp.market_data import ScalpMarketDataCache
    monkeypatch.setattr(config, 'SCALP_DISCOVERY_SYMBOLS', ())
    snapshot = tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'candidate_data': [], 'scalp_candidate_data': [
        {'symbol': 'SCALPONLY', 'candles': [], 'relative_volume': 2}
    ]}))
    cache = ScalpMarketDataCache(snapshot, context_store=None)
    assert cache.symbols() == ['SCALPONLY']
    assert cache.get('SCALPONLY')['relative_volume'] == 2


def test_scalp_market_data_reads_are_immutable(tmp_path, monkeypatch):
    from strategies.scalp.market_data import ScalpMarketDataCache
    monkeypatch.setattr(config, 'SCALP_DISCOVERY_SYMBOLS', ())
    snapshot = tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'candidate_data': [], 'scalp_candidate_data': [
        {'symbol': 'ACME', 'candles': [{'close': 10}], 'relative_volume': 2}
    ]}))
    cache = ScalpMarketDataCache(snapshot)
    first = cache.get('ACME')
    first['candles'][0]['close'] = 999
    assert cache.get('ACME')['candles'][0]['close'] == 10


def test_runtime_override_enables_only_the_scalp_instance(tmp_path, monkeypatch):
    from strategies.scalp.runtime import ScalpRuntime
    monkeypatch.setattr(config, 'SCALP_ENABLED', False)
    portfolio = ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl')
    runtime = ScalpRuntime(
        portfolio, lambda _: market_data(), universe=lambda: ['ACME'],
        setup_path=tmp_path/'s.json', events_path=tmp_path/'e.jsonl', enabled=True,
    )
    result = runtime.on_quotes({'ACME': quote()}, now=NOW)
    assert result['diagnostics']['funnel']['universe_observations'] == 1
    assert result['entries'][0]['status'] == 'OPENED'
    assert config.SCALP_ENABLED is False


def test_scalp_never_invokes_real_executor(tmp_path, monkeypatch):
    from execution.robinhood_executor import RobinhoodExecutor
    monkeypatch.setattr(RobinhoodExecutor, 'execute', lambda *a, **k: pytest.fail('real order path invoked'))
    _, portfolio, _ = opened(tmp_path, monkeypatch)
    assert portfolio.snapshot().open_positions[0].strategy_id == 'SCALP'
    assert config.MODE == 'SHADOW_TRADING'
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False


def test_scalp_entry_blocks_if_live_flags_or_kill_switch_are_unsafe(tmp_path, monkeypatch):
    entry, _ = controller(tmp_path/'flags', monkeypatch)
    monkeypatch.setattr(config, 'LIVE_TRADING_ENABLED', True)
    assert entry.process('ACME', quote(), market_data(), now=NOW)[1]['reason'] == 'SCALP_SAFETY_FLAGS_INVALID'

    monkeypatch.setattr(config, 'LIVE_TRADING_ENABLED', False)
    kill = tmp_path/'unblocked.json'
    kill.write_text('{"trading_blocked": false}')
    entry2 = ScalpEntryController(
        ShadowPortfolio(tmp_path/'kill-p.json', tmp_path/'kill-t.jsonl'),
        setup_path=tmp_path/'kill-s.json', events_path=tmp_path/'kill-e.jsonl',
        enabled=True, kill_switch_path=kill,
    )
    assert entry2.process('ACME', quote(), market_data(), now=NOW)[1]['reason'] == 'SCALP_KILL_SWITCH_NOT_BLOCKED'
