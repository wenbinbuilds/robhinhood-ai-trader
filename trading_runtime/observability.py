"""Concise, decision-neutral terminal observability for shadow trading.

This module only projects state that the strategies have already produced.  It
must never recompute a score, gate, order size, or execution decision.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import config
from execution.execution_guard import read_kill_switch
from strategies.identity import is_scalp_strategy
from strategies.scalp.diagnostics import STAGE_NAMES
from watcher.models import timestamp


REASON_TEXT = {
    "ENTRY_OVEREXTENDED": "price already moved too far to chase",
    "SIGNAL_SCORE_BELOW_THRESHOLD": "signal score below 0.70",
    "STALE_QUOTE": "quote is too old for scalp entry",
    "QUOTE_UNAVAILABLE": "no usable quote",
    "SPREAD_TOO_WIDE": "bid/ask spread is too wide",
    "VOLUME_EXPANSION_BELOW_MINIMUM": "volume expansion below 1.20x",
    "VOLUME_DATA_STALE": "volume input is stale",
    "VOLUME_DATA_UNAVAILABLE": "volume input is unavailable",
    "MICRO_BARS_STALE": "completed micro bars are stale",
    "MICRO_BARS_UNAVAILABLE": "completed micro bars are unavailable",
    "MICRO_BAR_REFRESH_FAILED": "micro-bar refresh failed",
    "INSUFFICIENT_COMPLETED_MICRO_BARS": "not enough completed micro bars",
    "UNCLASSIFIED_SETUP": "no supported setup is classified",
    "STALE_SCALP_EPISODE": "setup episode is closed or stale",
    "EXISTING_POSITION_OTHER_STRATEGY": "another strategy already holds the symbol",
    "DUPLICATE_POSITION": "symbol already has an open position",
    "INVALID_STOP": "no valid structural stop",
    "INSUFFICIENT_NET_EDGE": "expected move does not cover costs and minimum edge",
    "INVALID_TARGET": "no valid target",
    "SCALP_RR_BELOW_MINIMUM": "risk/reward is below the minimum",
    "MARKET_CLOSED": "regular market session is closed",
    "OVERDUE_EXIT_PENDING": "an overdue scalp exit must resolve first",
    "CONTEXT_EXPIRED": "research context expired",
    "PRICE_BEYOND_TARGET": "price is already beyond the researched target",
    "RISK_REWARD": "current risk/reward is no longer valid",
}


def human_reason(reason: str | None) -> str:
    if not reason or reason == "NONE":
        return "no blocking reason"
    return REASON_TEXT.get(reason, reason.replace("_", " ").lower())


def primary_reason(reasons: Sequence[str] | None) -> str:
    return next((reason for reason in (reasons or ()) if reason), "NONE")


def depth_aware_bottleneck(
    traces: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the deepest actually reached block, not the largest early filter."""

    stage_index = {name: index for index, name in enumerate(STAGE_NAMES)}
    reached_eligible = [
        row for row in traces
        if row.get("stage_flags", {}).get("eligible_micro_signals")
    ]
    if reached_eligible:
        blocked = [
            row for row in reached_eligible
            if row.get("blocking_stage") in stage_index
        ]
        if blocked:
            deepest = max(
                stage_index[row["blocking_stage"]] for row in blocked
            )
            rows = [
                row for row in blocked
                if stage_index[row["blocking_stage"]] == deepest
            ]
            counts = Counter(
                primary_reason(row.get("blocking_reasons")) for row in rows
            )
            reason, count = min(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
            return {
                "scope": "ELIGIBLE_CANDIDATE",
                "stage": rows[0]["blocking_stage"],
                "reason": reason,
                "count": count,
                "message": human_reason(reason),
            }
        return {
            "scope": "ELIGIBLE_CANDIDATE",
            "stage": "entries",
            "reason": "NONE",
            "count": len(reached_eligible),
            "message": "eligible candidates reached the end of the recorded funnel",
        }

    filters = dict(diagnostics.get("filtered_reasons", {}))
    if filters:
        reason, count = min(filters.items(), key=lambda item: (-item[1], item[0]))
        return {
            "scope": "UNIVERSE_FILTER",
            "stage": "before_eligibility",
            "reason": reason,
            "count": count,
            "message": "no eligible setup reached signal scoring; " + human_reason(reason),
        }
    return {
        "scope": "UNIVERSE_FILTER",
        "stage": "before_eligibility",
        "reason": "NONE",
        "count": 0,
        "message": "no eligible setup reached signal scoring",
    }


def scalp_candidate_projection(trace: Mapping[str, Any]) -> dict[str, Any]:
    flags = trace.get("stage_flags", {})
    lifecycle = trace.get("episode_lifecycle", {})
    reason = primary_reason(trace.get("blocking_reasons") or trace.get("rejection_reasons"))
    if flags.get("entries"):
        state = "ENTERED"
    elif reason == "STALE_SCALP_EPISODE":
        state = lifecycle.get("episode_status") or "DUPLICATE_PROTECTED"
    elif flags.get("signal_score_pass"):
        state = "BLOCKED"
    elif flags.get("eligible_micro_signals"):
        state = "WATCHING"
    else:
        state = "FILTERED"
    return {
        "symbol": trace.get("symbol"),
        "setup": trace.get("setup_type", "UNCLASSIFIED"),
        "score": trace.get("signal", {}).get("score"),
        "threshold": trace.get("signal", {}).get("minimum", config.SCALP_MIN_SIGNAL_SCORE),
        "quote_age": trace.get("freshness", {}).get("quote_age_seconds"),
        "spread": trace.get("spread", {}).get("observed_pct"),
        "volume_expansion": trace.get("volume_expansion", {}).get("observed"),
        "state": state,
        "reason": reason,
        "reason_text": human_reason(reason),
        "blocking_stage": trace.get("blocking_stage"),
        "episode_id": trace.get("episode_id"),
        "episode_status": lifecycle.get("episode_status"),
        "close_reason": lifecycle.get("close_reason"),
        "exact_block_reason": lifecycle.get("exact_block_reason"),
        "episode_age": lifecycle.get("episode_age_seconds"),
    }


def _fmt(value: Any, digits: int = 2, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)):
        return "UNAVAILABLE"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "UNAVAILABLE"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _today(now: datetime) -> str:
    return now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()


def _on_date(value: str | None, day: str) -> bool:
    at = timestamp(value)
    return bool(at and at.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat() == day)


@dataclass
class RuntimeDashboard:
    """Print a dashboard only when decision-relevant state changes."""

    portfolio: Any
    candidate_store: Any = None
    kill_switch_path: Any = config.LIVE_KILL_SWITCH_PATH
    output: Any = print

    def __post_init__(self) -> None:
        self._fingerprint: tuple[Any, ...] | None = None

    def update(
        self,
        *,
        now: datetime,
        provider: str,
        provider_mode: str,
        watcher_status: str,
        quotes: Mapping[str, Any],
        scalp_result: Mapping[str, Any] | None,
        scalp_v2_result: Mapping[str, Any] | None = None,
    ) -> bool:
        view = self.project(
            now=now, provider=provider, provider_mode=provider_mode,
            watcher_status=watcher_status, quotes=quotes,
            scalp_result=scalp_result, scalp_v2_result=scalp_v2_result,
        )
        fingerprint = self.semantic_fingerprint(view)
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        self.output(self.render(view), flush=True)
        return True

    def project(
        self,
        *,
        now: datetime,
        provider: str,
        provider_mode: str,
        watcher_status: str,
        quotes: Mapping[str, Any],
        scalp_result: Mapping[str, Any] | None,
        scalp_v2_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.portfolio.snapshot()
        day = _today(now)
        kill = read_kill_switch(self.kill_switch_path)
        positions = list(state.open_positions)
        closed_today = [row for row in state.closed_positions if _on_date(row.exit_timestamp, day)]
        position_open = [p for p in positions if not is_scalp_strategy(p.strategy_id or p.strategy)]
        scalp_open = [p for p in positions if is_scalp_strategy(p.strategy_id or p.strategy)]
        position_closed = [p for p in closed_today if not is_scalp_strategy(p.strategy_id or p.strategy)]
        scalp_closed = [p for p in closed_today if is_scalp_strategy(p.strategy_id or p.strategy)]
        stored_contexts = self.candidate_store.snapshot() if self.candidate_store is not None else []
        contexts = [
            item for item in stored_contexts
            if item.candidate_state not in {"REJECTED", "EXPIRED"}
            and not item.expired_at(now)
        ]
        position_candidates = []
        for item in sorted(
            contexts,
            key=lambda row: (row.dynamic_score is not None, row.dynamic_score or row.slow_context_score),
            reverse=True,
        )[:3]:
            score = item.dynamic_score if item.dynamic_score is not None else item.slow_context_score
            if item.true_hard_gate_failures:
                reason = item.true_hard_gate_failures[0]
            elif item.status == "ENTRY_BLOCKED":
                reason = str(item.metadata.get("position_blocker", {}).get("reason") or "ENTRY_BLOCKED")
            elif score is None or score < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:
                reason = f"NEEDS_SCORE_{config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:.2f}"
            elif score < config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:
                reason = f"NEEDS_SCORE_{config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:.2f}"
            else:
                reason = "NEEDS_CONFIRMATION"
            position_candidates.append({
                "symbol": item.symbol, "score": score,
                "state": item.candidate_state, "reason": reason,
                "reason_text": human_reason(reason),
            })

        diagnostics = dict((scalp_result or {}).get("diagnostics", {}))
        traces = list((scalp_result or {}).get("traces", []))
        projected_scalp = [scalp_candidate_projection(row) for row in traces]
        stage_index = {name: index for index, name in enumerate(STAGE_NAMES)}
        eligible_scalp_candidates = sorted(
            (row for row, trace in zip(projected_scalp, traces)
             if trace.get("stage_flags", {}).get("eligible_micro_signals")),
            key=lambda row: (
                stage_index.get(row.get("blocking_stage"), -1),
                row["score"] is not None, row["score"] or -1,
            ),
            reverse=True,
        )[:3]
        filtered_scalp_candidates = sorted(
            (row for row, trace in zip(projected_scalp, traces)
             if not trace.get("stage_flags", {}).get("eligible_micro_signals")),
            key=lambda row: (row["score"] is not None, row["score"] or -1),
            reverse=True,
        )[:3]
        scalp_candidates = eligible_scalp_candidates or filtered_scalp_candidates
        funnel = dict(diagnostics.get("funnel", {}))
        bottleneck = depth_aware_bottleneck(traces, diagnostics)
        v2 = dict(scalp_v2_result or {})
        market_values = [q.is_market_open for q in quotes.values() if q is not None]
        market = "OPEN" if any(value is True for value in market_values) else (
            "CLOSED" if market_values and all(value is False for value in market_values) else "UNKNOWN"
        )
        connected = watcher_status not in {"FAST_WATCHER_UNAVAILABLE", "NOT_STARTED"}
        recent_trades = sorted(
            state.closed_positions,
            key=lambda row: timestamp(row.exit_timestamp) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:5]
        return {
            "now": now.astimezone(timezone.utc).isoformat(),
            "market": market,
            "robinhood": f"{'CONNECTED' if connected else 'UNAVAILABLE'} — {provider} ({provider_mode})",
            "safety": (
                "SHADOW ONLY — LIVE EXECUTION BLOCKED"
                if (not config.LIVE_TRADING_ENABLED and not config.ROBINHOOD_EXECUTION_ENABLED
                    and kill.trading_blocked)
                else "SAFETY CONFIGURATION INVALID"
            ),
            "equity": state.equity, "cash": state.cash,
            "open_positions": len(positions),
            "realized": sum(p.net_pnl for p in closed_today),
            "unrealized": state.unrealized_pnl,
            "position": {
                "watching": len(contexts),
                "trade_ready": sum(c.candidate_state in {"TRADE_READY", "RISK_APPROVED"} for c in contexts),
                "open": position_open,
                "entries_today": (
                    sum(_on_date(p.entry_timestamp, day) for p in position_open)
                    + sum(
                        _on_date(p.entry_timestamp, day)
                        for p in state.closed_positions
                        if not is_scalp_strategy(p.strategy_id or p.strategy)
                    )
                ),
                "exits_today": len(position_closed),
                "realized": sum(p.net_pnl for p in position_closed),
                "unrealized": sum(p.unrealized_pnl for p in position_open),
                "candidates": position_candidates,
                "bottleneck": (
                    {"reason": "NONE", "message": f"{len(position_open)} position currently open"}
                    if position_open else
                    {"reason": position_candidates[0]["reason"],
                     "message": position_candidates[0]["reason_text"]}
                    if position_candidates else
                    {"reason": "NONE", "message": "no active position candidate"}
                ),
            },
            "scalp": {
                "universe": funnel.get("universe_observations", 0),
                "fresh": funnel.get("quote_fresh", 0),
                "classified": funnel.get("micro_signals_detected_anywhere", 0),
                "eligible": funnel.get("eligible_micro_signals", 0),
                "signal_pass": funnel.get("signal_score_pass", 0),
                "open": scalp_open,
                "entries_today": (
                    sum(_on_date(p.entry_timestamp, day) for p in scalp_open)
                    + sum(
                        _on_date(p.entry_timestamp, day)
                        for p in state.closed_positions
                        if is_scalp_strategy(p.strategy_id or p.strategy)
                    )
                ),
                "exits_today": len(scalp_closed),
                "realized": sum(p.net_pnl for p in scalp_closed),
                "unrealized": sum(p.unrealized_pnl for p in scalp_open),
                "candidates": scalp_candidates,
                "eligible_candidates": eligible_scalp_candidates,
                "filtered_candidates": filtered_scalp_candidates,
                "bottleneck": bottleneck,
            },
            "scalp_v2": {
                "research_only": True,
                "armed": int(v2.get("armed", 0) or 0),
                "fast_trigger": int(v2.get("fast_triggers", 0) or 0),
                "entry_ready": int(v2.get("entry_ready", 0) or 0),
                "open": int(v2.get("open_research_positions", 0) or 0),
                "closed": int(v2.get("closed_research_trades", 0) or 0),
                "realized": float(v2.get("research_realized_pnl", 0) or 0),
                "unrealized": float(v2.get("research_unrealized_pnl", 0) or 0),
            },
            "recent_trades": recent_trades,
        }

    @staticmethod
    def semantic_fingerprint(view: Mapping[str, Any]) -> tuple[Any, ...]:
        def position_key(p):
            return (p.symbol, round(p.last_price or 0, 2), round(p.unrealized_pnl, 2), p.monitoring_status)
        def candidate_key(c):
            return (c.get("symbol"), None if c.get("score") is None else round(c["score"], 3),
                    c.get("state"), c.get("reason"))
        def trade_key(row):
            return (row.strategy_display_name, row.symbol, row.exit_timestamp,
                    row.exit_reason, round(row.net_pnl, 2))
        pos, scalp, scalp_v2 = view["position"], view["scalp"], view["scalp_v2"]
        return (
            view["market"], view["robinhood"], view["safety"],
            round(view["equity"], 2), round(view["cash"], 2),
            tuple(position_key(p) for p in pos["open"]),
            tuple(candidate_key(c) for c in pos["candidates"]),
            pos["bottleneck"]["reason"], pos["bottleneck"]["message"],
            pos["watching"], pos["trade_ready"], pos["entries_today"], pos["exits_today"],
            scalp["universe"], scalp["fresh"], scalp["classified"], scalp["eligible"], scalp["signal_pass"],
            tuple(position_key(p) for p in scalp["open"]),
            tuple(candidate_key(c) for c in scalp["candidates"]),
            tuple(candidate_key(c) for c in scalp["eligible_candidates"]),
            tuple(candidate_key(c) for c in scalp["filtered_candidates"]),
            tuple(scalp["bottleneck"].get(k) for k in ("scope", "stage", "reason", "count")),
            scalp["entries_today"], scalp["exits_today"],
            tuple(scalp_v2.get(key) for key in (
                "armed", "fast_trigger", "entry_ready", "open", "closed",
                "realized", "unrealized",
            )),
            tuple(trade_key(row) for row in view["recent_trades"]),
        )

    @staticmethod
    def render(view: Mapping[str, Any]) -> str:
        p, s, v2 = view["position"], view["scalp"], view["scalp_v2"]
        lines = [
            "\nTRADER — SHADOW MODE",
            f"Time: {view['now']}",
            f"Market: {view['market']}",
            f"Robinhood: {view['robinhood']}",
            f"Safety: {view['safety']}",
            f"Equity: ${_fmt(view['equity'])}  Cash: ${_fmt(view['cash'])}  Open positions: {view['open_positions']}",
            f"Today P&L: realized=${_fmt(view['realized'], signed=True)} unrealized=${_fmt(view['unrealized'], signed=True)}",
            "",
            "POSITION",
            f"watching={p['watching']} trade-ready={p['trade_ready']} open={len(p['open'])} "
            f"entries={p['entries_today']} exits={p['exits_today']} "
            f"realized=${_fmt(p['realized'], signed=True)} unrealized=${_fmt(p['unrealized'], signed=True)}",
        ]
        for row in p["open"]:
            entered = timestamp(row.entry_timestamp)
            now = timestamp(view["now"])
            held = (now - entered).total_seconds() if entered and now else None
            pct = ((row.last_price / row.entry_price - 1) * 100
                   if row.last_price is not None and row.entry_price else None)
            stop_distance = (
                row.last_price - row.stop
                if row.last_price is not None and row.stop is not None else None
            )
            target_distance = (
                row.target - row.last_price
                if row.last_price is not None and row.target is not None else None
            )
            lines.append(
                f"  {row.symbol} OPEN entry=${_fmt(row.entry_price)} current=${_fmt(row.last_price)} "
                f"stop=${_fmt(row.stop)} target=${_fmt(row.target)} score={_fmt(row.dynamic_score, 3)} "
                f"pnl=${_fmt(row.unrealized_pnl, signed=True)} ({_fmt(pct, signed=True)}%) "
                f"hold={_duration(held)} distance-to-stop=${_fmt(stop_distance)} "
                f"distance-to-target=${_fmt(target_distance)} exit=HOLD "
                f"monitoring={row.monitoring_status}"
            )
        if not p["open"]:
            lines.append("  open positions: NONE")
        lines.append("  top candidates:")
        for row in p["candidates"]:
            lines.append(
                f"    {row['symbol']} score={_fmt(row['score'], 3)} "
                f"watch={config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:.2f} "
                f"trade={config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:.2f} "
                f"state={row['state']} next={row['reason']} ({row['reason_text']})"
            )
        if not p["candidates"]:
            lines.append("    NONE")
        pb = p["bottleneck"]
        lines.append(
            f"  bottleneck: {pb['reason']} — {pb['message']}"
        )

        lines.extend([
            "",
            "SCALP",
            f"universe={s['universe']} fresh={s['fresh']} classified={s['classified']} "
            f"eligible={s['eligible']} signal-passes={s['signal_pass']} open={len(s['open'])} "
            f"entries={s['entries_today']} exits={s['exits_today']} "
            f"realized=${_fmt(s['realized'], signed=True)} unrealized=${_fmt(s['unrealized'], signed=True)}",
            "  ELIGIBLE / DEEPEST CANDIDATES:",
        ])
        for row in s["eligible_candidates"]:
            lines.append(
                f"    {row['symbol']} {row['setup']} score={_fmt(row['score'], 3)}/"
                f"{row['threshold']:.2f} quote-age={_fmt(row['quote_age'], 1)}s "
                f"spread={_fmt((row['spread'] * 100) if row['spread'] is not None else None, 3)}% "
                f"volume={_fmt(row['volume_expansion'], 2)}x state={row['state']} "
                f"block={row['reason']} ({row['reason_text']})"
            )
            if row.get('episode_id'):
                lines.append(
                    f"      episode={row['episode_id']} status={row.get('episode_status') or row['state']} "
                    f"close={row.get('close_reason') or 'NONE'} "
                    f"block-detail={row.get('exact_block_reason') or row['reason']}"
                )
        if not s["eligible_candidates"]:
            lines.append("    NONE")
        lines.append("  TOP FILTERED CANDIDATES:")
        for row in s["filtered_candidates"]:
            lines.append(
                f"    {row['symbol']} {row['setup']} score={_fmt(row['score'], 3)}/"
                f"{row['threshold']:.2f} quote-age={_fmt(row['quote_age'], 1)}s "
                f"volume={_fmt(row['volume_expansion'], 2)}x "
                f"reason={row['reason']} ({row['reason_text']})"
            )
        if not s["filtered_candidates"]:
            lines.append("    NONE")
        b = s["bottleneck"]
        lines.append(
            f"  bottleneck: scope={b['scope']} stage={b['stage']} "
            f"reason={b['reason']} count={b['count']} — {b['message']}"
        )
        lines.extend([
            "",
            "SCALP V1 vs V2 — RESEARCH ONLY",
            f"  V1 eligible={s['eligible']} signal-pass={s['signal_pass']} "
            f"entry={s['entries_today']}",
            f"  V2 armed={v2['armed']} fast-trigger={v2['fast_trigger']} "
            f"entry-ready={v2['entry_ready']} open-research={v2['open']} "
            f"closed-research={v2['closed']}",
            f"  V1 canonical realized=${_fmt(s['realized'], signed=True)} "
            f"V2 research realized=${_fmt(v2['realized'], signed=True)} "
            f"unrealized=${_fmt(v2['unrealized'], signed=True)}",
        ])
        lines.extend(["", "RECENT TRADES"])
        for trade in view["recent_trades"]:
            held = getattr(trade, 'holding_time_seconds', 0) or (
                getattr(trade, 'holding_time_minutes', 0) * 60
            )
            cost = trade.gross_pnl - trade.net_pnl
            lines.append(
                f"  {trade.strategy_display_name} {trade.symbol} "
                f"${_fmt(trade.entry_price)} → ${_fmt(trade.exit_price)} "
                f"hold={_duration(held)} exit={trade.exit_reason} "
                f"gross=${_fmt(trade.gross_pnl, signed=True)} "
                f"cost=${_fmt(cost)} net=${_fmt(trade.net_pnl, signed=True)}"
            )
        if not view["recent_trades"]:
            lines.append("  NONE")
        return "\n".join(lines)


def render_scalp_drilldown(trace: Mapping[str, Any]) -> str:
    """Render one persisted candidate observation without altering runtime state."""

    f, s, g = trace.get("freshness", {}), trace.get("signal", {}), trace.get("geometry", {})
    micro, life = trace.get("micro_bars", {}), trace.get("episode_lifecycle", {})
    lines = [
        f"SCALP DEBUG — {trace.get('symbol')}",
        f"timestamp={trace.get('timestamp')} episode={trace.get('episode_id')}",
        f"setup={trace.get('setup_type')} evidence={','.join(trace.get('setup_evidence', [])) or 'NONE'}",
        f"quote_status={f.get('quote_status')} quote_age={f.get('quote_age_seconds')} spread={trace.get('spread', {}).get('observed_pct')}",
        f"completed_bar={micro.get('latest_completed_bar_timestamp')} bar_age={micro.get('bar_age_seconds')} provider={micro.get('provider_status')}",
        f"episode_created={life.get('episode_created_at')} episode_age={life.get('episode_age_seconds')} status={life.get('episode_status')}",
        f"episode_closed={life.get('episode_closed_at')} close_reason={life.get('close_reason')} exact_block={life.get('exact_block_reason')}",
        f"fingerprint_current={life.get('current_structural_fingerprint')}",
        f"fingerprint_original={life.get('initial_structural_fingerprint')}",
        f"fingerprint_fields={life.get('fingerprint_fields')}",
        f"structure_changed={life.get('structure_changed')} new_episode_allowed={life.get('new_episode_allowed')}",
        f"quote_price={life.get('current_quote_price')} anchor_original={life.get('original_anchor_price')}",
        f"bar_current={life.get('current_short_bar_timestamp')} bar_original={life.get('original_bar_timestamp')}",
        f"volume_current={life.get('current_volume_expansion')} volume_original={life.get('original_volume_expansion')}",
        f"score_before_penalties={s.get('score_before_penalties')} penalties={s.get('total_penalties')} final_score={s.get('score')} threshold={s.get('minimum')}",
    ]
    for name, row in s.get("components", {}).items():
        lines.append(
            f"  {name}: raw={row.get('raw')} normalized={row.get('normalized')} "
            f"weight={row.get('weight')} contribution={row.get('contribution')} clamp={row.get('clamp')}"
        )
    extension_reference = g.get('entry_extension_reference') or {
        'production_reference_type': 'EMA9',
        'production_reference_price': g.get('structural_levels', {}).get('ema9'),
        'formula': 'candidate_mark_price / ema9 - 1',
        'setup_specific': False,
    }
    lines.extend([
        f"entry={g.get('entry')} stop={g.get('stop')} target={g.get('target')} "
        f"extension={g.get('entry_extension_pct')} extension_threshold="
        f"{g.get('entry_extension_threshold_pct', config.SCALP_MAX_EXTENSION_PCT)}",
        "extension_reference=" + str(extension_reference),
        f"risk_pct={g.get('risk_pct')} gross_rr={g.get('gross_rr')} net_rr={g.get('net_rr')}",
        f"final={trace.get('final')} blocking_stage={trace.get('blocking_stage')} reasons={','.join(trace.get('rejection_reasons', [])) or 'NONE'}",
    ])
    return "\n".join(lines)
