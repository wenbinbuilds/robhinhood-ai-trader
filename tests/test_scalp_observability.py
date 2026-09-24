import json
from datetime import datetime, timezone

import config
from strategies.scalp.analytics import scalp_session_summary, scalp_summary
from strategies.scalp.diagnostics import (
    STAGE_NAMES, ScalpDiagnosticsJournal, candidate_trace, cycle_diagnostics,
)


NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def decision(**overrides):
    value = {
        'strategy_id': 'SCALP', 'episode_id': 'SCALP-ACME-1',
        'symbol': 'ACME', 'setup_type': 'MICRO_BREAKOUT',
        'setup_evidence': ['completed_breakout'], 'signal_score': .80,
        'expected_move_pct': .004, 'estimated_cost_pct': .0009,
        'expected_net_edge_pct': .0031, 'entry_price': 100.0,
        'stop': 99.8, 'target': 100.4, 'risk_pct': .002,
        'reward_pct': .004, 'risk_reward_ratio': 2.0,
        'net_reward_pct': .0031, 'net_risk_reward_ratio': 1.55,
        'features': {'spread_pct': .0004},
    }
    value.update(overrides)
    return value


def quality(**overrides):
    value = {'provider_status': 'OK', 'quote_status': 'FRESH',
             'quote_age_seconds': .5, 'spread_pct': .0004}
    value.update(overrides)
    return value


def trace(*, reasons=(), details=None, quality_value=None, market=None):
    detail = {'symbol': 'ACME', 'episode_id': 'SCALP-ACME-1',
              'decision': decision(), 'reasons': list(reasons),
              'entry_attempted': False}
    detail.update(details or {})
    return candidate_trace(
        cycle_id='cycle-1', symbol='ACME',
        quality=quality_value or quality(),
        market_data=market or {'relative_volume': 2,
                               'relative_volume_source': 'PROVIDER'},
        detail=detail, now=NOW,
    )


def test_strict_funnel_counters_are_monotonic_and_independent_signal_is_labeled():
    stale = trace(reasons=['STALE_QUOTE'],
                  quality_value=quality(quote_status='STALE', quote_age_seconds=3))
    illiquid = trace(reasons=['VOLUME_EXPANSION_BELOW_MINIMUM'],
                     market={'relative_volume': .8,
                             'relative_volume_source': 'PROVIDER'})
    success = trace(details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': True,
        'safety_approved': True, 'status': 'OPENED',
    })
    cycle = cycle_diagnostics(
        cycle_id='cycle-1', traces=[stale, illiquid, success],
        source='TEST', now=NOW,
    )
    funnel = cycle['funnel']
    ordered = list(STAGE_NAMES)
    assert all(funnel[left] >= funnel[right]
               for left, right in zip(ordered, ordered[1:]))
    assert funnel['micro_signals_detected_anywhere'] == 3
    assert funnel['eligible_micro_signals'] == 1
    assert funnel['micro_signals_detected_anywhere'] > funnel['liquidity_pass']


def test_reasons_are_aggregated_and_filtered_is_distinct_from_entry_blocked():
    early = trace(reasons=['STALE_QUOTE', 'SIGNAL_SCORE_BELOW_THRESHOLD'],
                  quality_value=quality(quote_status='STALE', quote_age_seconds=4))
    late = trace(reasons=['INSUFFICIENT_NET_EDGE'])
    cycle = cycle_diagnostics(cycle_id='c', traces=[early, late], source='TEST', now=NOW)
    assert cycle['rejection_reasons']['STALE_QUOTE'] == 1
    assert cycle['rejection_reasons']['INSUFFICIENT_NET_EDGE'] == 1
    assert early['rejection_class'] == 'FILTERED'
    assert late['rejection_class'] == 'ENTRY_BLOCKED'
    assert early['blocking_reasons'] == ['STALE_QUOTE']
    assert late['blocking_stage'] == 'expected_edge_pass'
    assert cycle['filtered_reasons'] == {'STALE_QUOTE': 1}
    assert cycle['entry_blocked_reasons'] == {'INSUFFICIENT_NET_EDGE': 1}


def test_early_filters_and_micro_signal_do_not_count_as_entry_attempts():
    for reason in ('STALE_QUOTE', 'SPREAD_TOO_WIDE', 'VOLUME_EXPANSION_BELOW_MINIMUM'):
        item = trace(reasons=[reason])
        assert item['setup_detected_anywhere'] is True
        assert item['stage_flags']['entry_attempts'] is False
    edge = trace(reasons=['INSUFFICIENT_NET_EDGE'])
    assert edge['stage_flags']['eligible_micro_signals'] is True
    assert edge['stage_flags']['entry_attempts'] is False
    low_score = trace(reasons=['SIGNAL_SCORE_BELOW_THRESHOLD'])
    assert low_score['stage_flags']['eligible_micro_signals'] is True
    assert low_score['stage_flags']['signal_score_pass'] is False
    assert low_score['stage_flags']['entry_attempts'] is False


def test_pre_execution_risk_portfolio_and_entry_counters_are_separate():
    preexec = trace(reasons=['LOW_RISK_REWARD'], details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': False,
    })
    risk = trace(reasons=['SCALP_RISK_REJECTED'], details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': False,
    })
    portfolio = trace(reasons=['MAX_POSITIONS'], details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': False,
    })
    opened = trace(details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': True,
        'safety_approved': True, 'status': 'OPENED',
    })
    diagnostics = cycle_diagnostics(
        cycle_id='c', traces=[preexec, risk, portfolio, opened],
        source='TEST', now=NOW,
    )
    cycle = diagnostics['funnel']
    assert cycle['entry_attempts'] == 4
    assert cycle['risk_attempts'] == 4 and cycle['risk_approved'] == 3
    assert cycle['pre_execution_attempts'] == 3
    assert cycle['pre_execution_passes'] == 2
    assert cycle['portfolio_attempts'] == 2
    assert cycle['portfolio_approved'] == 1
    assert cycle['entries'] == 1
    assert diagnostics['pre_execution_failure_reasons'] == {'LOW_RISK_REWARD': 1}
    assert diagnostics['risk_rejection_reasons'] == {'SCALP_RISK_REJECTED': 1}
    assert diagnostics['portfolio_block_reasons'] == {'MAX_POSITIONS': 1}


def test_same_episode_entry_is_counted_once_and_duplicate_is_visible():
    opened = trace(details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': True,
        'safety_approved': True, 'status': 'OPENED',
    })
    duplicate = trace(reasons=['DUPLICATE_POSITION'], details={
        'decision': None, 'episode_id': None, 'entry_attempted': False,
    })
    cycle = cycle_diagnostics(
        cycle_id='c', traces=[opened, duplicate], source='TEST', now=NOW,
    )['funnel']
    assert cycle['entries'] == 1
    assert cycle['duplicate_exposure_blocked'] == 1


def test_same_episode_cannot_double_count_an_entry_in_runtime(tmp_path, monkeypatch):
    from shadow.portfolio import ShadowPortfolio
    from strategies.scalp.runtime import ScalpRuntime
    from test_scalp_strategy import market_data, quote
    monkeypatch.setattr(config, 'SCALP_ENABLED', False)
    runtime = ScalpRuntime(
        ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl'),
        lambda _: market_data(), universe=lambda: ['ACME'],
        setup_path=tmp_path/'s.json', events_path=tmp_path/'e.jsonl',
        diagnostics_path=tmp_path/'d.jsonl', enabled=True,
    )
    first = runtime.on_quotes({'ACME': quote()}, now=NOW)
    second = runtime.on_quotes({'ACME': quote()}, now=NOW)
    assert first['diagnostics']['funnel']['entries'] == 1
    assert second['diagnostics']['funnel']['entries'] == 0
    assert second['diagnostics']['funnel']['duplicate_exposure_blocked'] == 1
    assert sum(result['diagnostics']['funnel']['entries']
               for result in (first, second)) == 1
    latency = first['traces'][0]['latency']
    assert latency['exchange_timestamp'] is not None
    assert latency['feature_compute_duration_ms'] >= 0
    assert latency['setup_compute_duration_ms'] >= 0
    assert latency['geometry_compute_duration_ms'] >= 0
    assert latency['risk_duration_ms'] >= 0
    assert latency['preexecution_duration_ms'] >= 0
    assert latency['shadow_entry_requested_at'] is not None
    assert latency['shadow_position_created_at'] is not None
    assert latency['shadow_execution_duration_ms'] >= 0
    persisted = json.loads((tmp_path/'s.json').read_text())
    milestones = persisted['active_episodes'][0]['latency_milestones']
    assert milestones['first_eligible_at']
    assert milestones['first_score_070_at']
    assert milestones['first_edge_pass_at']
    assert milestones['first_rr_pass_at']


def test_rejection_reason_classification_and_detail_payloads():
    cases = {
        'STALE_QUOTE': ('freshness', 'STALE'),
        'SPREAD_TOO_WIDE': ('spread', False),
        'VOLUME_EXPANSION_BELOW_MINIMUM': ('volume_expansion', 'INSUFFICIENT'),
        'INSUFFICIENT_NET_EDGE': ('friction', False),
        'INVALID_STOP': ('geometry', False),
    }
    for reason, (section, expected) in cases.items():
        kwargs = {}
        market = None
        if reason == 'STALE_QUOTE':
            kwargs['quality_value'] = quality(quote_status='STALE', quote_age_seconds=3)
        if reason == 'SPREAD_TOO_WIDE':
            kwargs['quality_value'] = quality(spread_pct=.002)
        if reason == 'VOLUME_EXPANSION_BELOW_MINIMUM':
            market = {'relative_volume': .5, 'relative_volume_source': 'PROVIDER'}
        if reason == 'INSUFFICIENT_NET_EDGE':
            kwargs['details'] = {'decision': decision(expected_net_edge_pct=.0001)}
        item = trace(reasons=[reason], market=market, **kwargs)
        if reason == 'STALE_QUOTE': assert item[section]['quote_status'] == expected
        elif reason == 'SPREAD_TOO_WIDE': assert item[section]['passed'] is expected
        elif reason == 'VOLUME_EXPANSION_BELOW_MINIMUM': assert item[section]['status'] == expected
        elif reason == 'INSUFFICIENT_NET_EDGE': assert item[section]['passed'] is expected
        else: assert item[section]['structurally_valid'] is expected


def test_session_summary_denominators_and_strategy_isolation(tmp_path):
    stale = trace(reasons=['STALE_QUOTE'],
                  quality_value=quality(quote_status='STALE', quote_age_seconds=3))
    success = trace(details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': True,
        'safety_approved': True, 'status': 'OPENED',
    })
    cycle = cycle_diagnostics(
        cycle_id='cycle-1', traces=[stale, success], source='TEST', now=NOW,
    )
    path = tmp_path/'diagnostics.jsonl'
    ScalpDiagnosticsJournal(path).record(cycle, [stale, success])
    momentum = {'strategy_id': 'MOMENTUM', 'net_pnl': 999, 'gross_pnl': 999,
                'exit_timestamp': NOW.isoformat()}
    summary = scalp_session_summary(path, [momentum], now=NOW)
    assert summary['candidate_observations'] == 2
    assert summary['rates']['fresh_quote'] == {
        'count': 1, 'denominator': 2, 'percent': 50.0,
    }
    assert summary['shadow_entries'] == 1
    assert summary['exits'] == 0
    assert summary['quote_age']['fresh'] == 1
    assert summary['quote_age']['stale'] == 1
    assert summary['spread_distribution']['pass_count'] == 1
    assert summary['setup_types']['MICRO_BREAKOUT'] == {
        'detected_anywhere': 2,
        'eligible_after_early_gates': 1,
        'entry_attempts': 1,
        'entries': 1,
    }
    assert scalp_summary([momentum])['trades'] == 0


def test_diagnostics_journal_persists_friction_geometry_and_liquidity(tmp_path):
    item = trace()
    cycle = cycle_diagnostics(cycle_id='cycle-1', traces=[item], source='TEST', now=NOW)
    path = tmp_path/'diagnostics.jsonl'
    ScalpDiagnosticsJournal(path).record(cycle, [item])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    candidate = next(row['payload'] for row in rows
                     if row['record_type'] == 'CANDIDATE_OBSERVATION')
    assert candidate['friction']['spread_cost_pct'] == .0004
    assert candidate['friction']['entry_slippage_pct'] == config.SCALP_ENTRY_SLIPPAGE_BPS/10_000
    assert candidate['geometry']['gross_rr'] == 2.0
    assert candidate['liquidity']['observed'] == 2


def test_entry_quality_joins_closed_scalp_trade_by_episode(tmp_path):
    opened = trace(details={
        'entry_attempted': True, 'risk_attempted': True, 'risk_approved': True,
        'pre_execution_attempted': True, 'pre_execution_passed': True,
        'portfolio_attempted': True, 'portfolio_approved': True,
        'safety_approved': True, 'status': 'OPENED',
    })
    cycle = cycle_diagnostics(
        cycle_id='cycle-1', traces=[opened], source='TEST', now=NOW,
    )
    path = tmp_path/'diagnostics.jsonl'
    ScalpDiagnosticsJournal(path).record(cycle, [opened])
    closed = {
        'strategy_id': 'SCALP', 'episode_id': 'SCALP-ACME-1',
        'exit_timestamp': NOW.isoformat(), 'holding_time_seconds': 42,
        'exit_reason': 'TARGET_HIT', 'gross_pnl': 12,
        'estimated_spread_cost': 1, 'estimated_slippage_cost': 2,
        'net_pnl': 9,
    }
    row = scalp_session_summary(path, [closed], now=NOW)['entry_quality'][0]
    assert row['setup_type'] == 'MICRO_BREAKOUT'
    assert row['expected_net_edge_pct'] == .0031
    assert row['holding_seconds'] == 42
    assert row['friction'] == 3
    assert row['net_pnl'] == 9


def test_debug_mode_only_changes_output(tmp_path, monkeypatch, capsys):
    from shadow.portfolio import ShadowPortfolio
    from strategies.scalp.runtime import ScalpRuntime
    from test_scalp_strategy import market_data, quote
    monkeypatch.setattr(config, 'SCALP_ENABLED', False)

    def run(name, debug):
        root = tmp_path/name
        runtime = ScalpRuntime(
            ShadowPortfolio(root/'p.json', root/'t.jsonl'),
            lambda _: market_data(), universe=lambda: ['ACME'],
            setup_path=root/'s.json', events_path=root/'e.jsonl',
            diagnostics_path=root/'d.jsonl', enabled=True, debug=debug,
        )
        return runtime.on_quotes({'ACME': quote()}, now=NOW)

    normal = run('normal', False)
    normal_output = capsys.readouterr().out
    debug = run('debug', True)
    debug_output = capsys.readouterr().out
    assert normal['diagnostics']['funnel']['entries'] == 1
    assert debug['diagnostics']['funnel']['entries'] == 1
    assert '[SCALP ENTRY]' in normal_output
    assert 'FINAL=SHADOW_ENTRY' not in normal_output
    assert 'FINAL=SHADOW_ENTRY' in debug_output
    assert config.SCALP_ENABLED is False


def test_debug_prints_early_candidate_failure_without_an_entry_attempt(tmp_path, monkeypatch, capsys):
    from shadow.portfolio import ShadowPortfolio
    from strategies.scalp.runtime import ScalpRuntime
    from test_scalp_strategy import market_data, quote
    monkeypatch.setattr(config, 'SCALP_ENABLED', False)
    runtime = ScalpRuntime(
        ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl'),
        lambda _: market_data(), universe=lambda: ['ACME'],
        setup_path=tmp_path/'s.json', events_path=tmp_path/'e.jsonl',
        diagnostics_path=tmp_path/'d.jsonl', enabled=True, debug=True,
    )
    result = runtime.on_quotes({'ACME': quote()}, entry_quotes={}, now=NOW,
        quote_quality={'ACME': {'provider_status': 'UNAVAILABLE',
                                'quote_status': 'UNAVAILABLE',
                                'quote_age_seconds': None, 'spread_pct': None}})
    output = capsys.readouterr().out
    assert result['diagnostics']['funnel']['entry_attempts'] == 0
    assert 'FINAL=FILTERED' in output
    assert 'QUOTE_UNAVAILABLE' in output
