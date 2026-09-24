"""Wires entry and position controllers without any LLM dependency."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from collections import defaultdict

import config
from strategies.scalp.diagnostics import (
    ScalpDiagnosticsJournal, candidate_trace, cycle_diagnostics,
)
from strategies.scalp.execution import ScalpEntryController
from strategies.scalp.position import ScalpPositionController


class ScalpRuntime:
    def __init__(self, portfolio, data_lookup, *, universe=None,
                 setup_path=config.SCALP_STATE_PATH,
                 events_path=config.SCALP_EVENT_LOG_PATH, enabled=None,
                 discovery_source=None, kill_switch_path=None,
                 diagnostics_path=None, debug=False, history_refresher=None,
                 clock=None):
        self.data_lookup = data_lookup
        self.history_refresher = history_refresher
        self.universe = universe or (lambda: [])
        self.discovery_source = discovery_source or config.SCALP_DISCOVERY_SOURCE
        diagnostic_file = diagnostics_path or Path(events_path).with_name('scalp_diagnostics.jsonl')
        self.diagnostics = ScalpDiagnosticsJournal(diagnostic_file)
        self.debug = bool(debug)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Strategy-owned price tape. It contains only real provider quotes; it
        # never manufactures OHLCV or volume.
        self._quote_tape = defaultdict(list)
        self.entry = ScalpEntryController(
            portfolio, setup_path=setup_path, events_path=events_path,
            enabled=enabled, kill_switch_path=kill_switch_path,
        )
        self.positions = ScalpPositionController(
            portfolio, setup_controller=self.entry.setup_controller,
            events=self.entry.events, engine=self.entry.engine)
        # Durable reconciliation happens during construction, before the fast
        # watcher can perform any discovery or new-entry evaluation.
        self.startup_recovery = self.positions.reconcile_startup(now=self.clock())

    def on_quotes(self, quotes, *, now, entry_quotes=None, quote_quality=None,
                  loop_timing=None, position_quotes=None):
        observed = quotes if entry_quotes is None else entry_quotes
        self._observe_quotes(observed, now)
        # Position exits run first. A just-closed episode is permanently closed;
        # entry then needs a genuinely different evidence key.
        exits = self.positions.process_quotes(
            quotes if position_quotes is None else position_quotes,
            self._data, now=now,
        )
        lifecycle = list(getattr(self.positions, 'last_lifecycle', []))
        universe = self.symbols(now)
        refresh = None
        effective_now = now
        if self.history_refresher is not None:
            refresh = self.history_refresher.poll(
                universe, now=now,
                quote_timestamps={
                    symbol: quote.timestamp for symbol, quote in observed.items()
                    if quote is not None
                },
            )
            data_by_symbol = self.history_refresher.get_many(universe)
            cycle_lookup = lambda symbol: self._with_quote_microstructure(
                symbol, data_by_symbol.get(symbol.upper(), {}), now
            )
        else:
            cycle_lookup = lambda symbol: self._with_quote_microstructure(
                symbol, self._data(symbol), now
            )
        # The history batch runs independently. This cycle sees one atomic old
        # or new cache generation and always evaluates the just-retrieved quote.
        overdue_pending = any(
            row.get('exit_status') == 'OVERDUE_EXIT_PENDING'
            for row in lifecycle
        )
        if overdue_pending:
            # A mandatory unresolved exit owns the cycle. Discovery remains
            # observable, but no new scalp risk is admitted until it resolves.
            entries = [{
                'symbol': symbol, 'reason': 'OVERDUE_EXIT_PENDING',
                'reasons': ['OVERDUE_EXIT_PENDING'], 'entry_attempted': False,
            } for symbol in universe]
        else:
            entries = self.entry.on_quotes(observed, cycle_lookup, now=effective_now)
        qualities = quote_quality or {}
        detail_by_symbol = {detail.get('symbol'): detail for detail in entries}
        cycle_id = 'SCALP-CYCLE-' + uuid4().hex
        traces = []
        for symbol in universe:
            quality = dict(qualities.get(symbol, {}))
            quote = observed.get(symbol) or quotes.get(symbol)
            if quote is not None:
                age = quote.age_at(effective_now)
                bid, ask = quote.bid, quote.ask
                spread = ((ask-bid)/((ask+bid)/2)
                          if bid is not None and ask is not None and ask >= bid and ask+bid > 0
                          else None)
                quality.update({
                    'provider_status': quality.get('provider_status', 'OK'),
                    'quote_status': ('FRESH' if 0 <= age <= config.SCALP_MAX_QUOTE_AGE_SECONDS
                                     else 'STALE'),
                    'quote_age_seconds': age, 'spread_pct': spread,
                })
            if not quality:
                quality = {'provider_status': 'UNAVAILABLE',
                           'quote_status': 'UNAVAILABLE',
                           'quote_age_seconds': None, 'spread_pct': None}
            data = cycle_lookup(symbol)
            detail = detail_by_symbol.get(symbol, {
                'symbol': symbol,
                'reason': ('QUOTE_UNAVAILABLE' if quality['provider_status'] != 'OK'
                           else 'STALE_QUOTE' if quality['quote_status'] != 'FRESH'
                           else 'NOT_EVALUATED'),
                'entry_attempted': False,
            })
            traces.append(candidate_trace(
                cycle_id=cycle_id, symbol=symbol, quality=quality,
                market_data=data, detail=detail, now=effective_now,
            ))
        diagnostics = cycle_diagnostics(
            cycle_id=cycle_id, traces=traces, source=self.discovery_source,
            now=effective_now, exits=len(exits), refresh=refresh,
            loop_timing=loop_timing, position_lifecycle=lifecycle,
        )
        self.diagnostics.record(diagnostics, traces)
        if self.debug:
            for trace in traces:
                if trace['stage_flags'].get('eligible_micro_signals'):
                    self._print_eligible_candidate(trace)
                if 'STALE_SCALP_EPISODE' in trace.get('rejection_reasons', []):
                    self._print_stale_episode(trace)
            for row in lifecycle:
                self._print_position_lifecycle(row)
            for trace in traces:
                self._print_trace(trace)
        return {'exits': exits, 'entries': entries, 'diagnostics': diagnostics,
                'traces': traces, 'position_lifecycle': lifecycle}

    def _observe_quotes(self, quotes, now):
        cutoff = now.astimezone(timezone.utc).timestamp() - config.SCALP_MAX_HOLD_SECONDS
        for symbol, quote in quotes.items():
            if quote is None or quote.timestamp.tzinfo is None:
                continue
            stamp = quote.timestamp.astimezone(timezone.utc).timestamp()
            mark = quote.mark_price
            if mark is None or mark <= 0:
                continue
            tape = self._quote_tape[symbol.upper()]
            if not tape or stamp > tape[-1][0]:
                tape.append((stamp, float(mark), quote.bid, quote.ask))
            self._quote_tape[symbol.upper()] = [row for row in tape if row[0] >= cutoff][-180:]

    def _with_quote_microstructure(self, symbol, data, now):
        result = dict(data or {})
        tape = list(self._quote_tape.get(symbol.upper(), ()))
        prices = [row[1] for row in tape]
        returns = [prices[index] / prices[index - 1] - 1
                   for index in range(1, len(prices)) if prices[index - 1]]
        micro = {
            'source': 'REAL_PROVIDER_QUOTES',
            'volume_supported': False,
            'sample_count': len(tape),
            'window_seconds': (tape[-1][0] - tape[0][0]) if len(tape) >= 2 else 0.0,
            'very_short_momentum': returns[-1] if returns else None,
            'short_price_momentum': (prices[-1] / prices[0] - 1
                                     if len(prices) >= 2 and prices[0] else None),
            'very_short_realized_volatility': (
                sum(value * value for value in returns) ** .5
                if len(returns) >= 2 else None
            ),
            'recent_high': max(prices) if len(prices) >= 2 else None,
            'recent_low': min(prices) if len(prices) >= 2 else None,
            'latest_timestamp': (
                datetime.fromtimestamp(tape[-1][0], timezone.utc).isoformat()
                if tape else None
            ),
        }
        result['quote_microstructure'] = micro
        return result

    def _data(self, symbol):
        if self.history_refresher is not None:
            return self.history_refresher.get(symbol)
        return self.data_lookup(symbol) or {}

    def symbols(self, now=None):
        return list(self.universe())

    def close(self):
        if self.history_refresher is not None:
            self.history_refresher.close()

    @staticmethod
    def _print_eligible_candidate(trace):
        freshness = trace['freshness']
        signal = trace['signal']
        lifecycle = trace.get('episode_lifecycle', {})
        block = (trace.get('blocking_reasons') or ['NONE'])[0]
        print(
            "[SCALP CANDIDATE] "
            f"symbol={trace['symbol']} episode_id={trace.get('episode_id')} "
            f"setup={trace.get('setup_type')} "
            f"quote_age={freshness.get('quote_age_seconds')} "
            f"spread_pct={trace['spread'].get('observed_pct')} "
            f"execution_liquidity_status={trace['execution_liquidity'].get('status')} "
            f"volume_expansion={trace['volume_expansion'].get('observed')} "
            f"volume_expansion_threshold={trace['volume_expansion'].get('minimum')} "
            f"micro_bar_age={trace['micro_bars'].get('bar_age_seconds')} "
            f"signal_score={signal.get('score')} "
            f"signal_threshold={signal.get('minimum')} "
            f"score_margin={signal.get('score_margin')} "
            f"episode_age={lifecycle.get('episode_age_seconds')} "
            f"episode_status={lifecycle.get('episode_status', 'UNKNOWN')} "
            f"final_block_reason={block}",
            flush=True,
        )
        component_text = []
        for name, row in signal.get('components', {}).items():
            component_text.append(
                f"{name}[raw={row.get('raw')},normalized={row.get('normalized')},"
                f"weight={row.get('weight')},weighted_contribution={row.get('contribution')},"
                f"penalty={row.get('penalty')},clamp={row.get('clamp')},"
                f"final_contribution={row.get('final_contribution')}]"
            )
        print(
            "SCALP SCORE COMPONENTS "
            f"symbol={trace['symbol']} " + " ".join(component_text) + " "
            f"score_before_penalties={signal.get('score_before_penalties')} "
            f"total_penalties={signal.get('total_penalties')} "
            f"final_score={signal.get('score')} threshold={signal.get('minimum')} "
            f"score_margin={signal.get('score_margin')}",
            flush=True,
        )

    @staticmethod
    def _print_stale_episode(trace):
        lifecycle = trace.get('episode_lifecycle', {})
        print(
            "[STALE SCALP EPISODE] "
            f"symbol={trace['symbol']} episode_id={trace.get('episode_id')} "
            f"setup_type={trace.get('setup_type')} "
            f"episode_created_at={lifecycle.get('episode_created_at')} "
            f"episode_last_updated_at={lifecycle.get('episode_last_updated_at')} "
            f"episode_age_seconds={lifecycle.get('episode_age_seconds')} "
            f"stale_after_seconds={lifecycle.get('stale_after_seconds')} "
            f"initial_structural_fingerprint={lifecycle.get('initial_structural_fingerprint')} "
            f"current_structural_fingerprint={lifecycle.get('current_structural_fingerprint')} "
            f"structure_changed={lifecycle.get('structure_changed')} "
            f"new_episode_allowed={lifecycle.get('new_episode_allowed')} "
            f"reason_stale={lifecycle.get('reason_stale')}",
            flush=True,
        )

    @staticmethod
    def _print_trace(trace):
        freshness, spread = trace['freshness'], trace['spread']
        volume, signal = trace['volume_expansion'], trace['signal']
        execution_liquidity = trace['execution_liquidity']
        micro = trace['micro_bars']
        friction, geometry = trace['friction'], trace['geometry']
        pct = lambda value: 'UNAVAILABLE' if value is None else f'{value*100:.3f}%'
        number = lambda value: 'UNAVAILABLE' if value is None else f'{value:.4f}'
        print(
            f"[SCALP {trace['symbol']} episode={trace['episode_id']}] "
            f"setup={trace['setup_type']} quote_age={freshness['quote_age_seconds']}s "
            f"quote_status={freshness['quote_status']} "
            f"bar_timeframe={micro['bar_timeframe_seconds']}s "
            f"latest_completed_bar={micro['latest_completed_bar_timestamp']} "
            f"bar_age={micro['bar_age_seconds']}s "
            f"expected_next_close={micro['expected_next_bar_close']} "
            f"bar_status={micro['freshness_status']} "
            f"bar_provider={micro['provider_status']} "
            f"spread={pct(spread['observed_pct'])} "
            f"execution_liquidity={execution_liquidity['status']} "
            f"volume_expansion={number(volume['observed'])} "
            f"signal_score={number(signal['score'])} signal_status={signal['data_status']} "
            f"expected_move={pct(friction['expected_move_pct'])} "
            f"cost={pct(friction['estimated_round_trip_cost_pct'])} "
            f"net_edge={pct(friction['expected_net_edge_pct'])} "
            f"entry={number(geometry['entry'])} stop={number(geometry['stop'])} "
            f"target={number(geometry['target'])} gross_rr={number(geometry['gross_rr'])} "
            f"net_rr={number(geometry['net_rr'])} FINAL={trace['final']} "
            f"reasons={','.join(trace['rejection_reasons']) or 'NONE'}",
            flush=True,
        )
        components = signal.get('components', {})
        if components:
            print(
                f"SCALP SCORE {trace['symbol']} setup={trace['setup_type']} "
                + " ".join(
                    f"{name}[raw={number(row.get('raw'))},"
                    f"normalized={number(row.get('normalized'))},"
                    f"weight={number(row.get('weight'))},"
                    f"contribution={number(row.get('contribution'))}]"
                    for name, row in components.items()
                )
                + f" FINAL_SCORE={number(signal['score'])} "
                  f"MARGIN={number(signal.get('score_margin'))} "
                  f"THRESHOLD={config.SCALP_MIN_SIGNAL_SCORE:.2f} "
                  f"FINAL={'PASS' if signal['passed'] else 'FAIL'}",
                flush=True,
            )

    @staticmethod
    def _print_position_lifecycle(row):
        number = lambda value: 'UNAVAILABLE' if value is None else f'{value:.3f}'
        print(
            f"[SCALP POSITION {row['symbol']} episode={row['episode_id']}] "
            f"entry={row['entry_time']} hold={number(row['hold_seconds'])}s "
            f"max_hold={row['max_hold_seconds']}s "
            f"remaining={number(row['time_remaining_seconds'])}s "
            f"stop={number(row['stop'])} target={number(row['target'])} "
            f"exit_quote_age={number(row['latest_exit_quote_age_seconds'])}s "
            f"status={row['exit_status']} next={row['next_required_action']}",
            flush=True,
        )
