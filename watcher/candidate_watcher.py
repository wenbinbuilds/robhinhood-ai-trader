"""Deterministic fast candidate scoring and local shadow-entry transitions.

This module has no model, subprocess, web/news, or broker-order dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config
from agent.candidate_context import CandidateContext, CandidateContextStore
from event_driven.alpha import AlphaCombiner
from event_driven.events import EventType, QuoteEvent
from shadow.execution import ShadowExecutionEngine
from watcher.models import FastQuote, timestamp
from watcher.storage import event


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def dynamic_weights(age_seconds: float) -> tuple[float, float]:
    return AlphaCombiner().weights(age_seconds)


@dataclass(frozen=True)
class LiveScore:
    score: float
    factors: Mapping[str, Mapping[str, Any]]


class LiveMarketScorer:
    """Score current market quality; raw appreciation is only 15% of the score."""

    weights = {
        "spread_quality": 0.25,
        "vwap_state": 0.25,
        "ema9_state": 0.15,
        "support_resistance_location": 0.20,
        "controlled_momentum": 0.15,
    }

    def score(self, context: CandidateContext, quote: FastQuote) -> LiveScore:
        current = quote.mark_price
        midpoint = quote.mid_price
        spread = (
            (quote.ask - quote.bid) / midpoint
            if midpoint is not None and quote.ask is not None and quote.bid is not None
            else None
        )
        spread_quality = (
            _clamp(1.0 - spread / config.MAX_SPREAD_PERCENT)
            if spread is not None and config.MAX_SPREAD_PERCENT > 0 else 0.0
        )
        vwap_state = self._level_score(current, context.research_vwap)
        ema_state = self._level_score(current, context.research_ema9)
        location = self._location_score(
            current,
            context.suggested_stop_reference,
            context.suggested_target_reference,
        )
        change = (
            (current - context.analysis_price) / context.analysis_price
            if current is not None and context.analysis_price > 0 else None
        )
        # Controlled progress is useful; a sharp extension is penalized rather
        # than rewarded, preventing raw-price chasing.
        if change is None:
            momentum = 0.0
        elif -0.01 <= change < 0:
            momentum = 0.55
        elif 0 <= change <= 0.02:
            momentum = 1.0
        elif 0.02 < change <= 0.03:
            momentum = 0.5
        else:
            momentum = 0.0
        values = {
            "spread_quality": (spread_quality, spread),
            "vwap_state": (vwap_state, {"price": current, "vwap": context.research_vwap}),
            "ema9_state": (ema_state, {"price": current, "ema9": context.research_ema9}),
            "support_resistance_location": (location, {"price": current, "support": context.suggested_stop_reference, "target": context.suggested_target_reference}),
            "controlled_momentum": (momentum, change),
        }
        total = sum(self.weights[name] * values[name][0] for name in self.weights)
        return LiveScore(
            round(_clamp(total), 6),
            {
                name: {
                    "value": round(value, 6),
                    "weight": self.weights[name],
                    "observed": observed,
                }
                for name, (value, observed) in values.items()
            },
        )

    @staticmethod
    def _level_score(price: float | None, level: float | None) -> float:
        if price is None or level is None or level <= 0:
            return 0.0
        distance = (price - level) / level
        if distance >= 0:
            return 1.0 if distance <= 0.03 else 0.5 if distance <= 0.05 else 0.0
        return 0.5 if distance >= -0.005 else 0.0

    @staticmethod
    def _location_score(price: float | None, support: float, target: float) -> float:
        if price is None or not 0 < support < target or price <= support or price >= target:
            return 0.0
        progress = (price - support) / (target - support)
        if 0.15 <= progress <= 0.70:
            return 1.0
        if progress < 0.15 or progress <= 0.85:
            return 0.6
        return 0.2


class FastCandidateWatcher:
    """Consumes fresh quotes and may mutate only the local shadow portfolio."""

    def __init__(
        self,
        store: CandidateContextStore,
        engine: ShadowExecutionEngine,
        *,
        events_path: Path,
        score_history_path: Path,
        scorer: LiveMarketScorer | None = None,
        event_orchestrator=None,
        state_store=None,
        clock=None,
    ) -> None:
        if config.MODE != "SHADOW_TRADING":
            raise ValueError("candidate watcher is local SHADOW_TRADING only")
        self.store = store
        self.engine = engine
        self.events_path = Path(events_path)
        self.score_history_path = Path(score_history_path)
        self.scorer = scorer or LiveMarketScorer()
        self.alpha_combiner = AlphaCombiner()
        self.event_orchestrator = event_orchestrator
        self.state_store = state_store or (
            event_orchestrator.state_store if event_orchestrator is not None else None
        )
        if (event_orchestrator is not None
                and self.state_store is not event_orchestrator.state_store):
            raise ValueError("candidate watcher and runtime must share CandidateStateStore")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if event_orchestrator is not None:
            event_orchestrator.bus.subscribe(EventType.QUOTE, self._on_quote_event)
        self.transitions: list[dict[str, Any]] = []

    def _on_quote_event(self, item) -> None:
        if not isinstance(item, QuoteEvent) or not item.symbol:
            return
        with self.store.lock:
            if item.symbol not in self.store.contexts:
                return
        payload = dict(item.payload)
        close = timestamp(payload.get("session_close"))
        quote = FastQuote(
            item.symbol, payload.get("bid"), payload.get("ask"),
            payload.get("last_price"), item.timestamp, item.source,
            payload.get("is_market_open"), close,
        )
        self.process_quotes({item.symbol: quote}, now=self.clock(), publish_quote_event=False)

    def symbols(self, now: datetime) -> list[str]:
        symbols: list[str] = []
        changed = False
        with self.store.lock:
            for context in self.store.contexts.values():
                if context.candidate_state == "REJECTED":
                    continue
                if context.expired_at(now):
                    if context.status != "CONTEXT_EXPIRED":
                        context.status = "CONTEXT_EXPIRED"
                        self._transition_context(context, "EXPIRED", now, "CONTEXT_TTL")
                        context.consecutive_qualifying_updates = 0
                        event(self.events_path, "CONTEXT_EXPIRED", now, symbol=context.symbol)
                        print(f"{context.symbol} CONTEXT EXPIRED", flush=True)
                        if self.event_orchestrator is not None:
                            self.event_orchestrator.expire(context.symbol, now=now)
                        changed = True
                    continue
                if not self.engine.portfolio.has_symbol(context.symbol):
                    symbols.append(context.symbol)
            if changed:
                self.store.save()
        return sorted(symbols)

    def process_quotes(self, quotes: Mapping[str, FastQuote], *, now: datetime,
                       publish_quote_event: bool = True) -> list[dict[str, Any]]:
        transitions: list[dict[str, Any]] = []
        with self.store.lock:
            for symbol in list(self.store.contexts):
                context = self.store.contexts[symbol]
                if context.expired_at(now):
                    continue
                if self.engine.portfolio.has_symbol(symbol):
                    self.store.contexts.pop(symbol, None)
                    continue
                quote = quotes.get(symbol)
                if quote is not None and self.event_orchestrator is not None and publish_quote_event:
                    self.event_orchestrator.quote(quote, cycle_id=context.research_cycle_id)
                blocker = self._hard_blocker(context, quote, now)
                if blocker:
                    context.status = "ENTRY_BLOCKED"
                    permanent = blocker in {
                        "INVALID_STOP", "PRICE_BEYOND_TARGET", "RISK_REWARD",
                    }
                    context.candidate_state = (
                        "REJECTED" if permanent else "INFRASTRUCTURE_BLOCKED"
                    )
                    context.consecutive_qualifying_updates = 0
                    self._record(context, quote, now, blocker=blocker)
                    if self.event_orchestrator is not None:
                        self.event_orchestrator.candidate_blocked(
                            symbol, now=now, reason=blocker, permanent=permanent
                        )
                    continue
                assert isinstance(quote, FastQuote) and quote.mark_price is not None
                if self.event_orchestrator is not None:
                    self.event_orchestrator.candidate_recovered(symbol, now=now)
                live = self.scorer.score(context, quote)
                age = context.age_at(now)
                assert age is not None
                combined = self.alpha_combiner.combine(
                    context.slow_context_score, live.score,
                    context_age_seconds=age,
                )
                slow_weight, live_weight = combined.slow_weight, combined.live_weight
                dynamic = combined.combined_alpha_score
                previous = context.status
                context.live_market_score = live.score
                context.slow_weight = round(slow_weight, 6)
                context.live_weight = round(live_weight, 6)
                context.dynamic_score = round(dynamic, 6)
                context.current_price = quote.mark_price
                context.context_age_seconds = round(age, 3)
                context.last_updated_at = now.astimezone(timezone.utc).isoformat()
                context.metadata["live_factors"] = dict(live.factors)
                if dynamic >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:
                    context.consecutive_qualifying_updates += 1
                    context.status = "PENDING_CONFIRMATION"
                else:
                    context.consecutive_qualifying_updates = 0
                    context.status = "WATCH"
                    context.candidate_state = "SETUP_FORMING"
                threshold_crossing = previous == "WATCH" and context.status == "PENDING_CONFIRMATION"
                score_recorded = self._record(
                    context, quote, now, threshold_crossing=threshold_crossing
                )
                if self.event_orchestrator is not None:
                    self.event_orchestrator.alpha(
                        symbol, now=now, slow=context.slow_context_score,
                        live=live.score, combined=dynamic,
                        slow_weight=slow_weight, live_weight=live_weight,
                        context_age_seconds=age,
                        meaningful=(score_recorded or threshold_crossing
                                    or context.consecutive_qualifying_updates >= config.FAST_ENTRY_CONFIRMATION_UPDATES),
                    )
                if context.consecutive_qualifying_updates < config.FAST_ENTRY_CONFIRMATION_UPDATES:
                    continue
                context.trade_ready_timestamp = now.astimezone(timezone.utc).isoformat()
                self._transition_context(context, "TRADE_READY", now, "CONFIRMED_THRESHOLD_CROSSING")
                if self.event_orchestrator is not None:
                    self.event_orchestrator.trade_ready(symbol, now=now, combined=dynamic)
                opened, detail = self._open(context, quote, now)
                if opened is None:
                    reason = str(detail.get("reason", "RISK_REJECTED"))
                    context.status = "ENTRY_BLOCKED"
                    self._transition_context(context, "REJECTED", now, reason)
                    context.consecutive_qualifying_updates = 0
                    transition = {"symbol": symbol, "from": previous, "to": "ENTRY_BLOCKED", "reason": reason}
                    transitions.append(transition)
                    if self.event_orchestrator is not None:
                        self.event_orchestrator.risk(symbol, now=now, approved=False, reason=reason)
                    event(self.events_path, "SHADOW_ENTRY_BLOCKED", now, symbol=symbol, reason=reason)
                    print(f"{symbol} ENTRY BLOCKED: {reason}", flush=True)
                    continue
                transition = {"symbol": symbol, "from": "WATCH", "to": "TRADE_CANDIDATE", "final": "SHADOW_POSITION", "dynamic_score": dynamic}
                transitions.append(transition)
                if self.event_orchestrator is not None:
                    self.event_orchestrator.risk(symbol, now=now, approved=True)
                    self.event_orchestrator.position_opened(symbol, now=now, trade_id=opened.trade_id)
                event(self.events_path, "WATCH_TO_SHADOW_POSITION", now, symbol=symbol, dynamic_score=round(dynamic, 6))
                print(f"{symbol} LIVE SCORE: {live.score:.3f}\nDYNAMIC SCORE: {dynamic:.3f}\nSTATUS: WATCH → TRADE_CANDIDATE → SHADOW_POSITION", flush=True)
                self.store.contexts.pop(symbol, None)
            self.store.generated_at = now.astimezone(timezone.utc).isoformat()
            self.store.save()
        self.transitions.extend(transitions)
        return transitions

    def _hard_blocker(self, context: CandidateContext, quote: FastQuote | None, now: datetime) -> str | None:
        if context.expired_at(now):
            return "CONTEXT_EXPIRED"
        if quote is None or quote.symbol != context.symbol or quote.timestamp.tzinfo is None:
            return "QUOTE_UNAVAILABLE"
        age = quote.age_at(now)
        if age < 0 or age > config.FAST_QUOTE_MAX_AGE_SECONDS:
            return "QUOTE_STALE"
        if quote.is_market_open is not True:
            return "MARKET_CLOSED"
        current = quote.mark_price
        if current is None:
            return "INVALID_PRICE"
        if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask < quote.bid:
            return "INVALID_SPREAD"
        midpoint = (quote.bid + quote.ask) / 2
        if midpoint <= 0 or (quote.ask - quote.bid) / midpoint > config.MAX_SPREAD_PERCENT:
            return "SPREAD_TOO_WIDE"
        entry = max(current, quote.ask)
        stop, target = context.suggested_stop_reference, context.suggested_target_reference
        if not 0 < stop < entry:
            return "INVALID_STOP"
        if target <= entry:
            return "PRICE_BEYOND_TARGET"
        if (target - entry) / (entry - stop) < config.MIN_RISK_REWARD_RATIO:
            return "RISK_REWARD"
        return None

    def _open(self, context: CandidateContext, quote: FastQuote, now: datetime):
        current = quote.mark_price
        assert current is not None and quote.ask is not None
        entry = max(current, quote.ask)
        current_risk_reward = (
            (context.suggested_target_reference - entry)
            / (entry - context.suggested_stop_reference)
        )
        coordinator = {
            "symbol": context.symbol,
            "decision": "TRADE_CANDIDATE",
            "entry": entry,
            "stop": context.suggested_stop_reference,
            "target": context.suggested_target_reference,
            "risk_reward_ratio": current_risk_reward,
            "confidence": context.coordinator_confidence,
            "combined_score": context.dynamic_score,
            "technical_score": context.technical_context_score,
            "news_score": context.news_score,
            "sector_score": context.sector_score,
            "market_score": context.market_score,
            "setup_name": config.STRATEGY_NAME,
            "thesis": context.llm_thesis,
            "invalidation_condition": context.invalidation_condition,
            "technical_context": {"technical_score": context.technical_context_score},
            "news_context": {"score": context.news_score, "catalyst_type": context.catalyst_classification, "event_clusters": []},
            "sector_context": {"score": context.sector_score, "sector": context.metadata.get("sector", "GENERAL")},
            "market_context": {"score": context.market_score, "regime": context.metadata.get("market_regime", "UNKNOWN")},
        }
        market_data = {
            "current_price": current,
            "bid": quote.bid,
            "ask": quote.ask,
            "quote_as_of": quote.timestamp.astimezone(timezone.utc).isoformat(),
        }
        opened, detail = self.engine.open_candidate(coordinator, market_data, now=now)
        if opened is not None:
            opened.slow_context_score = context.slow_context_score
            opened.live_market_score = context.live_market_score
            opened.dynamic_score = context.dynamic_score
            opened.slow_weight = context.slow_weight
            opened.live_weight = context.live_weight
            opened.context_age_seconds = context.context_age_seconds
            opened.qualitative_score = context.qualitative_score
            opened.llm_score = context.qualitative_score
            opened.discovery_timestamp = context.discovery_timestamp
            opened.watchlist_timestamp = context.watchlist_timestamp
            opened.trade_ready_timestamp = context.trade_ready_timestamp
            opened.state_transition_history = list(context.state_transition_history) + [
                {
                    "timestamp": now.astimezone(timezone.utc).isoformat(),
                    "previous_state": "TRADE_READY",
                    "new_state": "RISK_APPROVED",
                    "reason": "DETERMINISTIC_RISK_APPROVED",
                },
                {
                    "timestamp": now.astimezone(timezone.utc).isoformat(),
                    "previous_state": "RISK_APPROVED",
                    "new_state": "POSITION_OPEN",
                    "reason": "SHADOW_ENTRY_FILLED",
                },
            ]
            self.engine.portfolio.save(now)
        return opened, detail

    def _record(self, context: CandidateContext, quote: FastQuote | None, now: datetime, *, blocker=None, threshold_crossing=False) -> bool:
        last = None
        if context.last_history_at:
            try:
                last = datetime.fromisoformat(context.last_history_at.replace("Z", "+00:00"))
            except ValueError:
                pass
        sampled = last is None or (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() >= config.SCORE_HISTORY_SAMPLE_SECONDS
        if not (sampled or threshold_crossing):
            return False
        event(
            self.score_history_path,
            "CANDIDATE_SCORE",
            now,
            symbol=context.symbol,
            slow_score=context.slow_context_score,
            live_score=context.live_market_score,
            slow_weight=context.slow_weight,
            live_weight=context.live_weight,
            dynamic_score=context.dynamic_score,
            price=(quote.mark_price if quote else None),
            status=context.status,
            blocker=blocker,
        )
        context.last_history_at = now.astimezone(timezone.utc).isoformat()
        return True

    @staticmethod
    def _transition_context(context: CandidateContext, new_state: str,
                            now: datetime, reason: str) -> None:
        previous = context.candidate_state
        if previous == new_state:
            return
        context.candidate_state = new_state
        context.state_transition_history.append({
            "timestamp": now.astimezone(timezone.utc).isoformat(),
            "previous_state": previous,
            "new_state": new_state,
            "reason": reason,
        })
