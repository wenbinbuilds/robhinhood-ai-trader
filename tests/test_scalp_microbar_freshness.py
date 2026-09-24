from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic
from types import SimpleNamespace

import config
from strategies.scalp.freshness import completed_micro_bars, micro_bar_freshness
from strategies.scalp.history import ScalpHistoryRefresher
from strategies.scalp.signals import ScalpSignalEngine
from strategies.scalp.diagnostics import candidate_trace, cycle_diagnostics
from watcher.models import FastQuote


UTC = timezone.utc
BOUNDARY = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)


def bars(*, latest_begin=BOUNDARY - timedelta(minutes=5), count=30,
         interval=300, volume=1000):
    first = latest_begin - timedelta(seconds=interval * (count - 1))
    result = []
    for index in range(count):
        began = first + timedelta(seconds=interval * index)
        close = 99 + index * .05
        result.append({
            'begins_at': began.isoformat(), 'interval_seconds': interval,
            'open': close - .03, 'high': close + .04, 'low': close - .06,
            'close': close,
            'volume': volume if index < count - 1 else volume * 2,
            'bar_source': 'TEST_PROVIDER', 'interpolated': False,
        })
    return result


def raw_history(rows):
    return [{
        'begins_at': row['begins_at'], 'open_price': row['open'],
        'high_price': row['high'], 'low_price': row['low'],
        'close_price': row['close'], 'volume': row['volume'],
    } for row in rows]


def data(rows, **overrides):
    closes = [row['close'] for row in rows]
    value = {
        'candles': rows, 'vwap': closes[-1] - .05,
        'ema9': closes[-1] - .02, 'ema20': closes[-1] - .10,
        'relative_volume': 2.0,
        'relative_volume_source': 'COMPLETED_MICRO_BAR_RATIO',
        'spy_return_3bar': 0.0, 'qqq_return_3bar': 0.0,
        'rsi14': 60, 'macd': .2, 'macd_signal': .1,
    }
    value.update(overrides)
    return value


def quote(now, price=100.5):
    return FastQuote('ACME', price - .02, price + .02, price, now, 'TEST_FAST', True)


def test_completed_five_minute_bar_remains_valid_through_current_window():
    rows = bars()
    at_1501 = micro_bar_freshness(rows, now=BOUNDARY + timedelta(minutes=1))
    at_150459 = micro_bar_freshness(
        rows, now=BOUNDARY + timedelta(minutes=4, seconds=59))
    assert at_1501.freshness_status == 'FRESH'
    assert at_150459.freshness_status == 'FRESH'
    assert at_150459.latest_completed_bar_timestamp == BOUNDARY.isoformat()
    assert at_150459.bar_timeframe_seconds == 300


def test_boundary_aging_and_stale_are_timeframe_aware():
    rows = bars()
    aging = micro_bar_freshness(
        rows, now=BOUNDARY + timedelta(minutes=5, seconds=3))
    stale = micro_bar_freshness(
        rows, now=BOUNDARY + timedelta(minutes=7, seconds=1))
    assert aging.freshness_status == 'AGING'
    assert stale.freshness_status == 'STALE'
    assert stale.freshness_reason == 'NEW_COMPLETED_BAR_MISSING_BEYOND_ALLOWED_LAG'


def test_future_and_incomplete_bars_never_enter_features():
    now = BOUNDARY + timedelta(minutes=1)
    rows = bars() + [{
        'begins_at': now.isoformat(), 'interval_seconds': 300,
        'open': 1, 'high': 999, 'low': 1, 'close': 999, 'volume': 999999,
    }]
    completed = completed_micro_bars(rows, now)
    features = ScalpSignalEngine().features('ACME', quote(now), data(rows), now=now)
    assert len(completed) == 30
    assert features.recent_high < 200


class HistoryClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_historicals_many(self, symbols):
        self.calls.append(tuple(symbols))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return [response.get(symbol, RuntimeError('missing')) for symbol in symbols]


def history_call(rows):
    return SimpleNamespace(value=raw_history(rows), duration_seconds=.01)


def lookup_factory(seed):
    return lambda symbol: seed.get(symbol, {})


def test_refresher_fires_once_at_boundary_and_advances_cache():
    old = bars()
    new = bars(latest_begin=BOUNDARY)
    client = HistoryClient([{
        symbol: history_call(new) for symbol in ('ACME', 'SPY', 'QQQ')
    }])
    completed_at = BOUNDARY + timedelta(minutes=5, seconds=3)
    seed = {symbol: data(old) for symbol in ('ACME', 'SPY', 'QQQ')}
    refresher = ScalpHistoryRefresher(
        client, lookup_factory(seed), clock=lambda: completed_at)
    result = refresher.refresh(['ACME'], now=completed_at)
    repeated = refresher.refresh(['ACME'], now=completed_at + timedelta(seconds=2))
    assert result['attempt_count'] == 3 and result['success_count'] == 3
    assert repeated['attempt_count'] == 0
    assert len(client.calls) == 1
    assert micro_bar_freshness(
        refresher.get('ACME')['candles'], now=completed_at
    ).freshness_status == 'FRESH'


def test_stale_cache_recovers_and_refresh_is_independent_of_momentum_or_llm():
    old = bars(latest_begin=BOUNDARY - timedelta(minutes=10))
    new = bars(latest_begin=BOUNDARY)
    client = HistoryClient([{
        symbol: history_call(new) for symbol in ('ACME', 'SPY', 'QQQ')
    }])
    now = BOUNDARY + timedelta(minutes=5, seconds=3)
    seed = {symbol: data(old) for symbol in ('ACME', 'SPY', 'QQQ')}
    refresher = ScalpHistoryRefresher(client, lookup_factory(seed), clock=lambda: now)
    assert refresher.refresh(['ACME'], now=now)['success_count'] == 3
    assert refresher.get('ACME')['micro_bar_provider_status'] == 'OK'
    assert set(client.calls[0]) == {'ACME', 'SPY', 'QQQ'}


def test_failed_refresh_retains_bar_and_reports_provider_error():
    old = bars()
    client = HistoryClient([RuntimeError('down')])
    now = BOUNDARY + timedelta(minutes=5, seconds=3)
    seed = {symbol: data(old) for symbol in ('ACME', 'SPY', 'QQQ')}
    refresher = ScalpHistoryRefresher(client, lookup_factory(seed), clock=lambda: now)
    result = refresher.refresh(['ACME'], now=now)
    assert result['failure_count'] == 3
    cached = refresher.get('ACME')
    assert cached['candles'] == old
    assert cached['micro_bar_provider_status'] == 'ERROR'
    decision = ScalpSignalEngine().evaluate(
        'e', 'ACME', quote(now), cached, now=now)
    assert 'MICRO_BAR_REFRESH_FAILED' in decision.rejection_reasons
    assert 'VOLUME_DATA_UNAVAILABLE' in decision.rejection_reasons
    assert 'SIGNAL_SCORE_BELOW_THRESHOLD' not in decision.rejection_reasons


def test_bar_sequence_advances_only_after_boundary_refresh():
    old = bars(count=8)
    newest = bars(latest_begin=BOUNDARY, count=9)
    now = BOUNDARY + timedelta(minutes=5, seconds=3)
    client = HistoryClient([{
        symbol: history_call(newest) for symbol in ('ACME', 'SPY', 'QQQ')
    }])
    seed = {symbol: data(old) for symbol in ('ACME', 'SPY', 'QQQ')}
    refresher = ScalpHistoryRefresher(client, lookup_factory(seed), clock=lambda: now)
    assert len(refresher.get('ACME')['candles']) == 8
    assert len(refresher.get('ACME')['candles']) == 8
    refresher.refresh(['ACME'], now=now)
    assert len(refresher.get('ACME')['candles']) == 9


def test_liquidity_distinguishes_stale_data_from_true_insufficiency():
    fresh_now = BOUNDARY + timedelta(minutes=1)
    stale_now = BOUNDARY + timedelta(minutes=8)
    low = data(bars(), relative_volume=.8)
    weak = ScalpSignalEngine().evaluate('e', 'ACME', quote(fresh_now), low, now=fresh_now)
    stale = ScalpSignalEngine().evaluate('e', 'ACME', quote(stale_now), low, now=stale_now)
    assert 'VOLUME_EXPANSION_BELOW_MINIMUM' in weak.rejection_reasons
    assert 'VOLUME_DATA_STALE' not in weak.rejection_reasons
    assert 'VOLUME_DATA_STALE' in stale.rejection_reasons
    assert 'VOLUME_EXPANSION_BELOW_MINIMUM' not in stale.rejection_reasons


def test_stale_features_are_not_scored_as_below_threshold():
    now = BOUNDARY + timedelta(minutes=8)
    decision = ScalpSignalEngine().evaluate(
        'e', 'ACME', quote(now), data(bars()), now=now)
    assert decision.signal_score is None
    assert 'MICRO_BARS_STALE' in decision.rejection_reasons
    assert 'SIGNAL_SCORE_BELOW_THRESHOLD' not in decision.rejection_reasons
    assert decision.features.feature_provenance['return_3']['status'] == 'STALE'


def test_valid_features_produce_score_and_provenance():
    now = BOUNDARY + timedelta(minutes=1)
    decision = ScalpSignalEngine().evaluate(
        'e', 'ACME', quote(now), data(bars()), now=now)
    assert decision.signal_score is not None
    assert 'SIGNAL_DATA_STALE' not in decision.rejection_reasons
    assert decision.features.signal_data_status == 'VALID'
    assert set(decision.features.feature_provenance) >= {
        'return_3', 'price_vs_vwap', 'ema9_slope', 'relative_strength_spy',
        'breakout_volume_expansion', 'spread_pct', 'relative_volume',
    }


def test_refresher_exposes_no_broker_order_operation():
    client = HistoryClient([])
    refresher = ScalpHistoryRefresher(client, lambda _: {})
    assert not hasattr(refresher, 'place_order')
    assert not hasattr(client, 'place_order')
    assert config.MODE == 'SHADOW_TRADING' and config.SCALP_MODE == 'SHADOW'


def test_diagnostic_sequence_stays_eight_then_advances_to_nine():
    old = bars()
    new = bars(latest_begin=BOUNDARY)

    def cycle(count, rows, now):
        traces = []
        for index in range(count):
            symbol = f'T{index}'
            traces.append(candidate_trace(
                cycle_id=now.isoformat(), symbol=symbol,
                quality={'provider_status': 'OK', 'quote_status': 'FRESH',
                         'quote_age_seconds': .5, 'spread_pct': .0004},
                market_data=data(rows),
                detail={'symbol': symbol, 'reason': 'UNCLASSIFIED_SETUP',
                        'entry_attempted': False}, now=now,
            ))
        return cycle_diagnostics(
            cycle_id=now.isoformat(), traces=traces, source='TEST', now=now,
        )['funnel']['micro_bars_pass']

    assert cycle(8, old, BOUNDARY + timedelta(minutes=1)) == 8
    assert cycle(8, old, BOUNDARY + timedelta(minutes=4, seconds=59)) == 8
    assert cycle(9, new, BOUNDARY + timedelta(minutes=5, seconds=3)) == 9


def test_position_monitoring_runs_before_stale_data_blocks_new_entries(tmp_path):
    from shadow.portfolio import ShadowPortfolio
    from strategies.scalp.runtime import ScalpRuntime

    class PositionProbe:
        called = False

        def process_quotes(self, quotes, lookup, *, now):
            self.called = True
            return ['MONITORED']

    class EntryProbe:
        def on_quotes(self, quotes, lookup, *, now):
            return [{'symbol': 'ACME', 'reason': 'MICRO_BARS_STALE',
                     'reasons': ['MICRO_BARS_STALE'], 'entry_attempted': False}]

    runtime = ScalpRuntime(
        ShadowPortfolio(tmp_path/'p.json', tmp_path/'t.jsonl'),
        lambda _: data(bars()), universe=lambda: ['ACME'],
        setup_path=tmp_path/'s.json', events_path=tmp_path/'e.jsonl',
        diagnostics_path=tmp_path/'d.jsonl', enabled=True,
    )
    runtime.positions = PositionProbe()
    runtime.entry = EntryProbe()
    result = runtime.on_quotes(
        {'ACME': quote(BOUNDARY + timedelta(minutes=8))},
        now=BOUNDARY + timedelta(minutes=8),
    )
    assert runtime.positions.called is True
    assert result['exits'] == ['MONITORED']
    assert result['diagnostics']['funnel']['micro_bars_pass'] == 0


def test_liquidity_formula_and_persisted_units_are_explicit():
    now = BOUNDARY + timedelta(minutes=1)
    rows = bars(volume=1000)
    decision = ScalpSignalEngine().evaluate(
        'e', 'ACME', quote(now), data(rows), now=now)
    evidence = decision.features.feature_provenance['relative_volume']
    assert evidence['current_volume'] == 2000
    assert evidence['baseline_volume'] == 1000
    assert evidence['baseline_bar_count'] == 5
    assert evidence['bar_timeframe_seconds'] == 300
    assert evidence['completed_bars_only'] is True
    assert evidence['same_time_of_day_normalized'] is False
    assert evidence['formula'].startswith('LATEST_COMPLETED_BAR_VOLUME/')
    item = candidate_trace(
        cycle_id='c', symbol='ACME',
        quality={'provider_status': 'OK', 'quote_status': 'FRESH',
                 'quote_age_seconds': 0, 'spread_pct': .0004},
        market_data=data(rows),
        detail={'symbol': 'ACME', 'decision': decision,
                'reasons': list(decision.rejection_reasons)}, now=now,
    )
    assert item['liquidity']['current_volume'] == 2000
    assert item['liquidity']['baseline_volume'] == 1000


def test_mixed_timeframe_bars_are_not_combined():
    rows = bars(count=6)
    rows.insert(-1, {
        **rows[-2], 'begins_at': (BOUNDARY-timedelta(minutes=6)).isoformat(),
        'interval_seconds': 60, 'volume': 999999,
    })
    completed = completed_micro_bars(rows, BOUNDARY + timedelta(minutes=1))
    assert completed
    assert {row['interval_seconds'] for row in completed} == {300}
    assert all(row['volume'] != 999999 for row in completed)


def test_score_contributions_sum_clamp_and_missing_component_are_visible():
    now = BOUNDARY + timedelta(minutes=1)
    engine = ScalpSignalEngine()
    decision = engine.evaluate('e', 'ACME', quote(now), data(bars()), now=now)
    components = decision.features.score_breakdown
    assert abs(sum(row['contribution'] for row in components.values())
               - decision.signal_score) < 1e-6
    assert components['volume_expansion']['clamped_max'] is True
    missing = engine.evaluate(
        'e', 'ACME', quote(now), data(bars(), spy_return_3bar=None), now=now)
    assert missing.signal_score is None
    assert missing.features.score_breakdown['relative_strength']['missing'] is True
    assert config.SCALP_MIN_SIGNAL_SCORE == .70


def test_offline_session_diagnostics_do_not_mutate_strategy_state(tmp_path):
    from strategies.scalp.analytics import scalp_session_summary
    path = tmp_path/'diagnostics.jsonl'
    before = path.exists()
    summary = scalp_session_summary(path, [], now=BOUNDARY)
    assert path.exists() is before
    assert summary['signal_score_distribution']['count'] == 0


class BlockingHistoryClient(HistoryClient):
    def __init__(self, response):
        super().__init__([response])
        self.started = Event()
        self.release = Event()

    def get_historicals_many(self, symbols):
        self.started.set()
        assert self.release.wait(2)
        return super().get_historicals_many(symbols)


def test_background_refresh_is_nonblocking_and_cache_publication_is_atomic():
    old = bars()
    new = bars(latest_begin=BOUNDARY)
    response = {symbol: history_call(new) for symbol in ('ACME', 'BETA', 'SPY', 'QQQ')}
    client = BlockingHistoryClient(response)
    now = BOUNDARY + timedelta(minutes=5, seconds=3)
    seed = {symbol: data(old) for symbol in response}
    refresher = ScalpHistoryRefresher(client, lookup_factory(seed), clock=lambda: now)
    started = monotonic()
    scheduled = refresher.poll(['ACME', 'BETA'], now=now,
                               quote_timestamps={'ACME': now-timedelta(seconds=.5)})
    elapsed = monotonic()-started
    assert client.started.wait(1)
    assert elapsed < .2 and scheduled['refresh_in_progress'] is True
    during = refresher.get_many(['ACME', 'BETA'])
    assert all(row['candles'][-1]['begins_at'] == old[-1]['begins_at']
               for row in during.values())
    client.release.set()
    refresher.close()
    after = refresher.get_many(['ACME', 'BETA'])
    assert all(row['candles'][-1]['begins_at'].startswith(BOUNDARY.isoformat()[:19])
               for row in after.values())
    completed = refresher.poll(['ACME', 'BETA'], now=now)
    assert completed['refresh_completed'] is True
    assert len(client.calls) == 1


def test_refresh_timing_records_quote_age_crossing_without_mutating_quote():
    old = bars(); new = bars(latest_begin=BOUNDARY)
    start = BOUNDARY + timedelta(minutes=5, seconds=3)
    end = start + timedelta(seconds=3)
    response = {symbol: history_call(new) for symbol in ('ACME', 'SPY', 'QQQ')}
    client = HistoryClient([response])
    seed = {symbol: data(old) for symbol in response}
    refresher = ScalpHistoryRefresher(client, lookup_factory(seed), clock=lambda: end)
    quote_timestamp = start-timedelta(seconds=.5)
    result = refresher._refresh_due(
        refresher._claim_due(['ACME'], start), start,
        quote_timestamps={'ACME': quote_timestamp},
    )
    assert result['timing']['quote_age_immediately_before']['ACME'] == .5
    assert result['timing']['quote_age_immediately_after']['ACME'] == 3.5
    assert result['timing']['quotes_crossing_max_age_during_refresh'] == 1
    assert quote_timestamp == start-timedelta(seconds=.5)
