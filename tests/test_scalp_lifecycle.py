"""Deterministic restart and max-hold coverage; no external services."""

import json
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

import config
from agent.models import LlmReasoningResult, ReasoningTrace
from event_driven.reasoning import EventDrivenReasoningProvider
from shadow.portfolio import ShadowPortfolio
from strategies.scalp.analytics import scalp_session_summary, scalp_summary
from strategies.scalp.history import ScalpHistoryRefresher
from strategies.scalp.position import ScalpPositionController
from strategies.scalp.runtime import ScalpRuntime
from test_scalp_microbar_freshness import (
    BlockingHistoryClient, bars, data, history_call, lookup_factory,
)
from test_scalp_strategy import NOW, market_data, opened, quote


def _runtime(portfolio, root, now, *, history_refresher=None, universe=lambda: []):
    return ScalpRuntime(
        portfolio, lambda _: market_data(), universe=universe,
        setup_path=root/'setups.json', events_path=root/'events.jsonl',
        diagnostics_path=root/'diagnostics.jsonl', enabled=True,
        history_refresher=history_refresher, clock=lambda: now,
    )


def test_restart_reconciles_original_age_and_closes_once(tmp_path, monkeypatch):
    _entry, portfolio, original = opened(tmp_path, monkeypatch)
    restarted_at = NOW + timedelta(seconds=600)
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    runtime = _runtime(recovered, tmp_path, restarted_at)

    restored = recovered.snapshot().open_positions[0]
    assert restored.entry_timestamp == original.entry_timestamp == NOW.isoformat()
    assert restored.scalp_hold_seconds == 600
    assert restored.scalp_lifecycle_state == 'OVERDUE_SCALP_POSITION'
    assert restored.scalp_exit_status == 'OVERDUE_EXIT_PENDING'
    assert restored.scalp_overdue_by_seconds == 420
    assert runtime.startup_recovery['restored'] == 1
    assert runtime.startup_recovery['overdue'] == 1
    assert runtime.startup_recovery['exit_pending'] == 1

    result = runtime.on_quotes({'ACME': quote(restarted_at)}, now=restarted_at)
    assert len(result['exits']) == 1
    trade = result['exits'][0]
    assert trade.exit_reason == 'SCALP_RECOVERY_TIME_EXIT'
    assert trade.recovery_exit is True
    assert trade.holding_time_seconds == 600
    assert trade.crossed_max_hold_at == (NOW+timedelta(seconds=180)).isoformat()
    assert trade.max_hold_decision_delay_seconds == 420
    assert trade.max_hold_close_delay_seconds == 420
    assert trade.exit_price == round(
        100.23*(1-config.SCALP_EXIT_SLIPPAGE_BPS/10_000), 4,
    )
    assert runtime.on_quotes({'ACME': quote(restarted_at)}, now=restarted_at)['exits'] == []
    reloaded = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    assert not reloaded.snapshot().open_positions
    assert len(reloaded.snapshot().closed_positions) == 1
    assert len(reloaded.trades_path.read_text().splitlines()) == 1


def test_restart_without_quote_persists_pending_then_exits_on_fresh_bid(tmp_path, monkeypatch):
    _entry, portfolio, original = opened(tmp_path, monkeypatch)
    restarted_at = NOW + timedelta(seconds=600)
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    runtime = _runtime(recovered, tmp_path, restarted_at)

    first = runtime.on_quotes({}, now=restarted_at)
    assert first['exits'] == []
    pending = recovered.snapshot().open_positions[0]
    assert pending.entry_timestamp == original.entry_timestamp
    assert pending.scalp_exit_status == 'OVERDUE_EXIT_PENDING'
    assert pending.scalp_exit_reason == 'QUOTE_UNAVAILABLE'
    assert first['entries'] == []

    # A second restart while the exit is still pending preserves the same
    # entry clock and remains idempotent.
    second_restart = restarted_at + timedelta(seconds=1)
    recovered_again = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    runtime = _runtime(recovered_again, tmp_path, second_restart)
    pending = recovered_again.snapshot().open_positions[0]
    assert pending.entry_timestamp == original.entry_timestamp
    assert pending.scalp_hold_seconds == 601

    stale_at = restarted_at + timedelta(seconds=2)
    runtime.on_quotes(
        {'ACME': quote(stale_at-timedelta(seconds=3))}, now=stale_at,
    )
    pending = recovered_again.snapshot().open_positions[0]
    assert pending.scalp_exit_reason == 'STALE_QUOTE'
    assert pending.scalp_hold_seconds == 602

    fresh_at = restarted_at + timedelta(seconds=4)
    result = runtime.on_quotes({'ACME': quote(fresh_at)}, now=fresh_at)
    assert result['exits'][0].exit_reason == 'SCALP_RECOVERY_TIME_EXIT'
    assert {'PROCESS_RESTART', 'QUOTE_UNAVAILABLE', 'STALE_QUOTE'} <= set(
        result['exits'][0].max_hold_delay_reasons
    )
    assert len(ShadowPortfolio(
        portfolio.state_path, portfolio.trades_path,
    ).snapshot().closed_positions) == 1
    rows = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert sum(row['event'] == 'ScalpPositionClosed' for row in rows) == 1


def test_overdue_pending_exit_blocks_new_scalp_admission(tmp_path, monkeypatch):
    _entry, portfolio, _original = opened(tmp_path, monkeypatch)
    restarted_at = NOW + timedelta(seconds=600)
    recovered = ShadowPortfolio(portfolio.state_path, portfolio.trades_path)
    runtime = _runtime(
        recovered, tmp_path, restarted_at, universe=lambda: ['NEW'],
    )
    new_quote = replace(quote(restarted_at), symbol='NEW')
    result = runtime.on_quotes({'NEW': new_quote}, now=restarted_at)
    assert result['exits'] == []
    assert result['entries'][0]['reason'] == 'OVERDUE_EXIT_PENDING'
    assert recovered.has_symbol('ACME')
    assert not recovered.has_symbol('NEW')


def test_normal_max_hold_uses_wall_clock_at_179_and_180_seconds(tmp_path, monkeypatch):
    entry, portfolio, _position = opened(tmp_path, monkeypatch)
    manager = ScalpPositionController(
        portfolio, setup_controller=entry.setup_controller,
        events=entry.events, engine=entry.engine,
    )
    at_179 = NOW + timedelta(seconds=179)
    assert manager.process_quotes({'ACME': quote(at_179)}, now=at_179) == []
    assert portfolio.snapshot().open_positions[0].scalp_time_remaining_seconds == 1

    at_180 = NOW + timedelta(seconds=180)
    trade = manager.process_quotes({'ACME': quote(at_180)}, now=at_180)[0]
    assert trade.exit_reason == 'SCALP_TIME_EXIT'
    assert trade.recovery_exit is False
    assert trade.holding_time_seconds == 180
    assert trade.configured_max_hold_seconds == 180
    assert trade.max_hold_close_delay_seconds == 0
    assert trade.exit_price == round(
        100.23*(1-config.SCALP_EXIT_SLIPPAGE_BPS/10_000), 4,
    )


def test_time_exit_does_not_read_signal_pipeline(tmp_path, monkeypatch):
    entry, portfolio, _position = opened(tmp_path, monkeypatch)
    manager = ScalpPositionController(
        portfolio, setup_controller=entry.setup_controller,
        events=entry.events, engine=entry.engine,
    )

    def blocked(_symbol):
        raise AssertionError('signal/history path must not own mandatory exits')

    at_180 = NOW + timedelta(seconds=180)
    closed = manager.process_quotes(
        {'ACME': quote(at_180)}, blocked, now=at_180,
    )
    assert closed[0].exit_reason == 'SCALP_TIME_EXIT'


def test_slow_llm_block_does_not_delay_scalp_max_hold(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    class SlowReasoning:
        def reason(self, payload, *, expected_symbols, now):
            entered.set()
            assert release.wait(2)
            return LlmReasoningResult(
                status='UNAVAILABLE', candidates=(),
                trace=ReasoningTrace(
                    'FAKE', 'gpt-5.6-sol', now.isoformat(), 1, 1,
                    '1', '1', 'FAILED', 'TEST_DONE',
                ),
                failure_reason='TEST_DONE',
            )

    slow = EventDrivenReasoningProvider(SlowReasoning())
    thread = Thread(target=lambda: slow.reason(
        {'broad_market_context': {}, 'candidates': [{'symbol': 'NEW'}]},
        expected_symbols=['NEW'], now=NOW,
    ))
    thread.start()
    assert entered.wait(1)
    try:
        entry, portfolio, _position = opened(tmp_path, monkeypatch)
        manager = ScalpPositionController(
            portfolio, setup_controller=entry.setup_controller,
            events=entry.events, engine=entry.engine,
        )
        at_180 = NOW + timedelta(seconds=180)
        closed = manager.process_quotes({'ACME': quote(at_180)}, now=at_180)
        assert closed[0].exit_reason == 'SCALP_TIME_EXIT'
        assert not release.is_set()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_background_history_refresh_cannot_block_max_hold_exit(tmp_path, monkeypatch):
    entry, portfolio, _position = opened(tmp_path, monkeypatch)
    old = bars(latest_begin=NOW - timedelta(minutes=10))
    new = bars(latest_begin=NOW - timedelta(minutes=5))
    response = {symbol: history_call(new) for symbol in ('ACME', 'SPY', 'QQQ')}
    client = BlockingHistoryClient(response)
    seed = {
        symbol: data(
            old, vwap=99.0, ema9=99.0,
            very_short_momentum=0.0, volume_acceleration=1.0,
        ) for symbol in response
    }
    now_ref = [NOW + timedelta(seconds=179)]
    refresher = ScalpHistoryRefresher(
        client, lookup_factory(seed), clock=lambda: now_ref[0],
    )
    runtime = ScalpRuntime(
        portfolio, lambda symbol: seed.get(symbol, {}), universe=lambda: ['ACME'],
        setup_path=tmp_path/'setups.json', events_path=tmp_path/'events.jsonl',
        diagnostics_path=tmp_path/'diagnostics.jsonl', enabled=True,
        history_refresher=refresher, clock=lambda: NOW,
    )
    at_179 = NOW + timedelta(seconds=179)
    runtime.on_quotes({'ACME': quote(at_179)}, now=at_179)
    assert client.started.wait(1)
    before = refresher.get_many(['ACME'])['ACME']['candles'][-1]['begins_at']

    at_180 = NOW + timedelta(seconds=180)
    now_ref[0] = at_180
    result = runtime.on_quotes({'ACME': quote(at_180)}, now=at_180)
    assert result['exits'][0].exit_reason == 'SCALP_TIME_EXIT'
    assert not client.release.is_set()
    during = refresher.get_many(['ACME'])['ACME']['candles'][-1]['begins_at']
    assert during == before
    client.release.set()
    refresher.close()
    after = refresher.get_many(['ACME'])['ACME']['candles'][-1]['begins_at']
    assert after != before


def test_mstr_style_ema9_entry_path_is_unchanged(tmp_path, monkeypatch):
    # Use a clean controller and preserve the successful 0.040%-spread,
    # positive-edge geometry while ensuring EMA9_CONTINUATION classification.
    clean_root = tmp_path/'mstr'
    from strategies.scalp.execution import ScalpEntryController
    portfolio = ShadowPortfolio(clean_root/'portfolio.json', clean_root/'trades.jsonl')
    controller = ScalpEntryController(
        portfolio, setup_path=clean_root/'setups.json',
        events_path=clean_root/'events.jsonl', enabled=True,
    )
    market = market_data()
    market['candles'][-2]['high'] = 100.70  # EMA continuation resistance/target
    mstr_quote = replace(
        quote(bid=100.23, ask=100.27, last=100.25), symbol='MSTR',
    )
    position, detail = controller.process('MSTR', mstr_quote, market, now=NOW)
    assert position is not None, detail
    assert detail['decision'].setup_type == 'EMA9_CONTINUATION'
    assert detail['decision'].features.spread_pct < .0005
    assert detail['decision'].expected_net_edge_pct > 0
    assert detail['decision'].risk_reward_ratio >= config.SCALP_MIN_RISK_REWARD
    assert detail['risk_approved'] and detail['pre_execution_passed']


def test_lifecycle_analytics_distinguish_recovery_and_quote_delay(tmp_path):
    trade = {
        'strategy_id': 'SCALP', 'symbol': 'HALO', 'episode_id': 'episode',
        'exit_timestamp': NOW.isoformat(), 'holding_time_seconds': 600,
        'holding_time_minutes': 10, 'configured_max_hold_seconds': 180,
        'exit_reason': 'SCALP_RECOVERY_TIME_EXIT', 'recovery_exit': True,
        'crossed_max_hold_at': (NOW-timedelta(seconds=420)).isoformat(),
        'exit_decision_at': NOW.isoformat(), 'max_hold_decision_delay_seconds': 420,
        'max_hold_close_delay_seconds': 420,
        'max_hold_delay_reasons': ['PROCESS_RESTART', 'QUOTE_UNAVAILABLE'],
        'net_pnl': 1, 'gross_pnl': 1, 'return_percent': .1,
    }
    performance = scalp_summary([trade])
    assert performance['normal_time_exits'] == 0
    assert performance['recovery_time_exits'] == 1
    assert performance['p90_holding_seconds'] == 600
    assert performance['max_overdue_seconds'] == 420
    assert performance['positions_exceeding_max_quote_unavailable'] == 1

    cycle = {
        'event_id': 'c', 'record_type': 'DISCOVERY_CYCLE',
        'strategy_id': 'SCALP', 'timestamp': NOW.isoformat(),
        'payload': {
            'funnel': {}, 'rejection_reasons': {}, 'filtered_reasons': {},
            'entry_blocked_reasons': {}, 'pre_execution_failure_reasons': {},
            'risk_rejection_reasons': {}, 'portfolio_block_reasons': {},
            'setup_types': {}, 'position_lifecycle': [{
                'episode_id': 'episode', 'lifecycle_state': 'OVERDUE_SCALP_POSITION',
                'exit_status': 'OVERDUE_EXIT_PENDING', 'exit_reason': 'QUOTE_UNAVAILABLE',
                'overdue_by_seconds': 420,
            }],
        },
    }
    path = tmp_path/'diagnostics.jsonl'
    path.write_text(json.dumps(cycle)+'\n')
    session = scalp_session_summary(path, [trade], now=NOW)
    assert session['overdue_positions_seen'] == 1
    assert session['recovery_time_exits'] == 1
    assert session['max_hold_sla'][0]['close_delay_seconds'] == 420


def test_safety_and_threshold_constants_remain_unchanged():
    assert config.MODE == 'SHADOW_TRADING'
    assert config.SCALP_MODE == 'SHADOW'
    assert config.SCALP_ENABLED is False
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False
    assert config.SCALP_MAX_QUOTE_AGE_SECONDS == 2
    assert config.SCALP_MAX_SPREAD_PCT == .001
    assert config.SCALP_MIN_RELATIVE_VOLUME == 1.20
    assert config.SCALP_MIN_SIGNAL_SCORE == .70
    assert config.SCALP_MIN_EXPECTED_NET_EDGE == .0005
    assert config.SCALP_MIN_RISK_REWARD == 1.10
    assert config.SCALP_MAX_HOLD_SECONDS == 180
