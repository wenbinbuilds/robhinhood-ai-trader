"""Independent deterministic shadow watcher: no LLM, subprocess, or broker IO."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from zoneinfo import ZoneInfo

import config
from execution.shadow_executor import ShadowExecutor
from shadow.portfolio import ShadowPortfolio
from watcher.models import ExitRequest, FastQuote, timestamp, price
from watcher.quote_provider import FastQuoteProvider
from watcher.storage import atomic_json, event


class FastPositionWatcher:
    def __init__(self, portfolio: ShadowPortfolio, provider: FastQuoteProvider,
                 executor: ShadowExecutor, *, status_path: Path, events_path: Path,
                 clock=None, interval=None, candidate_watcher=None,
                 event_orchestrator=None):
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("watcher is local SHADOW_TRADING only")
        self.portfolio, self.provider, self.executor = portfolio, provider, executor
        if executor.engine.portfolio is not portfolio:
            raise ValueError("watcher/executor must share one portfolio")
        self.status_path, self.events_path = Path(status_path), Path(events_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.candidate_watcher = candidate_watcher
        self.event_orchestrator = event_orchestrator
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
        self._age_total = self._age_count = self._duration_total = self._cycles = 0
        self.metrics = dict(quote_requests=0, quote_failures=0, average_quote_age=None,
                            max_quote_age=None, average_cycle_duration=0,
                            degraded_intervals=0, stop_exits=0, target_exits=0,
                            reconstructed_exits=0)
        self.status = dict(enabled=True, provider=provider.name, mode=provider.mode,
                           status="NOT_STARTED", poll_interval=self.interval,
                           last_quote_timestamp=None, open_symbols=[], metrics=self.metrics)

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

    def tick(self):
        started = monotonic()
        before = self.portfolio.snapshot()
        all_symbols = sorted({p.symbol for p in before.open_positions})
        candidate_symbols = (
            self.candidate_watcher.symbols(self.clock())
            if self.candidate_watcher is not None else []
        )
        requested_symbols = list(dict.fromkeys(all_symbols + candidate_symbols))
        symbols = requested_symbols[:config.FAST_WATCH_MAX_SYMBOLS]
        self.status["open_symbols"] = all_symbols
        self.status["watched_symbols"] = symbols
        self.status["position_symbols"] = all_symbols
        self.status["candidate_symbols"] = candidate_symbols
        self.status["unmonitored_symbols"] = requested_symbols[len(symbols):]
        quotes = {}
        failed = False
        if symbols:
            self.metrics["quote_requests"] += 1
            try:
                quotes = self.provider.get_quotes(symbols)
                if not hasattr(quotes, "get"):
                    raise ValueError("invalid quote batch")
            except Exception as exc:
                failed = True
                self.metrics["quote_failures"] += 1
                self._event("FAST_PROVIDER_ERROR", self.clock(), error=type(exc).__name__)
                self._event("FAST_WATCHER_UNAVAILABLE", self.clock())
        # Re-read the clock AFTER IO; never bless a quote that aged during a wait.
        now = self.clock()
        degraded = failed or self.provider.mode != "REALTIME_FAST" or len(symbols) < len(requested_symbols)
        fresh = {}
        for symbol in symbols:
            quote = quotes.get(symbol)
            try:
                age = quote.age_at(now) if isinstance(quote, FastQuote) else None
                valid = (quote.symbol == symbol and quote.timestamp.tzinfo is not None
                         and age is not None and 0 <= age <= config.FAST_QUOTE_MAX_AGE_SECONDS
                         and quote.mark_price is not None and bool(quote.source))
            except (AttributeError, TypeError, ValueError):
                age, valid = None, False
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

        # Establish/reset the local U.S. trading-day counters before a fast
        # candidate can be risk-checked and opened on the new session.
        if any(q.is_market_open is True for q in fresh.values()):
            self.portfolio.begin_cycle(now, [], is_regular_session=True)

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

        # A single transaction covers marks, risk checks, and local exits. The
        # provider is outside it; a slow worker never holds it while reasoning.
        position_events = []
        closed_events = []
        with self.portfolio.lock:
            for position in self.portfolio.state.open_positions:
                quote = fresh.get(position.symbol)
                if quote is None:
                    position.monitoring_status = "PRICE_MONITORING_DEGRADED"
                    continue
                entry_at = timestamp(position.entry_timestamp)
                if entry_at is None or quote.timestamp < entry_at:
                    # A symbol can be closed/reopened while a batch is in flight.
                    # Never apply an old position's quote to a later entry.
                    fresh.pop(position.symbol, None)
                    position.monitoring_status = "PRICE_MONITORING_DEGRADED"
                    degraded = True
                    continue
                old = timestamp(position.last_price_timestamp)
                if old and quote.timestamp < old:
                    continue
                position.last_price = quote.mark_price
                position.mark_price_method = "MIDPOINT" if quote.mid_price is not None else "LAST_TRADE_FALLBACK"
                position.current_bid, position.current_ask = quote.bid, quote.ask
                position.last_price_timestamp = quote.timestamp.isoformat()
                position.quote_source, position.quote_mode = quote.source, self.provider.mode
                position.monitoring_status = ("ACTIVE" if self.provider.mode == "REALTIME_FAST"
                                              and quote.is_market_open is True else "PRICE_MONITORING_DEGRADED")
                position_events.append((position.symbol, quote.mark_price))
            self.portfolio.revalue()
            state = self.portfolio.state
            hard_loss = (all(p.symbol in fresh for p in state.open_positions)
                         and state.daily_pnl + state.unrealized_pnl <= -state.starting_capital * config.MAX_DAILY_LOSS_PERCENT)
            for position in list(state.open_positions):
                quote = fresh.get(position.symbol)
                if quote is None or quote.is_market_open is not True or quote.exit_price is None:
                    degraded = True
                    continue
                old = timestamp(position.last_price_timestamp)
                if old and quote.timestamp < old:
                    continue
                reason = None
                # Long stops use either last or executable bid; targets require
                # the executable bid (last fallback if no valid book exists).
                if min(quote.exit_price, price(quote.last_price) or quote.exit_price) <= position.stop:
                    reason = "STOP_HIT"
                elif quote.exit_price >= position.target:
                    reason = "TARGET_HIT"
                entry = timestamp(position.entry_timestamp)
                carried = entry and entry.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date() < now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date()
                cutoff = (now >= quote.session_close - timedelta(minutes=config.FORCE_EXIT_MINUTES_BEFORE_CLOSE)
                          if quote.session_close else self.executor.engine.market_closing(now))
                if reason is None and (cutoff or carried):
                    reason = "END_OF_DAY_EXIT"
                if reason is None and hard_loss:
                    reason = "HARD_RISK_EXIT"
                if reason:
                    trade = self.executor.execute_exit(ExitRequest(
                        position.trade_id, position.symbol, reason, quote, self.provider.mode), now=now)
                    if trade is not None:
                        if reason == "STOP_HIT":
                            self.metrics["stop_exits"] += 1
                        elif reason == "TARGET_HIT":
                            self.metrics["target_exits"] += 1
                        event(self.events_path, reason, now, symbol=position.symbol)
                        event(self.events_path, "SHADOW_EXIT_EXECUTED", now, trade_id=trade.trade_id, exit_method=trade.exit_method)
                        closed_events.append((position.symbol, reason))
            self.metrics["reconstructed_exits"] = sum(p.exit_method == "RECONSTRUCTED_FROM_BAR_DATA" for p in state.closed_positions)
            if all_symbols:
                self.portfolio.save(now)
        if self.event_orchestrator is not None:
            for symbol, current_price in position_events:
                self.event_orchestrator.position_updated(symbol, now=now, price=current_price)
            for symbol, reason in closed_events:
                self.event_orchestrator.position_closed(symbol, now=now, reason=reason)
        if degraded and symbols:
            self.metrics["degraded_intervals"] += 1
            self._event("PRICE_MONITORING_DEGRADED", now, mode=self.provider.mode)
        if fresh and monotonic() - self._last_sample >= config.FAST_QUOTE_LOG_INTERVAL_SECONDS:
            self._last_sample = monotonic()
            self._event("FAST_QUOTE_UPDATE", now, symbols=sorted(fresh), mode=self.provider.mode)
        self._cycles += 1
        self._duration_total += monotonic() - started
        self.metrics["average_cycle_duration"] = self._duration_total / self._cycles
        self.metrics["average_quote_age"] = self._age_total / self._age_count if self._age_count else None
        self.status.update(heartbeat=now.isoformat(), status=("IDLE" if not symbols else
                           "FAST_WATCHER_UNAVAILABLE" if failed else "DEGRADED" if degraded else "ACTIVE"),
                           provider_metrics=dict(getattr(self.provider, "metrics", {})))
        atomic_json(self.status_path, self.status)
        return dict(self.status)
