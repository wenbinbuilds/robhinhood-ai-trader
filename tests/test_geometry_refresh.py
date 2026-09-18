"""Structural evidence regressions: the RR gate never chooses the levels."""
from datetime import timedelta
from copy import deepcopy

import pytest
import config
from execution.geometry import completed_structure, geometry_diagnostics
from execution.pre_execution import PreExecutionValidator
from test_pre_execution_refresh import NOW, Refresher, risk, coordinator
NOW = NOW + timedelta(hours=2)


def bars(*, high=102, low=97, close=98):
    return [dict(begins_at=(NOW-timedelta(minutes=5*(40-i))).isoformat(),
                 open=close, high=high, low=low, close=close, volume=1000)
            for i in range(40)]


def evaluate(entry, candles, **tampered):
    original = dict(coordinator(), research_entry=100, research_stop=99, research_target=102)
    data = dict(symbol='ACME', current_price=entry, bid=entry-.01, ask=entry,
                quote_as_of=NOW.isoformat(), relative_volume=2,
                candles=candles, structure_evidence={}, **tampered)
    return PreExecutionValidator().evaluate(original, Refresher(data), risk(), now=NOW)


def test_a_unchanged_structure_small_drift_passes():
    result = evaluate(100.05, bars())
    assert result.approved
    assert result.plan.stop_price == 99
    assert result.plan.target_price == 102


def test_b_price_runs_into_unchanged_resistance_rejected():
    result = evaluate(101, bars())
    assert not result.approved
    detail = result.log_record['geometry_diagnostics']
    assert detail['RR_AT_RESEARCH_PRICE'] == 2
    assert detail['RR_AT_LIVE_ENTRY'] == .5
    assert detail['geometry_rejection_class'] == 'ENTRY_EXTENDED'
    assert detail['structural_maximum_entry'] == pytest.approx(100.2)


def test_c_completed_high_legitimately_restores_rr():
    candles = bars()
    candles[-1]['high'] = 106
    result = evaluate(101, candles)
    assert result.approved
    assert result.plan.target_price == 106
    assert result.log_record['geometry_diagnostics']['geometry_restored_valid_rr']


def test_d_stop_rises_only_from_recomputed_structure():
    result = evaluate(100.7, bars(high=102, low=99, close=99))
    assert result.approved
    assert result.plan.stop_price == 100  # completed-bar VWAP, not RR-derived
    assert 'vwap' in result.log_record['geometry_diagnostics']['stop_selection_reason']


def test_e_unsupported_target_inflation_cannot_pass():
    result = evaluate(101, bars(), intraday_resistance_reference=120, intraday_high=120)
    assert not result.approved
    assert result.log_record['refreshed_take_profit'] == 102


def test_f_artificial_stop_tightening_cannot_pass():
    result = evaluate(101, bars(), vwap=100.5, ema20=100.5, intraday_support_reference=100.5)
    assert not result.approved
    assert result.log_record['refreshed_stop_loss'] == 99


def test_forming_bar_does_not_expand_target():
    candles = bars()
    candles.append(dict(candles[-1], begins_at=(NOW-timedelta(minutes=1)).isoformat(), high=120))
    assert not evaluate(101, candles).approved


def test_stale_completed_structure_fails_closed():
    old = bars()[:-2]
    result = evaluate(100, old)
    assert not result.approved
    assert result.reason == 'COMPLETED_STRUCTURE_STALE'


def test_fast_stored_rr_does_not_prevent_confirmed_structural_refresh(tmp_path):
    from test_candidate_watchlist import watcher, context, quote, SequenceScorer, NOW as WATCH_NOW
    ctx = context(slow=.9, target=101.2)
    candidate, store, portfolio = watcher(tmp_path, ctx, SequenceScorer([1, 1]))
    assert candidate._hard_blocker(ctx, quote(), WATCH_NOW) is None
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    candidate.process_quotes({'ACME': quote(WATCH_NOW+timedelta(seconds=2))}, now=WATCH_NOW+timedelta(seconds=2))
    assert len(portfolio.snapshot().open_positions) == 1


def test_slow_overdue_eod_precedes_retrospective_target(tmp_path):
    from test_shadow_trading import opened, quote, candle, NOW as ENTRY
    portfolio, engine, position = opened(tmp_path)
    recovered = ENTRY+timedelta(days=1)
    candles = [candle(ENTRY+timedelta(minutes=5), 100, 106)]
    _, exits, _ = engine.monitor_positions(lambda _: quote(103, candles=candles, as_of=recovered), now=recovered)
    assert exits[0]['exit_reason'] == 'MISSED_EOD_RECOVERY_EXIT'
    assert exits[0]['exit_method'] != 'RECONSTRUCTED_FROM_BAR_DATA'
    assert exits[0]['exit_timestamp'] == recovered.isoformat()
    assert 'MISSED_EOD_MONITORING_WINDOW' in exits[0]['warnings'][0]


def test_fast_overdue_eod_precedes_target(tmp_path):
    from test_fast_watcher import setup_watcher, Quotes, NOW as ENTRY
    recovered = ENTRY+timedelta(days=1)
    watcher, portfolio, *_ = setup_watcher(tmp_path, Quotes(106, at=recovered), clock=lambda: recovered)
    watcher.tick()
    assert portfolio.snapshot().closed_positions[0].exit_reason == 'MISSED_EOD_RECOVERY_EXIT'


def test_geometry_summary_uses_actual_paired_attempts():
    from shadow.session_audit import ShadowSessionAudit
    rejected = evaluate(101, bars())
    changed = bars()
    changed[-1]['high'] = 106
    approved = evaluate(101, changed)
    summary = ShadowSessionAudit._entry_geometry(
        [rejected.log_record, approved.log_record], []
    )
    assert summary['attempts_with_geometry_evidence'] == 2
    assert summary['RR_AT_RESEARCH']['mean'] == 2
    assert summary['rr_failures_crossing_boundary_due_to_entry_drift'] == 1
    assert summary['refreshed_geometry_restored_valid_rr'] == 1
    assert summary['correctly_rejected_extended'] == 1


def test_timeout_blocks_context_without_terminal_rejection(tmp_path):
    from test_candidate_watchlist import watcher, context, quote, SequenceScorer, NOW as WATCH_NOW
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1, 1, 1]))
    provider = Refresher(error=TimeoutError())
    candidate.pre_execution_refresher = provider
    candidate.process_quotes({'ACME': quote()}, now=WATCH_NOW)
    later = WATCH_NOW+timedelta(seconds=2)
    candidate.process_quotes({'ACME': quote(later)}, now=later)
    candidate.process_quotes({'ACME': quote(later)}, now=later)
    assert len(provider.calls) == 1
    assert store.snapshot()[0].candidate_state == 'INFRASTRUCTURE_BLOCKED'
    store.replace([context(slow=.9, at=later)], now=later)
    assert store.snapshot()[0].candidate_state == 'SETUP_FORMING'
