"""Canonical ownership, durable local facts, and no broker execution."""
from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
import json

import pytest
import config
from test_candidate_watchlist import context, watcher, quote, SequenceScorer, NOW
from test_shadow_trading import opened
from shadow.portfolio import ShadowPortfolio
from event_driven.state import CandidateStateStore, CandidateState
from event_driven.events import PositionOpenedEvent, PositionClosedEvent, RiskRejectedEvent
from trading_runtime.reconciliation import ReconciliationService
from trading_runtime.setup_controller import SetupController, episode_id
from trading_runtime.contracts import MarketSnapshot, Quality
from trading_runtime.journal import EventJournal, RuntimeEvent, RuntimeEventType


def test_reconcile_missing_canonical_position(tmp_path):
    portfolio = ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl')
    store = CandidateStateStore(tmp_path/'c.json')
    store.project_position('ACME', CandidateState.POSITION_OPEN, now=NOW, episode_id='old', reason='fixture')
    events = []
    assert not ReconciliationService(portfolio, store, events.append).reconcile('ACME', now=NOW)
    assert store.get('ACME').state == CandidateState.EXPIRED
    assert events[0].payload['old_candidate_state'] == 'POSITION_OPEN'
    assert events[0].event_type.value == 'STATE_RECONCILED'


def test_reconcile_open_and_closed_portfolio_truth(tmp_path):
    portfolio, engine, position = opened(tmp_path)
    store = CandidateStateStore(tmp_path/'c.json')
    events = []
    reconcile = ReconciliationService(portfolio, store, events.append)
    assert reconcile.reconcile('ACME', now=NOW)
    assert store.get('ACME').episode_id == position.episode_id
    engine.close_at_price(position.trade_id, 98, 'STOP_HIT', now=NOW, exit_method='TEST')
    assert not reconcile.reconcile('ACME', now=NOW)
    assert store.get('ACME').state == CandidateState.CLOSED
    reconcile.reconcile('ACME', now=NOW)
    assert len(events) == 2


def test_duplicate_lifecycle_events_do_not_execute(tmp_path):
    portfolio, engine, position = opened(tmp_path)
    store = CandidateStateStore(tmp_path/'c.json')
    store.portfolio = portfolio
    ReconciliationService(portfolio, store, lambda _: None).reconcile('ACME', now=NOW)
    entry = PositionOpenedEvent(NOW, 'TEST', symbol='ACME', episode_id=position.episode_id)
    store.handle(entry)
    store.handle(entry)
    assert len(portfolio.snapshot().open_positions) == 1
    trade = engine.close_at_price(position.trade_id, 98, 'STOP_HIT', now=NOW, exit_method='TEST')
    cash = portfolio.snapshot().cash
    portfolio.close_position(position.trade_id, trade)
    exit_event = PositionClosedEvent(NOW, 'TEST', symbol='ACME', episode_id=position.episode_id)
    store.handle(exit_event)
    store.handle(exit_event)
    assert portfolio.snapshot().cash == cash
    assert len(portfolio.snapshot().closed_positions) == 1


def test_old_stopped_episode_cannot_reopen_but_new_cycle_can(tmp_path):
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1]*6))
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    position = portfolio.snapshot().open_positions[0]
    candidate.engine.close_at_price(position.trade_id, 98, 'STOP_HIT', now=NOW, exit_method='TEST')
    store.replace([context(slow=.9)], now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    assert not portfolio.has_symbol('ACME')
    new_context = replace(context(slow=.9), research_cycle_id='cycle-2', episode_id='')
    assert new_context.episode_id != position.episode_id
    store.replace([new_context], now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    assert portfolio.has_symbol('ACME')
    assert len(store.journal.read()) >= 2


def test_restart_restores_position_levels_and_episode_without_explicit_save(tmp_path):
    portfolio, _, position = opened(tmp_path)
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    restored = recovered.snapshot().open_positions[0]
    assert (restored.episode_id, restored.stop, restored.target) == (position.episode_id, position.stop, position.target)
    assert len(recovered.journal.replay()['positions']) == 1


def test_commit_failure_rolls_back_memory(tmp_path, monkeypatch):
    portfolio, engine, position = opened(tmp_path)
    before = portfolio.snapshot().to_dict()
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(portfolio, 'save', fail)
    with pytest.raises(OSError):
        engine.close_at_price(position.trade_id, 98, 'STOP_HIT', now=NOW, exit_method='TEST')
    assert portfolio.snapshot().to_dict() == before


def test_outbox_restart_recovers_missing_exit_event(tmp_path, monkeypatch):
    portfolio, engine, position = opened(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('journal unavailable')
    monkeypatch.setattr(portfolio.journal, 'append', fail)
    with pytest.raises(OSError):
        engine.close_at_price(position.trade_id, 98, 'STOP_HIT', now=NOW, exit_method='TEST')
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    assert not recovered.has_symbol('ACME')
    assert len(recovered.journal.replay()['closed_episodes']) == 1
    payload = next(r['payload'] for r in recovered.journal.read() if r['event'] == 'POSITION_CLOSED')
    assert {'episode_id', 'position_id', 'entry_time', 'exit_time', 'holding_seconds',
            'stop_at_exit', 'target_at_exit', 'realized_pnl', 'price_source', 'quote_timestamp'} <= payload.keys()


def test_journal_replay_is_idempotent_and_nonexecuting(tmp_path):
    journal = EventJournal(tmp_path/'events.jsonl')
    event = RuntimeEvent(RuntimeEventType.SHADOW_POSITION_OPENED, NOW.isoformat(), 'ACME', 'e1', payload={'stop': 99})
    assert journal.append(event)
    assert not journal.append(event)
    assert journal.replay() == journal.replay()
    assert len(journal.read()) == 1


def test_alpha_snapshots_are_symbol_specific_and_geometry_independent():
    a = context()
    b = replace(context(), symbol='OTHER', episode_id='')
    alpha = SetupController.alpha(a, NOW)
    other = SetupController.alpha(b, NOW)
    a.suggested_stop_reference = 1
    a.market_score = .01
    assert alpha.market_score == other.market_score == .7
    assert alpha.symbol != other.symbol
    assert alpha.episode_id != other.episode_id
    assert all(p.status == 'UNKNOWN' for p in alpha.provenance)
    with pytest.raises(FrozenInstanceError):
        alpha.slow_score = 0


@pytest.mark.parametrize('age,status', [(0, Quality.FRESH), (16, Quality.STALE), (-1, Quality.INVALID)])
def test_normalized_quote_quality(age, status):
    snapshot = MarketSnapshot.from_quote(quote(NOW-timedelta(seconds=age)), symbol='ACME', now=NOW, max_age=15)
    assert snapshot.quote_status == status


@pytest.mark.parametrize('missing', [True, False])
def test_quote_failure_is_temporary_and_monitoring_is_independent(tmp_path, missing):
    candidate, store, portfolio = watcher(tmp_path)
    candidate.process_quotes({} if missing else {'ACME': quote(NOW-timedelta(seconds=90))}, now=NOW)
    assert store.snapshot()[0].candidate_state == 'INFRASTRUCTURE_BLOCKED'
    assert not portfolio.has_symbol('ACME')


def test_slow_rejection_cannot_own_position(tmp_path):
    portfolio, _, position = opened(tmp_path)
    store = CandidateStateStore(tmp_path/'c.json')
    store.portfolio = portfolio
    ReconciliationService(portfolio, store, lambda _: None).reconcile('ACME', now=NOW)
    store.handle(RiskRejectedEvent(NOW, 'SLOW_ALPHA', symbol='ACME', payload={'reason': 'score fell'}))
    store.transition('ACME', CandidateState.REJECTED, timestamp=NOW, event_type='SLOW_ALPHA')
    assert store.get('ACME').state == CandidateState.POSITION_OPEN
    assert portfolio.has_symbol('ACME')


def test_end_to_end_shadow_never_invokes_real_executor(tmp_path, monkeypatch):
    from execution.robinhood_executor import RobinhoodExecutor
    assert config.MODE == 'SHADOW_TRADING'
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False
    def forbidden(*args, **kwargs):
        pytest.fail('real execution invoked')
    monkeypatch.setattr(RobinhoodExecutor, 'execute', forbidden)
    candidate, _, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1, 1]))
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    assert portfolio.has_symbol('ACME')


def test_legacy_loading_does_not_rewrite_historical_file(tmp_path):
    portfolio, _, position = opened(tmp_path)
    payload = json.loads(portfolio.state_path.read_text())
    for row in payload['open_positions']:
        for key in ('episode_id', 'research_cycle_id', 'entry_intent_id'):
            row.pop(key)
    portfolio.state_path.write_text(json.dumps(payload))
    original = portfolio.state_path.read_bytes()
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    assert recovered.snapshot().open_positions[0].episode_id == 'legacy:' + position.trade_id
    assert portfolio.state_path.read_bytes() == original


def test_restart_preserves_active_episode_and_slow_context(tmp_path):
    from agent.candidate_context import CandidateContextStore
    _, store, _ = watcher(tmp_path)
    recovered = CandidateContextStore(store.path)
    assert recovered.snapshot()[0].to_dict() == store.snapshot()[0].to_dict()


def test_stale_episode_event_cannot_reject_new_setup(tmp_path):
    store = CandidateStateStore(tmp_path/'s.json')
    store.discover('ACME', timestamp=NOW, event_type='TEST')
    store.update_facts('ACME', episode_id='new')
    store.handle(RiskRejectedEvent(NOW, 'TEST', symbol='ACME', episode_id='old'))
    assert store.get('ACME').state == CandidateState.DISCOVERED


def test_risk_geometry_and_portfolio_do_not_rewrite_alpha(tmp_path):
    from test_pre_execution_refresh import Refresher, risk, coordinator, fresh, NOW as REFRESH_NOW
    from execution.pre_execution import PreExecutionValidator
    from trading_runtime.portfolio_controller import PortfolioController
    from risk.risk_manager import RiskManager
    original = coordinator()
    before = dict(original)
    result = PreExecutionValidator().evaluate(original, Refresher(fresh()), risk(), now=REFRESH_NOW)
    assert result.approved
    assert original == before
    assert result.geometry_decision.entry == result.plan.entry_price
    assert result.risk_decision.episode_id == result.geometry_decision.episode_id
    portfolio = ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl')
    portfolio.state.cash = 0
    score = result.plan.coordinator_score
    decision, _ = PortfolioController(portfolio, RiskManager()).evaluate(result.plan, result.plan.entry_price)
    assert not decision.approved
    assert result.plan.coordinator_score == score


@pytest.mark.parametrize('price,offset,reason', [(98, 0, 'STOP_HIT'), (106, 0, 'TARGET_HIT'),
                                              (103, 1, 'MISSED_EOD_RECOVERY_EXIT')])
def test_position_controller_owns_exits(tmp_path, price, offset, reason):
    from test_fast_watcher import setup_watcher, Quotes, NOW as ENTRY
    from watcher.fast_watcher import PositionController
    at = ENTRY + timedelta(days=offset)
    controller, portfolio, *_ = setup_watcher(tmp_path, Quotes(price, at=at), clock=lambda: at)
    assert isinstance(controller, PositionController)
    controller.tick()
    assert portfolio.snapshot().closed_positions[0].exit_reason == reason


def test_candidate_watcher_cannot_close_position(tmp_path):
    candidate, store, portfolio = watcher(tmp_path, context(slow=.9), SequenceScorer([1, 1]))
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    candidate.process_quotes({'ACME': quote()}, now=NOW)
    store.replace([context(slow=.1)], now=NOW)
    candidate.process_quotes({}, now=NOW)
    assert portfolio.has_symbol('ACME')
    assert not portfolio.snapshot().closed_positions
