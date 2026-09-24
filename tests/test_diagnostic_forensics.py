"""Deterministic observability regressions; no broker access or execution."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

import config
from agent.cycle_diagnostics import position_watch_candidate_traces
from execution.pre_execution import PreExecutionValidator
from strategies.scalp.analytics import scalp_session_summary
from strategies.scalp.diagnostics import candidate_trace, cycle_diagnostics
from strategies.scalp.runtime import ScalpRuntime
from strategies.scalp.setup import ScalpSetupController
from watcher.fast_watcher import scalp_status_summary
from watcher.models import FastQuote
from watcher.quote_diagnostics import quote_provenance_summary
from watcher.quote_provider import quote_provenance


NOW = datetime(2026, 9, 23, 16, 40, 32, tzinfo=timezone.utc)


def _quote(*, exchange_age=0, cache=False, latency=.2):
    return FastQuote(
        symbol='ANDG', bid=53.83, ask=53.92, last_price=53.825,
        timestamp=NOW-timedelta(seconds=exchange_age), source='TEST',
        is_market_open=True, received_at=NOW,
        request_started_at=NOW-timedelta(seconds=latency),
        request_finished_at=NOW, provider_latency_seconds=latency,
        provider_status='OK', cache_hit=cache, cache_key='snapshot' if cache else None,
        cache_created_at=(NOW-timedelta(seconds=exchange_age) if cache else None),
        poll_cycle_id='poll-1',
    )


def test_quote_age_provenance_classifies_provider_old_and_cache_stale():
    old = quote_provenance(
        _quote(exchange_age=223), symbol='ANDG', evaluation_at=NOW,
        maximum_age_seconds=2, provider_status='OK', poll_cycle_id='poll-1',
    )
    assert old['stale_reason'] == 'PROVIDER_RETURNED_OLD_EXCHANGE_TIMESTAMP'
    assert old['exchange_quote_age_seconds'] == 223
    assert old['provider_latency_ms'] == pytest.approx(200)
    cached = quote_provenance(
        _quote(exchange_age=30, cache=True), symbol='ANDG', evaluation_at=NOW,
        maximum_age_seconds=2, provider_status='OK', poll_cycle_id='poll-2',
    )
    assert cached['stale_reason'] == 'LOCAL_CACHE_REUSE'
    assert cached['local_cache_age_seconds'] == 30


def test_quote_not_polled_and_symbol_not_refreshed_are_distinct():
    not_polled = quote_provenance(
        None, symbol='ACME', evaluation_at=NOW, maximum_age_seconds=2,
        provider_status='OK', requested_this_cycle=False,
    )
    requested = quote_provenance(
        None, symbol='ACME', evaluation_at=NOW, maximum_age_seconds=2,
        provider_status='OK', requested_this_cycle=True,
    )
    assert not_polled['stale_reason'] == 'QUOTE_NOT_POLLED_THIS_CYCLE'
    assert requested['stale_reason'] == 'SYMBOL_NOT_REFRESHED'


def test_per_symbol_polling_cadence_and_quote_distribution(tmp_path):
    path = tmp_path/'quotes.jsonl'
    rows = []
    for index, age in enumerate((.5, 1.5, 3.5)):
        rows.append({
            'timestamp': (NOW+timedelta(seconds=index*2)).isoformat(),
            'event': 'QUOTE_REQUEST_TRACE', 'symbol': 'ANDG',
            'poll_cycle_id': f'poll-{index}', 'strategy_scopes': ['SCALP'],
            'requested_this_cycle': True, 'provider_status': 'OK',
            'provider_latency_ms': 200+index, 'exchange_quote_age_seconds': age,
            'poll_interval_since_previous_seconds': None if index == 0 else 2.0,
            'full_universe_cycle_duration_seconds': .25,
            'freshness_by_strategy': {'SCALP': age <= 2},
            'cache_hit': False, 'exchange_timestamp': NOW.isoformat(),
            'stale_reason': None if age <= 2 else 'PROVIDER_RETURNED_OLD_EXCHANGE_TIMESTAMP',
        })
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
    report = quote_provenance_summary(path, strategy='SCALP')
    assert report['poll_cycles'] == 3
    assert report['quote_age']['median'] == 1.5
    assert report['quote_age']['percent_le_2s'] == pytest.approx(200/3)
    assert report['per_symbol']['ANDG']['median_poll_interval_seconds'] == 2
    assert report['per_symbol']['ANDG']['freshness_pass_rate_percent'] == pytest.approx(200/3)


def _eligible_trace():
    components = {
        'momentum': {'raw': .1, 'normalized': .8, 'weight': .5,
                     'contribution': .4, 'clamped_min': False, 'clamped_max': False},
        'extension_penalty': {'raw': .2, 'normalized': .5, 'weight': -.2,
                              'contribution': -.1, 'clamped_min': False, 'clamped_max': False},
    }
    decision = {
        'episode_id': 'episode', 'setup_type': 'EMA9_CONTINUATION',
        'setup_evidence': ['ema'], 'signal_score': .30,
        'entry_price': 100, 'stop': 99.7, 'target': 100.5,
        'risk_pct': .003, 'reward_pct': .005, 'risk_reward_ratio': 1.666,
        'net_reward_pct': .004, 'net_risk_reward_ratio': 1.333,
        'expected_move_pct': .005, 'estimated_cost_pct': .001,
        'expected_net_edge_pct': .004,
        'features': {'score_breakdown': components, 'signal_data_status': 'VALID',
                     'entry_extension': .001},
    }
    return candidate_trace(
        cycle_id='c', symbol='ACME',
        quality={'provider_status': 'OK', 'quote_status': 'FRESH',
                 'quote_age_seconds': .5, 'spread_pct': .0004},
        market_data={'relative_volume': 1.3},
        detail={'decision': decision, 'reason': 'SIGNAL_SCORE_BELOW_THRESHOLD',
                'reasons': ['SIGNAL_SCORE_BELOW_THRESHOLD'],
                'episode_id': 'episode', 'entry_attempted': False,
                'episode_lifecycle': {'episode_status': 'FORMING',
                                      'episode_age_seconds': 12}},
        now=NOW,
    )


def test_eligible_candidate_trace_score_contribution_sum_margin_and_print(capsys):
    trace = _eligible_trace()
    assert trace['stage_flags']['eligible_micro_signals']
    assert trace['signal']['score_before_penalties'] == pytest.approx(.4)
    assert trace['signal']['total_penalties'] == pytest.approx(.1)
    assert trace['signal']['score'] == pytest.approx(.3)
    assert trace['signal']['score_margin'] == pytest.approx(-.4)
    ScalpRuntime._print_eligible_candidate(trace)
    output = capsys.readouterr().out
    assert '[SCALP CANDIDATE]' in output
    assert 'final_block_reason=SIGNAL_SCORE_BELOW_THRESHOLD' in output
    assert 'score_before_penalties=0.4' in output


def test_eligible_distribution_and_ranked_downstream_counterfactual(tmp_path):
    trace = _eligible_trace()
    cycle = cycle_diagnostics(cycle_id='c', traces=[trace], source='TEST', now=NOW)
    from strategies.scalp.diagnostics import ScalpDiagnosticsJournal
    path = tmp_path/'scalp.jsonl'
    ScalpDiagnosticsJournal(path).record(cycle, [trace])
    report = scalp_session_summary(path, [], now=NOW)
    assert report['eligible_signal_score_distribution']['count'] == 1
    assert report['eligible_score_by_setup']['EMA9_CONTINUATION']['eligible'] == 1
    top = report['top_20_rejected_scalp_candidates_by_score'][0]
    assert top['score'] == .3
    assert top['offline_downstream_counterfactual']['geometry_pass'] is True
    assert top['final_rejection'] == 'SIGNAL_SCORE_BELOW_THRESHOLD'


def test_stale_episode_lifecycle_and_structural_reset_creates_new_identity(tmp_path):
    controller = ScalpSetupController(tmp_path/'episodes.json')
    old = SimpleNamespace(recent_high=101.0, recent_low=99.0)
    first = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('break',), NOW.isoformat(), old, now=NOW,
    )
    controller.close(first.episode_id, 'ACME')
    stale = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('break',), NOW.isoformat(), old,
        now=NOW+timedelta(seconds=10),
    )
    assert stale.state == 'CLOSED'
    assert stale.stale_reason == 'EPISODE_ID_PREVIOUSLY_CLOSED'
    assert stale.stale_after_seconds is None
    reset = SimpleNamespace(recent_high=102.0, recent_low=100.0)
    new = controller.episode(
        'ACME', 'MICRO_BREAKOUT', ('new_break',),
        (NOW+timedelta(minutes=5)).isoformat(), reset,
        now=NOW+timedelta(minutes=5),
    )
    assert new.state == 'FORMING'
    assert new.episode_id != first.episode_id
    assert new.structure_changed is True
    assert new.new_episode_allowed is True


def _position_result():
    rules = [
        {'rule_name': 'QUOTE_FRESHNESS', 'type': 'HARD', 'status': 'PASS'},
        {'rule_name': 'STOP_REFERENCE_AVAILABLE', 'type': 'HARD',
         'status': 'PASS', 'actual': 53.34978793618252},
        {'rule_name': 'RESISTANCE_ABOVE_ENTRY', 'type': 'HARD',
         'status': 'FAIL', 'actual': {'entry': 53.92, 'resistance': 53.84}},
        {'rule_name': 'MINIMUM_RISK_REWARD', 'type': 'HARD',
         'status': 'NOT_EVALUATED', 'actual': -0.14029868022154957},
    ]
    return {'analyzed_candidates': [{
        'symbol': 'ANDG',
        'coordinator_decision': {
            'combined_score': .602, 'technical_score': .769,
            'decision': 'NO_TRADE',
            'true_hard_gate_failures': ['RESISTANCE_ABOVE_ENTRY'],
        },
        'supporting_indicators': {'intraday_support_reference': 52.03},
        'deterministic_technical_metrics': {
            'quote_age_at_analysis_seconds': 223.178124,
            'quote_as_of': '2026-09-23T16:36:49.536387971Z',
            'latest_completed_bar_timestamp': '2026-09-23T16:35:00Z',
            'analysis_started_at': '2026-09-23T16:40:32.714511+00:00',
            'technical_validation': {
                'rules': rules,
                'true_hard_gate_failures': ['RESISTANCE_ABOVE_ENTRY'],
                'signal_quality_failures': ['MINIMUM_CONFIDENCE'],
            },
        },
    }]}


def test_position_candidate_above_point_60_and_resistance_calculation_trace():
    trace = position_watch_candidate_traces(_position_result())[0]
    assert trace['symbol'] == 'ANDG'
    assert trace['combined_score'] == .602
    assert trace['primary_block'] == 'RESISTANCE_ABOVE_ENTRY'
    assert trace['resistance_distance_from_entry'] == pytest.approx(-.08)
    assert trace['minimum_required_resistance_distance_for_RR'] == pytest.approx(
        1.5*(53.92-53.34978793618252)
    )
    assert trace['minimum_resistance_price_for_RR'] == pytest.approx(54.7753180957)
    assert trace['gross_RR'] == pytest.approx(-.1402986802)
    assert trace['quote_fresh'] is True  # Passes the unchanged 300s slow gate.
    assert trace['coherent_geometry_timestamps'] is False


def test_coherent_geometry_timestamps_and_incoherent_mix():
    coherent = {
        'quote_as_of': NOW.isoformat(), 'refresh_completed_at': NOW.isoformat(),
        'structure_evidence': {'latest_completed_bar': (NOW-timedelta(minutes=5)).isoformat()},
        'candles': [{'begins_at': (NOW-timedelta(minutes=5)).isoformat(),
                     'interval_seconds': 300}],
    }
    evidence = PreExecutionValidator.geometry_timestamp_evidence(coherent, NOW)
    assert evidence['coherent'] is True
    incoherent = dict(coherent, quote_as_of=(NOW-timedelta(minutes=4)).isoformat())
    assert PreExecutionValidator.geometry_timestamp_evidence(
        incoherent, NOW,
    )['coherent'] is False


def test_status_summary_separates_universe_filter_from_eligible_blocker():
    summary = scalp_status_summary({
        'filtered_reasons': {'STALE_QUOTE': 4, 'SPREAD_TOO_WIDE': 1},
        'entry_blocked_reasons': {'SIGNAL_SCORE_BELOW_THRESHOLD': 2},
    })
    assert summary['universe_primary_filter'] == 'STALE_QUOTE'
    assert summary['eligible_candidate_primary_block'] == 'SIGNAL_SCORE_BELOW_THRESHOLD'
    assert summary['universe_filter_counts'] != summary['eligible_candidate_block_counts']


def test_debug_pass_did_not_change_safety_thresholds_or_position_weights():
    assert config.MODE == 'SHADOW_TRADING'
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False
    assert config.SCALP_MAX_QUOTE_AGE_SECONDS == 2
    assert config.SCALP_MIN_SIGNAL_SCORE == .70
    assert config.COORDINATOR_NO_TRADE_THRESHOLD == .60
    assert config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD == .72
    assert config.COORDINATOR_WEIGHTS == {
        'technical': .70, 'news': .10, 'sector': .05,
        'market': .05, 'qualitative': .10,
    }
