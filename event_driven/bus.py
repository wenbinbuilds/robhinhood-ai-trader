"""Priority event bus with newest-quote-wins backpressure."""

from __future__ import annotations

from collections import defaultdict
from heapq import heapify, heappop, heappush
from itertools import count
from threading import Condition, Event, Thread
from typing import Callable

from event_driven.events import EventType, MarketEvent, QuoteEvent

EventHandler = Callable[[MarketEvent], None]


class InProcessEventBus:
    """Thread-safe event queue.

    Quote events are coalesced per symbol: a queued older quote becomes a cheap
    tombstone when a newer quote arrives. Non-quote events preserve priority
    and insertion order. Handler exceptions are isolated and retained for
    diagnostics instead of terminating the dispatcher.
    """

    def __init__(self, *, max_pending: int = 1_000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
        self._condition = Condition()
        self._heap: list[tuple[int, int, MarketEvent]] = []
        self._sequence = count()
        self._handlers: dict[EventType | None, list[EventHandler]] = defaultdict(list)
        self._latest_quote: dict[str, str] = {}
        self._stop = Event()
        self._thread: Thread | None = None
        self.failures: list[dict[str, str]] = []
        self.dropped_events = 0
        self.coalesced_quotes = 0

    def subscribe(self, event_type: EventType | None, handler: EventHandler) -> None:
        with self._condition:
            self._handlers[event_type].append(handler)

    def publish(self, item: MarketEvent) -> bool:
        with self._condition:
            if isinstance(item, QuoteEvent) and item.symbol:
                previous_id = self._latest_quote.get(item.symbol)
                if previous_id is not None:
                    self.coalesced_quotes += 1
                    self._heap = [row for row in self._heap if row[2].event_id != previous_id]
                    heapify(self._heap)
            if len(self._heap) >= self.max_pending:
                # Risk/position events must enter even under pressure. Remove a
                # lowest-priority pending event; otherwise reject the new one.
                worst = max(range(len(self._heap)), key=lambda i: (self._heap[i][0], self._heap[i][1]))
                if self._heap[worst][0] <= int(item.priority):
                    self.dropped_events += 1
                    return False
                removed = self._heap[worst][2]
                if (isinstance(removed, QuoteEvent) and removed.symbol
                        and self._latest_quote.get(removed.symbol) == removed.event_id):
                    self._latest_quote.pop(removed.symbol, None)
                self._heap[worst] = self._heap[-1]
                self._heap.pop()
                if worst < len(self._heap):
                    heapify(self._heap)
                self.dropped_events += 1
            if isinstance(item, QuoteEvent) and item.symbol:
                self._latest_quote[item.symbol] = item.event_id
            heappush(self._heap, (int(item.priority), next(self._sequence), item))
            self._condition.notify()
            return True

    def _pop(self, *, wait: bool) -> MarketEvent | None:
        with self._condition:
            while wait and not self._heap and not self._stop.is_set():
                self._condition.wait(.25)
            while self._heap:
                _, _, item = heappop(self._heap)
                if isinstance(item, QuoteEvent) and item.symbol:
                    if self._latest_quote.get(item.symbol) != item.event_id:
                        continue
                    self._latest_quote.pop(item.symbol, None)
                return item
            return None

    def dispatch_one(self) -> bool:
        item = self._pop(wait=False)
        if item is None:
            return False
        handlers = tuple(self._handlers.get(item.event_type, ())) + tuple(self._handlers.get(None, ()))
        for handler in handlers:
            try:
                handler(item)
            except Exception as exc:  # component failure isolation
                self.failures.append({
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "handler": getattr(handler, "__name__", type(handler).__name__),
                    "error": type(exc).__name__,
                })
        return True

    def drain(self) -> int:
        handled = 0
        while self.dispatch_one():
            handled += 1
        return handled

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("event bus already started")
        self._stop.clear()
        self._thread = Thread(target=self._run, name="shadow-event-bus", daemon=False)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() or self.pending_count:
            item = self._pop(wait=True)
            if item is None:
                continue
            handlers = tuple(self._handlers.get(item.event_type, ())) + tuple(self._handlers.get(None, ()))
            for handler in handlers:
                try:
                    handler(item)
                except Exception as exc:
                    self.failures.append({
                        "event_id": item.event_id,
                        "event_type": item.event_type.value,
                        "handler": getattr(handler, "__name__", type(handler).__name__),
                        "error": type(exc).__name__,
                    })

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("event bus failed to stop cleanly")
        self._thread = None

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._heap)
