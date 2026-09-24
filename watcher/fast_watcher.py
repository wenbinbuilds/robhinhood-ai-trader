"""Independent deterministic shadow watcher: no LLM, subprocess, or broker IO."""
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict

import config
from execution.shadow_executor import ShadowExecutor
from shadow.portfolio import ShadowPortfolio
from watcher.models import ExitRequest, FastQuote, timestamp, price
from watcher.quote_provider import FastQuoteProvider, quote_provenance
from watcher.storage import atomic_json, event
from trading_runtime.contracts import MarketSnapshot, Quality
from strategies.identity import is_scalp_strategy


def scalp_status_summary(diagnostic):
    """Keep universe attrition distinct from post-eligibility rejection."""

    universe = dict(diagnostic.get('filtered_reasons', {}))
    eligible = dict(diagnostic.get('entry_blocked_reasons', {}))
    primary = lambda values: (
        min(values, key=lambda name: (-values[name], name)) if values else 'NONE'
    )
    return {
        'universe_primary_filter': primary(universe),
        'eligible_candidate_primary_block': primary(eligible),
        'universe_filter_counts': universe,
        'eligible_candidate_block_counts': eligible,
    }


class PositionController:
    def __init__(self, portfolio: ShadowPortfolio, provider: FastQuoteProvider,
                 executor: ShadowExecutor, *, status_path: Path, events_path: Path,
                 clock=None, interval=None, candidate_watcher=None,
                 event_orchestrator=None, scalp_runtime=None, debug=False,
                 dashboard=None, scalp_v2_runtime=None):
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("watcher is local SHADOW_TRADING only")
        self.portfolio, self.provider, self.executor = portfolio, provider, executor
        if executor.engine.portfolio is not portfolio:
            raise ValueError("watcher/executor must share one portfolio")
        self.status_path, self.events_path = Path(status_path), Path(events_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.candidate_watcher = candidate_watcher
        self.event_orchestrator = event_orchestrator
        self.scalp_runtime = scalp_runtime
        self.scalp_v2_runtime = scalp_v2_runtime
        self.debug = bool(debug)
        self.dashboard = dashboard
        if (candidate_watcher is not None and event_orchestrator is not None
                and candidate_watcher.state_store is not event_orchestrator.state_store):
            raise ValueError("fast watcher diagnostics must share CandidateStateStore")
        self.interval = config.FAST_QUOTE_INTERVAL_SECONDS if interval is None else interval
        if self.interval <= 0 or config.FAST_WATCH_MAX_SYMBOLS < 1:
            raise ValueError("invalid watcher configuration")
        self.stop_event = Event()
        self.thread = None
        self._last_events = {}
        self._last_sample = float("-inf")
        self._last_tick_started = None
        self._last_exchange_timestamp = {}
        self._last_poll_at = {}
        self._poll_intervals = defaultdict(list)
        self._session_universe_filters = Counter()
        self._session_eligible_blocks = Counter()
        # Keep diagnostics beside the injected watcher event log.  Production
        # still resolves to logs/quote_provenance.jsonl, while isolated tests
        # cannot leak synthetic quotes into the real session log.
        self.quote_provenance_path = self.events_path.with_name(
            Path(config.QUOTE_PROVENANCE_LOG_PATH).name
        )
        self.cycle_timing_path = self.events_path.with_name(
            'fast_watcher_latency.jsonl'
        )
        self._age_total = self._age_count = self._duration_total = self._cycles = 0
        self.metrics = dict(quote_requests=0, quote_failures=0, average_quote_age=None,
                            max_quote_age=None, average_cycle_duration=0,
                            degraded_intervals=0, stop_exits=0, target_exits=0,
                            reconstructed_exits=0)
        self.status = dict(enabled=True, provider=provider.name, mode=provider.mode,
                           status="NOT_STARTED", poll_interval=self.interval,
                           last_quote_timestamp=None, open_symbols=[], metrics=self.metrics)
        if self.scalp_runtime is not None:
            self.status['scalp_recovery'] = dict(
                getattr(self.scalp_runtime, 'startup_recovery', {})
            )

    def _event(self, kind, now, *, symbol=None, **fields):
        # Repeated failures/staleness are sampled, while every exit is recorded.
        key = (kind, symbol)
        previous = self._last_events.get(key)
        if previous is not None and (now - previous).total_seconds() < config.FAST_QUOTE_LOG_INTERVAL_SECONDS:
            return
        self._last_events[key] = now
        event(self.events_path, kind, now, symbol=symbol, **fields)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("watcher already started")
        self.stop_event.clear()
        self._event("FAST_WATCHER_STARTED", self.clock(), mode=self.provider.mode)
        self.thread = Thread(target=self._run, name="shadow-fast-watcher", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()  # Providers must implement bounded IO, never model calls.
        if self.scalp_runtime is not None and hasattr(self.scalp_runtime, 'close'):
            self.scalp_runtime.close()
        self.status.update(status="STOPPED", heartbeat=self.clock().isoformat())
        atomic_json(self.status_path, self.status)

    def _run(self):
        while not self.stop_event.is_set():
            started = monotonic()
            try:
                self.tick()
            except Exception as exc:
                self.status.update(status="FAST_WATCHER_UNAVAILABLE", error=type(exc).__name__)
                # A persistence failure is visible on stderr even if disk logging fails.
                print(f"FAST_WATCHER_UNAVAILABLE: {type(exc).__name__}", flush=True)
            self.stop_event.wait(max(0.01, self.interval - (monotonic() - started)))

    def _manage_position_strategy(self, fresh, all_symbols, now, degraded):
        """Mark and exit POSITION holdings before either strategy can enter."""

        position_events = []
        closed_events = []
        with self.portfolio.lock:
            for position in self.portfolio.state.open_positions:
                if is_scalp_strategy(position.strategy_id or position.strategy):
                    continue
                quote = fresh.get(position.symbol)
                if quote is None:
                    position.monitoring_status = "PRICE_MONITORING_DEGRADED"
                    continue
                entry_at = timestamp(position.entry_timestamp)
                if entry_at is None or quote.timestamp < entry_at:
                    fresh.pop(position.symbol, None)
                    position.monitoring_status = "PRICE_MONITORING_DEGRADED"
                    degraded = True
                    continue
                old = timestamp(position.last_price_timestamp)
                if old and quote.timestamp < old:
                    continue
                position.last_price = quote.mark_price
                position.mark_price_method = (
                    "MIDPOINT" if quote.mid_price is not None else "LAST_TRADE_FALLBACK"
                )
                position.current_bid, position.current_ask = quote.bid, quote.ask
                position.last_price_timestamp = quote.timestamp.isoformat()
                position.quote_source, position.quote_mode = quote.source, self.provider.mode
                position.monitoring_status = (
                    "ACTIVE" if self.provider.mode == "REALTIME_FAST"
                    and quote.is_market_open is True
                    else "PRICE_MONITORING_DEGRADED"
                )
                position_events.append((position.symbol, quote.mark_price))
            self.portfolio.revalue({
                symbol: quote.mark_price for symbol, quote in fresh.items()
            })
            state = self.portfolio.state
            hard_loss = (
                all(position.symbol in fresh for position in state.open_positions)
                and state.daily_pnl + state.unrealized_pnl
                <= -state.starting_capital * config.MAX_DAILY_LOSS_PERCENT
            )
            for position in list(state.open_positions):
                if is_scalp_strategy(position.strategy_id or position.strategy):
                    continue
                quote = fresh.get(position.symbol)
                if quote is None or quote.is_market_open is not True or quote.exit_price is None:
                    degraded = True
                    continue
                old = timestamp(position.last_price_timestamp)
                if old and quote.timestamp < old:
                    continue
                reason = None
                if min(quote.exit_price, price(quote.last_price) or quote.exit_price) <= position.stop:
                    reason = "STOP_HIT"
                elif quote.exit_price >= position.target:
                    reason = "TARGET_HIT"
                entry = timestamp(position.entry_timestamp)
                carried = entry and entry.astimezone(
                    ZoneInfo(config.MARKET_TIMEZONE)
                ).date() < now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date()
                cutoff = (
                    now >= quote.session_close
                    - timedelta(minutes=config.FORCE_EXIT_MINUTES_BEFORE_CLOSE)
                    if quote.session_close else self.executor.engine.market_closing(now)
                )
                if cutoff or carried:
                    reason = "MISSED_EOD_RECOVERY_EXIT" if carried else "END_OF_DAY_EXIT"
                if reason is None and hard_loss:
                    reason = "HARD_RISK_EXIT"
                if reason:
                    trade = self.executor.execute_exit(ExitRequest(
                        position.trade_id, position.symbol, reason, quote,
                        self.provider.mode,
                    ), now=now)
                    if trade is not None:
                        if reason == "STOP_HIT":
                            self.metrics["stop_exits"] += 1
                        elif reason == "TARGET_HIT":
                            self.metrics["target_exits"] += 1
                        event(self.events_path, reason, now, symbol=position.symbol)
                        event(
                            self.events_path, "SHADOW_EXIT_EXECUTED", now,
                            trade_id=trade.trade_id, exit_method=trade.exit_method,
                        )
                        closed_events.append((position.symbol, reason))
            self.metrics["reconstructed_exits"] = sum(
                position.exit_method == "RECONSTRUCTED_FROM_BAR_DATA"
                for position in state.closed_positions
            )
            if all_symbols:
                self.portfolio.save(now)
        return degraded, position_events, closed_events

    def tick(self):
        started = monotonic()
        cycle_wall_started = self.clock()
        if cycle_wall_started.tzinfo is None:
            cycle_wall_started = cycle_wall_started.replace(tzinfo=timezone.utc)
        quote_batch_started = started
        poll_cycle_id = 'QUOTE-POLL-' + uuid4().hex
        loop_delay = (max(0.0, started-self._last_tick_started-self.interval)
                      if self._last_tick_started is not None else 0.0)
        self._last_tick_started = started
        discovery_started = monotonic()
        before = self.portfolio.snapshot()
        all_symbols = sorted({p.symbol for p in before.open_positions})
        candidate_symbols = (
            self.candidate_watcher.symbols(self.clock())
            if self.candidate_watcher is not None else []
        )
        scalp_symbols = self.scalp_runtime.symbols(self.clock()) if self.scalp_runtime is not None else []
        # V2 records a logical FAST_WATCH set, but the control's provider
        # universe/order remains untouched. Activating a separate prioritized
        # provider schedule requires a later controlled capacity experiment.
        scalp_v2_symbols = (
            self.scalp_v2_runtime.fast_watch_symbols()
            if self.scalp_v2_runtime is not None else []
        )
        requested_symbols = list(dict.fromkeys(
            all_symbols + candidate_symbols + scalp_symbols
        ))
        symbols = requested_symbols[:config.FAST_WATCH_MAX_SYMBOLS]
        discovery_duration = monotonic() - discovery_started
        self.status["open_symbols"] = all_symbols
        self.status["watched_symbols"] = symbols
        self.status["position_symbols"] = all_symbols
        self.status["candidate_symbols"] = candidate_symbols
        self.status["scalp_symbols"] = scalp_symbols
        self.status["scalp_v2_fast_watch_symbols"] = scalp_v2_symbols
        self.status["unmonitored_symbols"] = requested_symbols[len(symbols):]
        quotes = {}
        failed = False
        if symbols:
            self.metrics["quote_requests"] += 1
            try:
                if 'poll_cycle_id' in signature(self.provider.get_quotes).parameters:
                    quotes = self.provider.get_quotes(
                        symbols, poll_cycle_id=poll_cycle_id
                    )
                else:
                    quotes = self.provider.get_quotes(symbols)
                if not hasattr(quotes, "get"):
                    raise ValueError("invalid quote batch")
            except Exception as exc:
                failed = True
                self.metrics["quote_failures"] += 1
                self._event("FAST_PROVIDER_ERROR", self.clock(), error=type(exc).__name__)
                self._event("FAST_WATCHER_UNAVAILABLE", self.clock())
        quote_batch_duration = monotonic() - quote_batch_started
        # Re-read the clock AFTER IO; never bless a quote that aged during a wait.
        now = self.clock()
        degraded = failed or self.provider.mode != "REALTIME_FAST" or len(symbols) < len(requested_symbols)
        ingest_started = monotonic()
        fresh = {}
        scalp_observed = {}
        quote_quality = {}
        for symbol in symbols:
            previous_poll = self._last_poll_at.get(symbol)
            if previous_poll is not None:
                samples = self._poll_intervals[symbol]
                samples.append((now-previous_poll).total_seconds())
                if len(samples) > 10_000:
                    del samples[:-10_000]
            self._last_poll_at[symbol] = now
            quote = quotes.get(symbol)
            snapshot = MarketSnapshot.from_quote(quote, symbol=symbol, now=now,
                                                 max_age=config.FAST_QUOTE_MAX_AGE_SECONDS)
            scalp_snapshot = MarketSnapshot.from_quote(
                quote, symbol=symbol, now=now,
                max_age=config.SCALP_MAX_QUOTE_AGE_SECONDS,
            )
            trace = quote_provenance(
                quote, symbol=symbol, evaluation_at=now,
                maximum_age_seconds=config.SCALP_MAX_QUOTE_AGE_SECONDS,
                provider_status=(
                    'ERROR' if failed else 'OK' if quote is not None else 'NO_QUOTE'
                ),
                poll_cycle_id=poll_cycle_id,
                previous_exchange_timestamp=self._last_exchange_timestamp.get(symbol),
            )
            scopes = []
            if symbol in all_symbols or symbol in candidate_symbols:
                scopes.append('POSITION')
            if symbol in scalp_symbols:
                scopes.append('SCALP')
            trace.update({
                'request_context': 'FAST_WATCHER',
                'strategy_scopes': scopes,
                'requested_this_cycle': True,
                'batch_size': len(symbols),
                # Direct quote collection is one provider batch request.  The
                # server may parallelize internally, but the client does not.
                'provider_concurrency': 1,
                'full_universe_cycle_duration_seconds': quote_batch_duration,
                'poll_interval_since_previous_seconds': (
                    self._poll_intervals[symbol][-1]
                    if self._poll_intervals[symbol] else None
                ),
                'freshness_by_strategy': {
                    'POSITION': (
                        snapshot.quote_age is not None
                        and 0 <= snapshot.quote_age <= config.FAST_QUOTE_MAX_AGE_SECONDS
                    ),
                    'SCALP': (
                        scalp_snapshot.quote_age is not None
                        and 0 <= scalp_snapshot.quote_age
                        <= config.SCALP_MAX_QUOTE_AGE_SECONDS
                    ),
                },
                'freshness_thresholds_seconds': {
                    'POSITION': config.FAST_QUOTE_MAX_AGE_SECONDS,
                    'SCALP': config.SCALP_MAX_QUOTE_AGE_SECONDS,
                },
            })
            if quote is not None:
                self._last_exchange_timestamp[symbol] = quote.timestamp
            quote_quality[symbol] = {
                "provider_status": (
                    "ERROR" if failed else "OK" if quote is not None else "UNAVAILABLE"
                ),
                "quote_status": scalp_snapshot.quote_status.value,
                "quote_age_seconds": scalp_snapshot.quote_age,
                "spread_pct": scalp_snapshot.spread,
                "provenance": trace,
            }
            event(
                self.quote_provenance_path, 'QUOTE_REQUEST_TRACE', now,
                max_bytes=config.QUOTE_PROVENANCE_LOG_MAX_BYTES, **trace,
            )
            if quote is not None and scalp_snapshot.quote_status != Quality.INVALID:
                # Scalp entry receives provider-successful stale observations so
                # its own hard freshness gate can record STALE_QUOTE. Position
                # management still receives only globally fresh observations.
                scalp_observed[symbol] = quote
            age = snapshot.quote_age
            valid = snapshot.quote_status == Quality.FRESH and bool(snapshot.provider)
            if age is not None and age >= 0:
                self._age_total += age
                self._age_count += 1
                self.metrics["max_quote_age"] = max(self.metrics["max_quote_age"] or 0, age)
                at = quote.timestamp.isoformat()
                last = timestamp(self.status["last_quote_timestamp"])
                if last is None or quote.timestamp > last:
                    self.status["last_quote_timestamp"] = at
            if valid:
                fresh[symbol] = quote
            else:
                degraded = True
                self._event("FAST_QUOTE_STALE", now, symbol=symbol)
        quote_ingest_duration = monotonic() - ingest_started
        quote_ingested_at = self.clock()
        if quote_ingested_at.tzinfo is None:
            quote_ingested_at = quote_ingested_at.replace(tzinfo=timezone.utc)

        # Establish/reset the local U.S. trading-day counters before a fast
        # candidate can be risk-checked and opened on the new session.
        if any(q.is_market_open is True for q in fresh.values()):
            self.portfolio.begin_cycle(now, [], is_regular_session=True)

        # Canonical open-position work always wins the cycle. POSITION exits
        # are committed here; SCALP exits run at the start of its runtime call.
        # Only after both paths have processed exits may discovery open risk.
        position_started = monotonic()
        degraded, position_events, closed_events = self._manage_position_strategy(
            fresh, all_symbols, now, degraded
        )
        if self.event_orchestrator is not None:
            for symbol, current_price in position_events:
                self.event_orchestrator.position_updated(
                    symbol, now=now, price=current_price
                )
            for symbol, reason in closed_events:
                self.event_orchestrator.position_closed(
                    symbol, now=now, reason=reason
                )
        position_work_duration = monotonic() - position_started

        # Existing scalp positions own the cycle before any momentum candidate
        # work. This keeps mandatory exits ahead of pre-execution refresh IO.
        scalp_result = None
        scalp_started = monotonic()
        if self.scalp_runtime is not None:
            scalp_kwargs = {
                'now': now,
                'entry_quotes': {
                    symbol: scalp_observed[symbol] for symbol in scalp_symbols
                    if symbol in scalp_observed
                },
                'quote_quality': {
                    symbol: quote_quality[symbol] for symbol in scalp_symbols
                    if symbol in quote_quality
                },
            }
            if 'loop_timing' in signature(self.scalp_runtime.on_quotes).parameters:
                scalp_kwargs['loop_timing'] = {
                    'fast_watcher_loop_delay_seconds': loop_delay,
                    'configured_interval_seconds': self.interval,
                    'poll_cycle_id': poll_cycle_id,
                    'quote_batch_duration_seconds': quote_batch_duration,
                    'symbol_discovery_duration_seconds': discovery_duration,
                    'quote_ingest_duration_seconds': quote_ingest_duration,
                    'quote_ingested_at': quote_ingested_at.astimezone(
                        timezone.utc
                    ).isoformat(),
                    'position_work_duration_seconds': position_work_duration,
                    'cycle_started_at': cycle_wall_started.astimezone(
                        timezone.utc
                    ).isoformat(),
                    'symbols_requested': len(symbols),
                    'provider_concurrency': 1,
                }
            if 'position_quotes' in signature(self.scalp_runtime.on_quotes).parameters:
                scalp_kwargs['position_quotes'] = {
                    symbol: scalp_observed[symbol] for symbol in all_symbols
                    if symbol in scalp_observed
                }
            scalp_result = self.scalp_runtime.on_quotes(fresh, **scalp_kwargs)
            self.status["scalp_discovery"] = scalp_result["diagnostics"]
            self.status["quote_quality"] = quote_quality
            if self.event_orchestrator is not None:
                for trade in scalp_result['exits']:
                    self.event_orchestrator.position_closed(
                        trade.symbol, now=now, reason=trade.exit_reason)
                for detail in scalp_result['entries']:
                    if detail.get('status') == 'OPENED':
                        self.event_orchestrator.position_opened(
                            detail['symbol'], now=now, trade_id=detail['trade_id'])
        scalp_work_duration = monotonic() - scalp_started

        scalp_v2_result = None
        scalp_v2_started = monotonic()
        if self.scalp_v2_runtime is not None and scalp_result is not None:
            scalp_v2_result = self.scalp_v2_runtime.on_v1_cycle(
                scalp_result, now=now,
            )
            self.status["scalp_v2_research"] = {
                key: scalp_v2_result.get(key) for key in (
                    "strategy_id", "research_only", "armed", "armed_symbols",
                    "fast_triggers", "entry_ready", "open_research_positions",
                    "closed_research_trades", "research_realized_pnl",
                    "research_unrealized_pnl", "duration_ms",
                )
            }
        scalp_v2_work_duration = monotonic() - scalp_v2_started

        candidate_started = monotonic()
        if self.candidate_watcher is not None:
            if self.event_orchestrator is not None:
                for symbol in candidate_symbols:
                    quote = fresh.get(symbol)
                    if quote is not None:
                        self.event_orchestrator.quote(quote)
            else:
                self.candidate_watcher.process_quotes(fresh, now=now)

        if self.event_orchestrator is not None:
            for symbol in all_symbols:
                quote = fresh.get(symbol)
                if quote is not None:
                    self.event_orchestrator.quote(quote, position=True)
        candidate_work_duration = monotonic() - candidate_started

        terminal_started = monotonic()
        if degraded and symbols:
            self.metrics["degraded_intervals"] += 1
            self._event("PRICE_MONITORING_DEGRADED", now, mode=self.provider.mode)
        sample_due = monotonic() - self._last_sample >= config.FAST_QUOTE_LOG_INTERVAL_SECONDS
        # The scalp funnel is an outcome of every discovery poll, not a sampled
        # quote-health metric. Persist and display each cycle so a short-lived
        # blocking stage cannot disappear between terminal samples.
        if scalp_result is not None and self.debug:
            diagnostic = scalp_result["diagnostics"]
            funnel = diagnostic.get('funnel')
            if funnel is None:  # Compatibility for injected watcher probes.
                print("SCALP DISCOVERY: " + " ".join(
                    f"{key}={value}" for key, value in diagnostic.items()
                ), flush=True)
            else:
                print(
                    "SCALP FUNNEL: "
                    f"universe={funnel['universe_observations']} "
                    f"provider_ok={funnel['provider_ok']} "
                    f"duplicate_pass={funnel['duplicate_symbol_pass']} "
                    f"runtime_safety_pass={funnel['runtime_safety_pass']} "
                    f"quote_fresh={funnel['quote_fresh']} "
                    f"market_open={funnel['market_open_pass']} "
                    f"valid_book={funnel['valid_bid_ask']} "
                    f"spread_pass={funnel['spread_pass']} "
                    f"micro_bars_pass={funnel['micro_bars_pass']} "
                    f"execution_liquidity_pass={funnel['execution_liquidity_pass']} "
                    f"volume_expansion_pass={funnel['volume_expansion_pass']} "
                    f"micro_detected_anywhere={funnel['micro_signals_detected_anywhere']} "
                    f"eligible_micro_signals={funnel['eligible_micro_signals']}",
                    flush=True,
                )
                print(
                    "SCALP SIGNAL FUNNEL: "
                    f"episode_open={funnel['episode_open_pass']} "
                    f"signal_pass={funnel['signal_score_pass']} "
                    f"extension_pass={funnel['extension_pass']} "
                    f"stop_pass={funnel['stop_pass']} "
                    f"edge_pass={funnel['expected_edge_pass']} "
                    f"target_pass={funnel['target_pass']} "
                    f"scalp_rr_pass={funnel['scalp_rr_pass']} "
                    f"geometry_pass={funnel['geometry_pass']} "
                    f"overtrading_pass={funnel['overtrading_pass']}",
                    flush=True,
                )
                print(
                    "SCALP ENTRY FUNNEL: "
                    f"entry_attempts={funnel['entry_attempts']} "
                    f"risk_attempts={funnel['risk_attempts']} "
                    f"risk_approved={funnel['risk_approved']} "
                    f"risk_rejected={funnel['risk_rejected']} "
                    f"pre_execution_attempts={funnel['pre_execution_attempts']} "
                    f"pre_execution_passes={funnel['pre_execution_passes']} "
                    f"pre_execution_failures={funnel['pre_execution_failures']} "
                    f"portfolio_attempts={funnel['portfolio_attempts']} "
                    f"portfolio_approved={funnel['portfolio_approved']} "
                    f"portfolio_blocked={funnel['portfolio_blocked']} "
                    f"safety_approved={funnel['safety_approved']} "
                    f"entries={funnel['entries']}",
                    flush=True,
                )
                status_summary = scalp_status_summary(diagnostic)
                universe_filters = status_summary['universe_filter_counts']
                eligible_blocks = status_summary['eligible_candidate_block_counts']
                self._session_universe_filters.update(universe_filters)
                self._session_eligible_blocks.update(eligible_blocks)
                universe_primary = status_summary['universe_primary_filter']
                eligible_primary = status_summary['eligible_candidate_primary_block']
                print(
                    "SCALP STATUS\n"
                    f"universe={funnel['universe_observations']} "
                    f"fresh_quotes={funnel['quote_fresh']} "
                    f"volume_expansion_pass={funnel['volume_expansion_pass']} "
                    f"classified_setups={funnel['micro_signals_detected_anywhere']} "
                    f"eligible={funnel['eligible_micro_signals']} "
                    f"signal_pass={funnel['signal_score_pass']} "
                    f"entry_attempts={funnel['entry_attempts']} "
                    f"universe_primary_filter={universe_primary} "
                    f"eligible_candidate_primary_block={eligible_primary}",
                    flush=True,
                )
                print(
                    "UNIVERSE_FILTER_COUNTS: "
                    + (" ".join(f"{key}={value}" for key, value in sorted(
                        universe_filters.items(), key=lambda item: (-item[1], item[0])
                    )) or "NONE"),
                    flush=True,
                )
                print(
                    "ELIGIBLE_CANDIDATE_BLOCK_COUNTS: "
                    + (" ".join(f"{key}={value}" for key, value in sorted(
                        eligible_blocks.items(), key=lambda item: (-item[1], item[0])
                    )) or "NONE"),
                    flush=True,
                )
                print(
                    "SESSION_CUMULATIVE_COUNTS: "
                    "universe_filters="
                    + (",".join(
                        f"{key}:{value}" for key, value in
                        self._session_universe_filters.most_common()
                    ) or "NONE")
                    + " eligible_blocks="
                    + (",".join(
                        f"{key}:{value}" for key, value in
                        self._session_eligible_blocks.most_common()
                    ) or "NONE")
                    + f" entries={self.metrics.get('scalp_entries', 0) + funnel['entries']}",
                    flush=True,
                )
                self.metrics['scalp_entries'] = (
                    self.metrics.get('scalp_entries', 0) + funnel['entries']
                )
                bars = diagnostic.get('micro_bar_status', {})
                if bars:
                    print(
                        "SCALP MICRO BAR STATUS: "
                        f"provider_ok={bars.get('provider_history_ok', 0)} "
                        f"fresh={bars.get('fresh', 0)} "
                        f"aging={bars.get('aging', 0)} "
                        f"stale={bars.get('stale', 0)} "
                        f"unavailable={bars.get('unavailable', 0)} "
                        f"age_median={bars.get('age_median_seconds')}s "
                        f"age_p95={bars.get('age_p95_seconds')}s "
                        f"age_max={bars.get('age_max_seconds')}s "
                        f"refresh_attempts={bars.get('refresh_attempts', 0)} "
                        f"refresh_successes={bars.get('refresh_successes', 0)} "
                        f"refresh_unchanged={bars.get('refresh_unchanged', 0)} "
                        f"refresh_failures={bars.get('refresh_failures', 0)} "
                        f"refresh_in_progress={bars.get('refresh_in_progress', False)} "
                        f"refresh_duration={bars.get('refresh_timing', {}).get('duration_seconds')}s "
                        f"quotes_aged_out={bars.get('refresh_timing', {}).get('quotes_crossing_max_age_during_refresh')} "
                        f"loop_block={bars.get('event_loop_blocking_duration_seconds')}s",
                        flush=True,
                    )
                for label, name in (
                    ('SCALP FILTERED REASONS', 'filtered_reasons'),
                    ('SCALP ENTRY BLOCKED REASONS', 'entry_blocked_reasons'),
                    ('SCALP PRE-EXECUTION FAILURES', 'pre_execution_failure_reasons'),
                ):
                    values = diagnostic.get(name, {})
                    print(
                        label + ': ' + (" ".join(
                            f"{key}={value}" for key, value in sorted(
                                values.items(), key=lambda item: (-item[1], item[0])
                            )
                        ) or 'NONE'),
                        flush=True,
                    )
                setup_types = diagnostic.get('setup_types', {})
                print(
                    'SCALP SETUPS (detected/eligible/attempted/entered): '
                    + (' '.join(
                        f"{name}={counts.get('detected_anywhere', 0)}/"
                        f"{counts.get('eligible_after_early_gates', 0)}/"
                        f"{counts.get('entry_attempts', 0)}/"
                        f"{counts.get('entries', 0)}"
                        for name, counts in sorted(setup_types.items())
                    ) or 'NONE'),
                    flush=True,
                )
        if self.dashboard is not None:
            projected_status = (
                "FAST_WATCHER_UNAVAILABLE" if failed else
                "DEGRADED" if degraded else "ACTIVE"
            )
            self.dashboard.update(
                now=now, provider=self.provider.name,
                provider_mode=self.provider.mode,
                watcher_status=projected_status,
                quotes=quotes, scalp_result=scalp_result,
                scalp_v2_result=scalp_v2_result,
            )
        terminal_render_duration = monotonic() - terminal_started
        if (fresh or scalp_result is not None) and sample_due:
            self._last_sample = monotonic()
            if fresh:
                self._event("FAST_QUOTE_UPDATE", now, symbols=sorted(fresh), mode=self.provider.mode)
        self._cycles += 1
        self._duration_total += monotonic() - started
        self.metrics["average_cycle_duration"] = self._duration_total / self._cycles
        self.metrics["average_quote_age"] = self._age_total / self._age_count if self._age_count else None
        self.status['polling'] = {
            'configured_interval_seconds': self.interval,
            'latest_batch_size': len(symbols),
            'latest_full_universe_cycle_duration_seconds': quote_batch_duration,
            'provider_concurrency': 1,
            'per_symbol': {
                symbol: {
                    'poll_attempts': len(samples) + 1,
                    'median_poll_interval_seconds': (
                        sorted(samples)[len(samples)//2] if samples else None
                    ),
                    'max_poll_interval_seconds': max(samples) if samples else None,
                }
                for symbol, samples in sorted(self._poll_intervals.items())
            },
        }
        self.status.update(heartbeat=now.isoformat(), status=("IDLE" if not symbols else
                           "FAST_WATCHER_UNAVAILABLE" if failed else "DEGRADED" if degraded else "ACTIVE"),
                           provider_metrics=dict(getattr(self.provider, "metrics", {})))
        status_started = monotonic()
        atomic_json(self.status_path, self.status)
        status_persistence_duration = monotonic() - status_started
        cycle_duration = monotonic() - started
        current_positions = {
            item.symbol: item for item in self.portfolio.snapshot().open_positions
        }
        position_monitoring = {}
        for symbol in all_symbols:
            quote = quotes.get(symbol)
            checked = symbol in fresh and quote is not None
            position = current_positions.get(symbol)
            position_monitoring[symbol] = {
                'latest_mark_exchange_timestamp': (
                    quote.timestamp.astimezone(timezone.utc).isoformat()
                    if quote is not None and quote.timestamp.tzinfo else None
                ),
                'latest_mark_age_seconds': (
                    quote.age_at(now) if quote is not None else None
                ),
                'stop_check_at': now.astimezone(timezone.utc).isoformat()
                if checked else None,
                'target_check_at': now.astimezone(timezone.utc).isoformat()
                if checked else None,
                'provider_latency_ms': (
                    quote.provider_latency_seconds * 1000
                    if quote is not None
                    and quote.provider_latency_seconds is not None else None
                ),
                'fallback_used': bool(
                    quote is not None and quote.mid_price is None
                    and price(quote.last_price) is not None
                ),
                'monitoring_status': (
                    position.monitoring_status if position is not None else 'CLOSED'
                ),
                'provider_mode': self.provider.mode,
            }
        cycle_timing = {
            'poll_cycle_id': poll_cycle_id,
            'cycle_started_at': cycle_wall_started.astimezone(timezone.utc).isoformat(),
            'cycle_finished_at': self.clock().astimezone(timezone.utc).isoformat(),
            'configured_interval_seconds': self.interval,
            'cycle_duration_seconds': cycle_duration,
            'cycle_to_cycle_delay_seconds': loop_delay,
            'estimated_wait_seconds': max(0.0, self.interval-cycle_duration),
            'symbol_discovery_duration_seconds': discovery_duration,
            'quote_batch_duration_seconds': quote_batch_duration,
            'quote_ingest_duration_seconds': quote_ingest_duration,
            'position_work_duration_seconds': position_work_duration,
            'scalp_work_duration_seconds': scalp_work_duration,
            'scalp_v2_research_duration_seconds': scalp_v2_work_duration,
            'candidate_work_duration_seconds': candidate_work_duration,
            'terminal_render_duration_seconds': terminal_render_duration,
            'status_persistence_duration_seconds': status_persistence_duration,
            'symbols_requested': len(symbols),
            'provider_concurrency': 1,
            'dashboard_enabled': self.dashboard is not None,
            'debug_output_enabled': self.debug,
            'position_monitoring': position_monitoring,
        }
        self.status['latest_cycle_timing'] = cycle_timing
        event(
            self.cycle_timing_path, 'FAST_CYCLE_TIMING', self.clock(),
            max_bytes=config.FAST_EVENT_LOG_MAX_BYTES, **cycle_timing,
        )
        return dict(self.status)


# Backward-compatible public name; one implementation owns position monitoring.
FastPositionWatcher = PositionController
