"""Read-only, evidence-bounded diagnostics for a shadow trading session.

The auditor intentionally consumes only local JSON/JSONL artifacts.  It never
refreshes market data, invokes an LLM, or calls a broker operation.  Missing
telemetry is reported as unavailable instead of being inferred as success.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import config


SLOW_BUCKETS = (
    ("<0.50", None, 0.50),
    ("0.50-0.55", 0.50, 0.55),
    ("0.55-0.60", 0.55, 0.60),
    ("0.60-0.65", 0.60, 0.65),
    ("0.65-0.70", 0.65, 0.70),
    ("0.70+", 0.70, None),
)

DYNAMIC_BUCKETS = (
    ("<0.65", None, 0.65),
    ("0.65-0.68", 0.65, 0.68),
    ("0.68-0.70", 0.68, 0.70),
    ("0.70-0.72", 0.70, 0.72),
    ("0.72-0.75", 0.72, 0.75),
    ("0.75-0.80", 0.75, 0.80),
    ("0.80+", 0.80, None),
)

GEOMETRY_GATES = (
    "MINIMUM_RISK_REWARD",
    "MINIMUM_STOP_DISTANCE",
    "RESISTANCE_ABOVE_ENTRY",
    "INVALID_STOP",
    "SPREAD",
)

INFRASTRUCTURE_REASONS = {
    "QUOTE_UNAVAILABLE", "QUOTE_STALE", "FAST_QUOTE_STALE",
    "PRE_EXECUTION_REFRESH_UNAVAILABLE", "REFRESHED_QUOTE_STALE",
    "REFRESHED_EXCHANGE_TIMESTAMP_MISSING_OR_INVALID",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _session_date(value: Any) -> str | None:
    at = _timestamp(value)
    if at is None:
        return None
    return at.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        stream = path.open(encoding="utf-8")
    except OSError:
        return rows
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 6)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 6)


def _distribution(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [item for value in values if (item := _number(value)) is not None]
    return {
        "count": len(numbers),
        "mean": round(mean(numbers), 6) if numbers else None,
        "median": round(median(numbers), 6) if numbers else None,
        "p25": _percentile(numbers, 0.25),
        "p75": _percentile(numbers, 0.75),
        "minimum": round(min(numbers), 6) if numbers else None,
        "maximum": round(max(numbers), 6) if numbers else None,
    }


def _bucket(value: float | None, definitions: Sequence[tuple[str, float | None, float | None]]) -> str | None:
    if value is None:
        return None
    for label, low, high in definitions:
        if (low is None or value >= low) and (high is None or value < high):
            return label
    return None


def rejection_class(reason: str) -> str:
    """Classify a reason without weakening or bypassing the underlying gate."""

    value = str(reason or "UNKNOWN").upper()
    if value.startswith("SLOW_SCORE_BELOW") or "BELOW_0.60" in value:
        return "SCORE_BELOW_THRESHOLD"
    if any(token in value for token in INFRASTRUCTURE_REASONS) or value.startswith(
        ("PRE_EXECUTION_REFRESH_FAILED", "FAST_PROVIDER_ERROR", "INFRASTRUCTURE")
    ):
        return "INFRASTRUCTURE_FAILURE"
    if value in {"MARKET_CLOSED", "CONTEXT_EXPIRED"}:
        return "TEMPORARY_BLOCK"
    if value in {
        "PRICE_VS_VWAP", "EMA_STRUCTURE", "RSI", "MACD", "RELATIVE_VOLUME",
        "CANDLE_STRUCTURE", "MARKET_DIRECTION", "MINIMUM_STRATEGY_SCORE",
        "MAXIMUM_CONFLICTS", "MINIMUM_CONFIDENCE",
    }:
        return "SIGNAL_QUALITY_WARNING"
    return "TERMINAL_HARD_REJECTION"


def reconsideration_class(reason: str) -> str:
    value = str(reason or "UNKNOWN").upper()
    if rejection_class(value) in {"INFRASTRUCTURE_FAILURE", "TEMPORARY_BLOCK"}:
        return "TEMPORARY"
    if any(token in value for token in (
        "RISK_REWARD", "STOP", "RESISTANCE", "SPREAD", "REQUIRED_FIELDS",
        "MINIMUM_CANDLES", "QUOTE_FRESHNESS", "PRICE_RANGE",
    )):
        return "REEVALUATABLE_NEXT_SLOW_CYCLE"
    return "PERMANENT"


class ShadowSessionAudit:
    """Aggregate a single market-date session from local runtime evidence."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir)
        self.logs_dir = self.project_dir / "logs"
        self.state_dir = self.project_dir / "state"

    def _event_rows(self) -> list[dict[str, Any]]:
        paths = sorted(self.logs_dir.glob("market_events.jsonl*"))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in paths:
            for row in _read_jsonl(path):
                identity = str(row.get("event_id") or "")
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                rows.append(row)
        return rows

    def available_sessions(self) -> list[str]:
        dates = {
            path.stem for path in self.logs_dir.glob("????-??-??.jsonl")
        }
        for name in (
            "candidate_score_history.jsonl", "fast_candidate_watcher.jsonl",
            "fast_watcher.jsonl", "market_events.jsonl", "market_events.jsonl.1",
        ):
            for row in _read_jsonl(self.logs_dir / name):
                date = _session_date(row.get("timestamp"))
                if date:
                    dates.add(date)
        return sorted(dates)

    def summarize(self, session_date: str | None = None) -> dict[str, Any]:
        sessions = self.available_sessions()
        selected = session_date or (sessions[-1] if sessions else datetime.now(
            ZoneInfo(config.MARKET_TIMEZONE)
        ).date().isoformat())
        cycle_rows = [
            row for row in _read_jsonl(self.logs_dir / f"{selected}.jsonl")
            if isinstance(row.get("scanner_candidates"), list)
        ]
        events = [
            row for row in self._event_rows()
            if _session_date(row.get("timestamp")) == selected
        ]
        score_rows = [
            row for row in _read_jsonl(self.logs_dir / "candidate_score_history.jsonl")
            if _session_date(row.get("timestamp")) == selected
        ]
        candidate_events = [
            row for row in _read_jsonl(self.logs_dir / "fast_candidate_watcher.jsonl")
            if _session_date(row.get("timestamp")) == selected
        ]
        watcher_events = [
            row for row in _read_jsonl(self.logs_dir / "fast_watcher.jsonl")
            if _session_date(row.get("timestamp")) == selected
        ]
        analyzed = [
            (index, row)
            for index, cycle in enumerate(cycle_rows)
            for row in cycle.get("analyzed_candidates", [])
            if isinstance(row, Mapping)
        ]
        scanner = [
            row for cycle in cycle_rows
            for row in cycle.get("scanner_candidates", [])
            if isinstance(row, Mapping)
        ]
        analysis = self._analyze_candidates(analyzed, score_rows)
        self._merge_runtime_rejections(
            analysis["rejections"], score_rows, candidate_events, watcher_events
        )
        self._merge_runtime_hard_gates(analysis["hard_gates"], score_rows)
        opportunities = self._fast_opportunities(score_rows, candidate_events, events)
        funnel = self._funnel(
            scanner, analyzed, analysis, opportunities, candidate_events, events, selected
        )
        timestamps = [
            at for row in [*cycle_rows, *events, *score_rows, *candidate_events]
            if (at := _timestamp(row.get("timestamp") or row.get("analysis_started_at")))
        ]
        observed_hours = (
            max((max(timestamps) - min(timestamps)).total_seconds() / 3600, 1 / 60)
            if len(timestamps) >= 2 else None
        )
        quote_reliability = self._quote_reliability(watcher_events, score_rows, selected)
        result = {
            "session_date": selected,
            "entry_geometry": self._entry_geometry(candidate_events, cycle_rows),
            "evidence": {
                "slow_cycles": len(cycle_rows),
                "observed_hours": round(observed_hours, 3) if observed_hours else None,
                "forward_outcome_note": (
                    "Outcomes use sampled fast-watch prices, not full tick/bar history; "
                    "missing values are not treated as misses."
                ),
                "funnel_unit_note": (
                    "Scanner, slow, and admission stages are candidate-cycle observations. "
                    "Legacy fast-score rows lack context IDs, so their stages are unique-symbol "
                    "counts; new rows include research_cycle_id for episode-level reporting."
                ),
            },
            "funnel": funnel,
            "rejections": analysis["rejections"],
            "hard_gates": analysis["hard_gates"],
            "geometry_examples": analysis["geometry_examples"],
            "geometry_diagnostics": self._geometry_diagnostics(
                analysis["geometry_examples"]
            ),
            "slow_score_buckets": analysis["slow_score_buckets"],
            "dynamic_score_buckets": self._dynamic_buckets(score_rows, analyzed),
            "counterfactuals": self._counterfactuals(analyzed, score_rows, candidate_events),
            "fast_watch_opportunities": opportunities,
            "quote_reliability": quote_reliability,
            "terminal_rejections": self._terminal_rejections(analysis, candidate_events),
            "scanner_coverage": self._scanner_coverage(cycle_rows, scanner, analyzed),
            "position_and_risk_blocks": self._position_blocks(candidate_events, events),
            "scores": analysis["scores"],
            "soft_warning_combinations": analysis["soft_warning_combinations"],
            "soft_warning_effects": analysis["soft_warning_effects"],
            "frequency": self._frequency(funnel, observed_hours),
            "llm_cache": self._llm_cache(cycle_rows),
            "thresholds": {
                "watch": config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE,
                "trade": config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
                "minimum_risk_reward": config.MIN_RISK_REWARD_RATIO,
                "confirmation_updates": config.FAST_ENTRY_CONFIRMATION_UPDATES,
            },
            "warnings": [],
        }
        if not cycle_rows:
            result["warnings"].append("No slow-cycle records were available for this session.")
        if result["dynamic_score_buckets"]["outcome_coverage"] == 0:
            result["warnings"].append("No sampled forward-price coverage was available for score calibration.")
        return result

    @staticmethod
    def _entry_geometry(candidate_events, cycles):
        from execution.geometry import geometry_diagnostics
        attempts = [row.get('geometry_diagnostics') for row in candidate_events
                    if row.get('event') == 'PRE_EXECUTION_REFRESH'
                    and isinstance(row.get('geometry_diagnostics'), Mapping)]
        previous = {}
        comparisons = []
        for cycle in cycles:
            for candidate in cycle.get('analyzed_candidates', []):
                metrics = candidate.get('deterministic_technical_metrics') or {}
                validation = metrics.get('technical_validation') or {}
                rules = {row.get('rule_name'): row.get('actual') for row in validation.get('rules', [])}
                levels = rules.get('RESISTANCE_ABOVE_ENTRY') or {}
                if not isinstance(levels, Mapping):
                    continue
                entry, stop, target = levels.get('entry'), rules.get('STOP_REFERENCE_AVAILABLE'), levels.get('resistance')
                if not all(isinstance(v, (int, float)) for v in (entry, stop, target)):
                    continue
                symbol = candidate.get('symbol')
                research = previous.get(symbol)
                if research:
                    detail = geometry_diagnostics(research, entry, stop, target)
                    detail['symbol'] = symbol
                    comparisons.append(detail)
                previous[symbol] = dict(entry=entry, stop=stop, target=target)
        failed = [d for d in attempts if d.get('geometry_rejection_class')]
        drift = sum(d.get('entry_drift_crossed_rr_boundary') is True for d in failed)
        return {
            'attempts_with_geometry_evidence': len(attempts),
            'RR_AT_RESEARCH': _distribution(d.get('RR_AT_RESEARCH_PRICE') for d in attempts),
            'RR_AT_ENTRY': _distribution(d.get('RR_AT_LIVE_ENTRY') for d in attempts),
            'RR_DEGRADATION': _distribution(d.get('RR_DEGRADATION') for d in attempts),
            'rr_failures_crossing_boundary_due_to_entry_drift': drift,
            'entry_drift_failure_percent': 100*drift/len(failed) if failed else None,
            'refreshed_geometry_restored_valid_rr': sum(d.get('geometry_restored_valid_rr') is True for d in attempts),
            'correctly_rejected_extended': sum(d.get('geometry_rejection_class') == 'ENTRY_EXTENDED' for d in attempts),
            'nearby_resistance_only_attribution': None,
            'wide_structural_stop_only_attribution': None,
            'failures_with_resistance_contraction': sum(d.get('resistance_contraction_crossed_rr_boundary') is True for d in failed),
            'failures_with_stop_widening': sum(d.get('stop_widening_crossed_rr_boundary') is True for d in failed),
            'attribution_note': 'RR jointly depends on reward room and stop distance; exclusive causal counts require paired geometry evidence.',
            'historical_adjacent_slow_comparisons': len(comparisons),
            'historical_entry_drift_boundary_crossings': sum(d['entry_drift_crossed_rr_boundary'] for d in comparisons),
            'historical_rr_failures_with_entry_drift': sum(d['entry_drift_crossed_rr_boundary'] and d['geometry_rejection_class'] is not None for d in comparisons),
            'historical_comparison_note': 'Adjacent slow snapshots are observational comparisons, not replayed entry attempts.',
            'historical_examples': [d for d in comparisons if d['entry_drift_crossed_rr_boundary']],
        }

    @staticmethod
    def _validation(row: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[str], list[str]]:
        metrics = row.get("deterministic_technical_metrics", {})
        validation = metrics.get("technical_validation", {}) if isinstance(metrics, Mapping) else {}
        rules = validation.get("rules", []) if isinstance(validation, Mapping) else []
        rules = [item for item in rules if isinstance(item, Mapping)]
        hard = list(validation.get("true_hard_gate_failures", [])) if isinstance(validation, Mapping) else []
        soft = list(validation.get("signal_quality_failures", [])) if isinstance(validation, Mapping) else []
        if not hard:
            hard = [
                str(item.get("rule_name")) for item in rules
                if item.get("status") == "FAIL" and item.get("classification") in {
                    "DATA_VALIDITY_HARD", "MARKET_ACCESS_HARD", "LIQUIDITY_HARD",
                    "STRUCTURAL_TRADE_HARD", "RISK_HARD",
                }
            ]
        return rules, list(dict.fromkeys(hard)), list(dict.fromkeys(soft))

    def _analyze_candidates(
        self, analyzed: Sequence[tuple[int, Mapping[str, Any]]],
        score_rows: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        categories: dict[str, Counter[str]] = defaultdict(Counter)
        gate_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        slow_buckets: dict[str, dict[str, Any]] = {
            label: {"count": 0, "admitted": 0, "hard_rejected": 0}
            for label, _low, _high in SLOW_BUCKETS
        }
        technical_scores: list[float] = []
        qualitative_scores: list[float] = []
        slow_scores: list[float] = []
        warning_combinations: Counter[str] = Counter()
        score_and_warnings: list[tuple[float, set[str]]] = []
        unique_opportunities = len(analyzed)
        later_prices: dict[str, list[tuple[int, float]]] = defaultdict(list)
        fast_max: dict[str, float] = {}
        for index, item in analyzed:
            metrics = item.get("deterministic_technical_metrics", {})
            price = _number(metrics.get("current_price")) if isinstance(metrics, Mapping) else None
            symbol = str(item.get("symbol") or "").upper()
            if symbol and price is not None:
                later_prices[symbol].append((index, price))
        for item in score_rows:
            symbol = str(item.get("symbol") or "").upper()
            score = _number(item.get("dynamic_score"))
            if symbol and score is not None:
                fast_max[symbol] = max(score, fast_max.get(symbol, 0))
        for cycle_index, row in analyzed:
            coordinator = row.get("coordinator_decision", {})
            coordinator = coordinator if isinstance(coordinator, Mapping) else {}
            slow = _number(coordinator.get("combined_score"))
            live = _number(row.get("live_score"))
            dynamic = _number(row.get("dynamic_score"))
            technical = _number(coordinator.get("technical_score"))
            qualitative = _number(coordinator.get("qualitative_score"))
            if technical is not None:
                technical_scores.append(technical)
            if qualitative is not None:
                qualitative_scores.append(qualitative)
            if slow is not None:
                slow_scores.append(slow)
            rules, hard, soft = self._validation(row)
            if soft:
                warning_combinations[" + ".join(sorted(soft))] += 1
                for reason in soft:
                    categories["SIGNAL_QUALITY_WARNING"][reason] += 1
            if slow is not None:
                score_and_warnings.append((slow, set(soft)))
            bucket = _bucket(slow, SLOW_BUCKETS)
            if bucket:
                slow_buckets[bucket]["count"] += 1
                if hard:
                    slow_buckets[bucket]["hard_rejected"] += 1
                elif slow is not None and slow >= config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:
                    slow_buckets[bucket]["admitted"] += 1
                metrics = row.get("deterministic_technical_metrics", {})
                start = _number(metrics.get("current_price")) if isinstance(metrics, Mapping) else None
                symbol = str(row.get("symbol") or "").upper()
                forward = [price for later_index, price in later_prices.get(symbol, []) if later_index > cycle_index]
                if start is not None and start > 0 and forward:
                    slow_buckets[bucket].setdefault("max_followup_returns_percent", []).append(
                        100 * (max(forward) / start - 1)
                    )
                if fast_max.get(symbol, 0) >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:
                    slow_buckets[bucket]["symbols_later_observed_above_0.72"] = (
                        slow_buckets[bucket].get("symbols_later_observed_above_0.72", 0) + 1
                    )
            if hard:
                for reason in hard:
                    categories["TERMINAL_HARD_REJECTION"][reason] += 1
            elif slow is not None and slow < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:
                categories["SCORE_BELOW_THRESHOLD"]["SLOW_SCORE_BELOW_0.60"] += 1
            vetoes = coordinator.get("vetoes", [])
            if isinstance(vetoes, Sequence) and not isinstance(vetoes, (str, bytes)):
                for reason in vetoes:
                    if hard and str(reason).startswith("TRUE_HARD_REJECTION"):
                        continue
                    categories[rejection_class(str(reason))][str(reason)] += 1
            rules_by_name = {str(rule.get("rule_name")): rule for rule in rules}
            for raw_name in hard:
                name = {
                    "STOP_REFERENCE_AVAILABLE": "INVALID_STOP",
                }.get(raw_name, raw_name)
                gate_rows[name].append(self._geometry_row(
                    row, rules_by_name, cycle_index=cycle_index,
                    slow_score=slow, live_score=live, dynamic_score=dynamic,
                ))
        hard_gate_report: dict[str, Any] = {}
        geometry_examples: dict[str, list[dict[str, Any]]] = {}
        for gate in GEOMETRY_GATES:
            rows = gate_rows.get(gate, [])
            strong = [row for row in rows if (row.get("technical_score") or 0) >= 0.75]
            hard_gate_report[gate] = {
                "blocked": len(rows),
                "percent_of_analyzed": round(100 * len(rows) / unique_opportunities, 2) if unique_opportunities else None,
                "technically_strong_blocked": len(strong),
                "percent_of_strong_candidates": None,
                "average_slow_score": self._average(row.get("slow_score") for row in rows),
                "average_live_score": self._average(row.get("live_score") for row in rows),
                "average_dynamic_score": self._average(row.get("dynamic_score") for row in rows),
            }
            geometry_examples[gate] = rows
        strong_total = sum(
            1 for _index, row in analyzed
            if (_number((row.get("coordinator_decision") or {}).get("technical_score")) or 0) >= 0.75
        )
        for value in hard_gate_report.values():
            value["percent_of_strong_candidates"] = (
                round(100 * value["technically_strong_blocked"] / strong_total, 2)
                if strong_total else None
            )
        for bucket in slow_buckets.values():
            returns = bucket.pop("max_followup_returns_percent", [])
            bucket["forward_price_coverage"] = len(returns)
            bucket["average_max_followup_return_percent"] = self._average(returns)
            bucket.setdefault("symbols_later_observed_above_0.72", 0)
            bucket["dynamic_followup_caveat"] = (
                "Symbol-level later observation may belong to a newer slow context; "
                "sub-threshold contexts were not fast-watched."
            )
        all_warnings = sorted({name for _score, names in score_and_warnings for name in names})
        warning_effects = {}
        for name in all_warnings:
            failed = [score for score, names in score_and_warnings if name in names]
            passed = [score for score, names in score_and_warnings if name not in names]
            failed_mean = self._average(failed)
            passed_mean = self._average(passed)
            warning_effects[name] = {
                "failed_count": len(failed),
                "average_slow_score_when_failed": failed_mean,
                "average_slow_score_when_not_failed": passed_mean,
                "observed_score_difference": (
                    round(failed_mean - passed_mean, 6)
                    if failed_mean is not None and passed_mean is not None else None
                ),
                "causal_caveat": "Observed association; correlated signals and other score inputs are not held constant.",
            }
        return {
            "rejections": {
                category: {
                    "total": sum(counter.values()),
                    "reasons": dict(counter.most_common()),
                }
                for category, counter in categories.items()
            },
            "hard_gates": hard_gate_report,
            "geometry_examples": geometry_examples,
            "slow_score_buckets": slow_buckets,
            "scores": {
                "slow": _distribution(slow_scores),
                "qualitative": _distribution(qualitative_scores),
                "technical": _distribution(technical_scores),
            },
            "soft_warning_combinations": dict(warning_combinations.most_common()),
            "soft_warning_effects": warning_effects,
            "hard_gate_rows": gate_rows,
        }

    @staticmethod
    def _merge_runtime_rejections(
        report: dict[str, Any], score_rows: Sequence[Mapping[str, Any]],
        candidate_events: Sequence[Mapping[str, Any]],
        watcher_events: Sequence[Mapping[str, Any]],
    ) -> None:
        def add(reason: str) -> None:
            category = rejection_class(reason)
            group = report.setdefault(category, {"total": 0, "reasons": {}})
            group["total"] += 1
            reasons = group["reasons"]
            reasons[reason] = int(reasons.get(reason, 0)) + 1

        for row in score_rows:
            if row.get("blocker"):
                add(str(row["blocker"]))
        for row in candidate_events:
            if row.get("event") == "PRE_EXECUTION_REFRESH" and row.get("status") == "REJECTED":
                add(str(row.get("rejection_reason") or "PRE_EXECUTION_REFRESH_REJECTED"))
        for row in watcher_events:
            if row.get("event") in {"FAST_PROVIDER_ERROR", "FAST_QUOTE_STALE"}:
                add(str(row.get("event")))

    @staticmethod
    def _merge_runtime_hard_gates(
        report: dict[str, Any], score_rows: Sequence[Mapping[str, Any]]
    ) -> None:
        mapping = {
            "RISK_REWARD": "MINIMUM_RISK_REWARD",
            "INVALID_STOP": "INVALID_STOP",
            "PRICE_BEYOND_TARGET": "RESISTANCE_ABOVE_ENTRY",
            "SPREAD_TOO_WIDE": "SPREAD",
            "INVALID_SPREAD": "SPREAD",
        }
        grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
        for row in score_rows:
            gate = mapping.get(str(row.get("blocker") or ""))
            symbol = str(row.get("symbol") or "").upper()
            if gate and symbol:
                grouped[gate][symbol] = row
        for gate, symbols in grouped.items():
            value = report.setdefault(gate, {})
            rows = list(symbols.values())
            value["fast_runtime_symbols_blocked"] = len(rows)
            value["average_fast_slow_score"] = ShadowSessionAudit._average(
                row.get("slow_score") for row in rows
            )
            value["average_fast_live_score"] = ShadowSessionAudit._average(
                row.get("live_score") for row in rows
            )
            value["average_fast_dynamic_score"] = ShadowSessionAudit._average(
                row.get("dynamic_score") for row in rows
            )
            value["fast_technically_strong_blocked"] = sum(
                (_number(row.get("technical_score")) or 0) >= 0.75 for row in rows
            )

    @staticmethod
    def _geometry_diagnostics(examples: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
        rr_rows = list(examples.get("MINIMUM_RISK_REWARD", []))
        formula_mismatches = 0
        denominator_errors = 0
        narrow_reward_vs_required_risk = 0
        late_entry_with_unchanged_target = 0
        previous_by_symbol: dict[str, Mapping[str, Any]] = {}
        for row in rr_rows:
            risk = _number(row.get("risk_distance"))
            reward = _number(row.get("reward_distance"))
            rr = _number(row.get("calculated_risk_reward"))
            if risk is None or reward is None or risk <= 0:
                denominator_errors += 1
            # Distances in the human-facing examples are rounded to 6 decimals.
            elif rr is None or abs(rr - reward / risk) > 1e-4:
                formula_mismatches += 1
            if risk is not None and reward is not None and reward < risk * config.MIN_RISK_REWARD_RATIO:
                narrow_reward_vs_required_risk += 1
            symbol = str(row.get("symbol") or "")
            prior = previous_by_symbol.get(symbol)
            if prior:
                prior_target = _number(prior.get("target"))
                target = _number(row.get("target"))
                prior_entry = _number(prior.get("entry"))
                entry = _number(row.get("entry"))
                prior_rr = _number(prior.get("calculated_risk_reward"))
                if (
                    None not in (prior_target, target, prior_entry, entry, prior_rr, rr)
                    and abs(target - prior_target) < 1e-6 and entry > prior_entry and rr < prior_rr
                ):
                    late_entry_with_unchanged_target += 1
            previous_by_symbol[symbol] = row
        stop_rows = list(examples.get("MINIMUM_STOP_DISTANCE", []))
        return {
            "minimum_risk_reward": {
                "samples": len(rr_rows),
                "formula_mismatches": formula_mismatches,
                "invalid_denominators": denominator_errors,
                "reward_below_1.5x_risk": narrow_reward_vs_required_risk,
                "later_entry_with_unchanged_target_and_lower_rr": late_entry_with_unchanged_target,
                "calculation": "(target - entry) / (entry - stop)",
            },
            "minimum_stop_distance": {
                "samples": len(stop_rows),
                "all_below_required_minimum": bool(stop_rows) and all(
                    (_number(row.get("stop_distance_percent")) or float("inf")) < 0.2
                    for row in stop_rows
                ),
                "calculation": "(entry - stop) / entry",
            },
        }

    @staticmethod
    def _geometry_row(row: Mapping[str, Any], rules: Mapping[str, Mapping[str, Any]], **extra: Any) -> dict[str, Any]:
        stop = _number((rules.get("STOP_REFERENCE_AVAILABLE") or {}).get("actual"))
        resistance_actual = (rules.get("RESISTANCE_ABOVE_ENTRY") or {}).get("actual")
        resistance_actual = resistance_actual if isinstance(resistance_actual, Mapping) else {}
        entry = _number(resistance_actual.get("entry"))
        target = _number(resistance_actual.get("resistance"))
        stop_ratio = _number((rules.get("MINIMUM_STOP_DISTANCE") or {}).get("actual"))
        rr = _number((rules.get("MINIMUM_RISK_REWARD") or {}).get("actual"))
        coordinator = row.get("coordinator_decision", {})
        coordinator = coordinator if isinstance(coordinator, Mapping) else {}
        return {
            "symbol": str(row.get("symbol", "")),
            "entry": entry,
            "stop": stop,
            "target": target,
            "risk_distance": round(entry - stop, 6) if entry is not None and stop is not None else None,
            "reward_distance": round(target - entry, 6) if target is not None and entry is not None else None,
            "stop_distance_percent": round(stop_ratio * 100, 6) if stop_ratio is not None else None,
            "minimum_stop_distance_percent": 0.2,
            "calculated_risk_reward": rr,
            "minimum_required_risk_reward": config.MIN_RISK_REWARD_RATIO,
            "technical_score": _number(coordinator.get("technical_score")),
            **extra,
        }

    @staticmethod
    def _average(values: Iterable[Any]) -> float | None:
        rows = [item for value in values if (item := _number(value)) is not None]
        return round(mean(rows), 6) if rows else None

    @staticmethod
    def _fast_opportunities(
        score_rows: Sequence[Mapping[str, Any]],
        candidate_events: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in score_rows:
            if row.get("symbol"):
                symbol = str(row["symbol"]).upper()
                episode = str(row.get("research_cycle_id") or "LEGACY_SYMBOL_SCOPE")
                grouped[(symbol, episode)].append(row)
        terminal: dict[str, tuple[datetime, str]] = {}
        for row in [*candidate_events, *events]:
            symbol = str(row.get("symbol") or "").upper()
            at = _timestamp(row.get("timestamp"))
            if not symbol or at is None:
                continue
            reason = row.get("reason") or row.get("rejection_reason")
            if row.get("event") in {"CONTEXT_EXPIRED", "SHADOW_ENTRY_BLOCKED", "WATCH_TO_SHADOW_POSITION", "SHADOW_ENTRY", "POSITION_OPENED"}:
                terminal[symbol] = (at, str(reason or row.get("event")))
        result = []
        for (symbol, episode), rows in sorted(grouped.items()):
            rows = sorted(rows, key=lambda item: str(item.get("timestamp", "")))
            times = [at for row in rows if (at := _timestamp(row.get("timestamp")))]
            dynamic = [_number(row.get("dynamic_score")) for row in rows]
            live = [_number(row.get("live_score")) for row in rows]
            above70 = [at for row in rows if (_number(row.get("dynamic_score")) or 0) >= 0.70 and (at := _timestamp(row.get("timestamp")))]
            above72 = [at for row in rows if (_number(row.get("dynamic_score")) or 0) >= 0.72 and (at := _timestamp(row.get("timestamp")))]
            result.append({
                "symbol": symbol,
                "research_cycle_id": None if episode == "LEGACY_SYMBOL_SCOPE" else episode,
                "admitted_at": min(times).isoformat() if times else None,
                "slow_score": next((_number(row.get("slow_score")) for row in rows if _number(row.get("slow_score")) is not None), None),
                "live_updates_logged": len(rows),
                "maximum_live_score": max((value for value in live if value is not None), default=None),
                "maximum_dynamic_score": max((value for value in dynamic if value is not None), default=None),
                "logged_span_above_0.70_seconds": ShadowSessionAudit._time_span(above70),
                "logged_span_above_0.72_seconds": ShadowSessionAudit._time_span(above72),
                "confirmation_opportunities": sum(1 for value in dynamic if value is not None and value >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD),
                "stopped_reason": terminal.get(symbol, (None, "STILL_ACTIVE_OR_REPLACED"))[1],
            })
        return result

    @staticmethod
    def _time_span(times: Sequence[datetime]) -> float:
        return round((max(times) - min(times)).total_seconds(), 3) if len(times) >= 2 else 0.0

    def _funnel(
        self, scanner: Sequence[Mapping[str, Any]],
        analyzed: Sequence[tuple[int, Mapping[str, Any]]], analysis: Mapping[str, Any],
        opportunities: Sequence[Mapping[str, Any]], candidate_events: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]], selected: str,
    ) -> dict[str, Any]:
        hard_total = sum(
            1 for _index, row in analyzed if self._validation(row)[1]
        )
        below = 0
        admitted = 0
        for _index, row in analyzed:
            coordinator = row.get("coordinator_decision", {})
            coordinator = coordinator if isinstance(coordinator, Mapping) else {}
            score = _number(coordinator.get("combined_score"))
            _rules, hard, _soft = self._validation(row)
            vetoes = coordinator.get("vetoes", [])
            vetoed = isinstance(vetoes, Sequence) and not isinstance(vetoes, (str, bytes)) and bool(vetoes)
            if not hard and not vetoed and score is not None and score < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE:
                below += 1
            if not hard and not vetoed and score is not None and score >= config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE and row.get("entry") is not None:
                admitted += 1
        def unique_reached(level: float) -> int:
            return sum(1 for row in opportunities if (_number(row.get("maximum_dynamic_score")) or 0) >= level)
        pre = [row for row in candidate_events if row.get("event") == "PRE_EXECUTION_REFRESH"]
        passed = [row for row in pre if row.get("status") == "APPROVED"]
        hard_failed = [row for row in pre if str(row.get("rejection_reason") or "").startswith("REFRESHED_HARD_GATE_FAILED")]
        confirmed = {
            str(row.get("symbol")) for row in events
            if row.get("event") == "TRADE_CANDIDATE"
        }
        risk_approved = sum(row.get("event") == "RISK_APPROVED" for row in events)
        entries = {
            (str(row.get("symbol")), str(row.get("timestamp"))) for row in events
            if row.get("event") == "SHADOW_ENTRY"
        }
        if not entries:
            entries = {
                (str(row.get("symbol")), str(row.get("timestamp"))) for row in candidate_events
                if row.get("event") == "WATCH_TO_SHADOW_POSITION"
            }
        state = _read_json(self.state_dir / "shadow_portfolio.json")
        exits = sum(
            1 for row in state.get("closed_positions", [])
            if isinstance(row, Mapping) and _session_date(row.get("exit_timestamp")) == selected
        )
        stages = [
            ("scanner_candidates", len(scanner)),
            ("unique_symbols_discovered", len({str(row.get("symbol", "")).upper() for row in scanner if row.get("symbol")})),
            ("slow_analyses_completed", len(analyzed)),
            ("true_hard_rejected", hard_total),
            ("slow_score_below_0.60", below),
            ("fast_watch_admitted", admitted),
            ("setup_forming", len(opportunities)),
            ("dynamic_reached_0.65", unique_reached(0.65)),
            ("dynamic_reached_0.70", unique_reached(0.70)),
            ("dynamic_reached_0.72", unique_reached(0.72)),
            ("confirmed_threshold_crossings", len(confirmed)),
            ("pre_execution_attempts", len(pre)),
            ("pre_execution_hard_gate_failures", len(hard_failed)),
            ("pre_execution_passed", len(passed)),
            ("risk_approvals", risk_approved or len(passed)),
            ("shadow_entries", len(entries)),
            ("shadow_exits", exits),
        ]
        counts = dict(stages)
        paths = (
            ("scanner_candidates", "slow_analyses_completed"),
            ("slow_analyses_completed", "fast_watch_admitted"),
            ("fast_watch_admitted", "setup_forming"),
            ("setup_forming", "dynamic_reached_0.65"),
            ("dynamic_reached_0.65", "dynamic_reached_0.70"),
            ("dynamic_reached_0.70", "dynamic_reached_0.72"),
            ("dynamic_reached_0.72", "confirmed_threshold_crossings"),
            ("confirmed_threshold_crossings", "pre_execution_attempts"),
            ("pre_execution_attempts", "pre_execution_passed"),
            ("pre_execution_passed", "risk_approvals"),
            ("risk_approvals", "shadow_entries"),
            ("shadow_entries", "shadow_exits"),
        )
        conversions = {
            f"{source}_to_{target}": (
                round(100 * counts[target] / counts[source], 2)
                if counts[source] else None
            )
            for source, target in paths
        }
        attrition = {
            "true_hard_rejected_percent_of_slow_analyses": (
                round(100 * hard_total / len(analyzed), 2) if analyzed else None
            ),
            "below_0.60_percent_of_slow_analyses": (
                round(100 * below / len(analyzed), 2) if analyzed else None
            ),
            "pre_execution_hard_failure_percent_of_attempts": (
                round(100 * len(hard_failed) / len(pre), 2) if pre else None
            ),
        }
        return {"counts": counts, "conversion_percent": conversions, "attrition_percent": attrition}

    @staticmethod
    def _price_series(score_rows: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[datetime, float]]]:
        result: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for row in score_rows:
            at, price = _timestamp(row.get("timestamp")), _number(row.get("price"))
            symbol = str(row.get("symbol") or "").upper()
            if at and price is not None and symbol:
                result[symbol].append((at, price))
        for rows in result.values():
            rows.sort()
        return result

    def _dynamic_buckets(
        self, score_rows: Sequence[Mapping[str, Any]],
        analyzed: Sequence[tuple[int, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        series = self._price_series(score_rows)
        geometries: dict[str, tuple[float | None, float | None]] = {}
        for _index, row in analyzed:
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                geometries[symbol] = (_number(row.get("stop")), _number(row.get("target")))
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in score_rows:
            dynamic = _number(row.get("dynamic_score"))
            label = _bucket(dynamic, DYNAMIC_BUCKETS)
            at, entry = _timestamp(row.get("timestamp")), _number(row.get("price"))
            symbol = str(row.get("symbol") or "").upper()
            if label is None or at is None or entry is None or not symbol:
                continue
            future = [
                (stamp, price) for stamp, price in series.get(symbol, [])
                if stamp > at and stamp <= at + timedelta(minutes=20)
            ]
            outcome: dict[str, Any] = {}
            if future:
                prices = [price for _stamp, price in future]
                outcome["mfe_percent"] = 100 * (max(prices) / entry - 1)
                outcome["mae_percent"] = 100 * (min(prices) / entry - 1)
                stop, target = geometries.get(symbol, (None, None))
                outcome["target_observed"] = target is not None and max(prices) >= target
                outcome["stop_observed"] = stop is not None and min(prices) <= stop
                for minutes in (5, 10, 20):
                    target_at = at + timedelta(minutes=minutes)
                    sample = next((price for stamp, price in future if stamp >= target_at), None)
                    outcome[f"forward_{minutes}m_percent"] = (
                        100 * (sample / entry - 1) if sample is not None else None
                    )
            buckets[label].append(outcome)
        report: dict[str, Any] = {}
        coverage = 0
        for label, _low, _high in DYNAMIC_BUCKETS:
            rows = buckets.get(label, [])
            covered = [row for row in rows if row]
            coverage += len(covered)
            report[label] = {
                "samples": len(rows),
                "forward_coverage": len(covered),
                "mfe_percent": self._average(row.get("mfe_percent") for row in covered),
                "mae_percent": self._average(row.get("mae_percent") for row in covered),
                "forward_5m_percent": self._average(row.get("forward_5m_percent") for row in covered),
                "forward_10m_percent": self._average(row.get("forward_10m_percent") for row in covered),
                "forward_20m_percent": self._average(row.get("forward_20m_percent") for row in covered),
                "target_observed_count": sum(row.get("target_observed") is True for row in covered),
                "stop_observed_count": sum(row.get("stop_observed") is True for row in covered),
            }
        return {"buckets": report, "outcome_coverage": coverage}

    def _counterfactuals(
        self, analyzed: Sequence[tuple[int, Mapping[str, Any]]],
        score_rows: Sequence[Mapping[str, Any]], candidate_events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        eligible_slow: list[float] = []
        for _index, row in analyzed:
            coordinator = row.get("coordinator_decision", {})
            coordinator = coordinator if isinstance(coordinator, Mapping) else {}
            score = _number(coordinator.get("combined_score"))
            _rules, hard, _soft = self._validation(row)
            if score is not None and not hard and not coordinator.get("vetoes"):
                eligible_slow.append(score)
        maximum_by_symbol: dict[str, float] = {}
        for row in score_rows:
            symbol, score = str(row.get("symbol") or "").upper(), _number(row.get("dynamic_score"))
            if symbol and score is not None:
                maximum_by_symbol[symbol] = max(score, maximum_by_symbol.get(symbol, 0))
        current_trade = config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD
        configurations = ((0.60, 0.72), (0.58, 0.72), (0.55, 0.72), (0.60, 0.70), (0.60, 0.68), (0.58, 0.70), (0.55, 0.68))
        preapproved = {
            str(row.get("symbol") or "").upper() for row in candidate_events
            if row.get("event") == "PRE_EXECUTION_REFRESH" and row.get("status") == "APPROVED"
        }
        result = []
        for watch, trade in configurations:
            newly_watched = sum(watch <= score < config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE for score in eligible_slow)
            extra_trade = sum(trade <= score < current_trade for score in maximum_by_symbol.values()) if trade < current_trade else 0
            result.append({
                "watch_threshold": watch,
                "trade_threshold": trade,
                "minimum_risk_reward": config.MIN_RISK_REWARD_RATIO,
                "additional_watch_opportunities": newly_watched,
                "additional_trade_candidates_observed": extra_trade,
                "additional_hard_gate_passes": 0 if not extra_trade else None,
                "additional_shadow_entries": 0 if not extra_trade else None,
                "hypothetical_pnl": None,
                "win_rate": None,
                "average_winner": None,
                "average_loser": None,
                "profit_factor": None,
                "maximum_drawdown": None,
                "expectancy": None,
                "evidence_note": (
                    "Runtime settings unchanged. Lower-watch names lack fast quotes; lower-trade "
                    "crossings lack refreshed-gate and fill evidence."
                ),
                "current_pre_execution_pass_symbols": len(preapproved),
            })
        return result

    def _quote_reliability(
        self, watcher_events: Sequence[Mapping[str, Any]],
        score_rows: Sequence[Mapping[str, Any]], selected: str,
    ) -> dict[str, Any]:
        status = _read_json(self.state_dir / "fast_watcher_status.json")
        status_date = _session_date(status.get("heartbeat"))
        provider = status.get("provider_metrics", {}) if status_date == selected else {}
        metrics = status.get("metrics", {}) if status_date == selected else {}
        requests = int(provider.get("request_count", metrics.get("quote_requests", 0)) or 0)
        failures = int(provider.get("failure_count", metrics.get("quote_failures", 0)) or 0)
        threshold_near_failure = 0
        failure_times = [
            at for row in watcher_events
            if row.get("event") in {"FAST_PROVIDER_ERROR", "FAST_QUOTE_STALE"}
            and (at := _timestamp(row.get("timestamp")))
        ]
        for row in score_rows:
            at, score = _timestamp(row.get("timestamp")), _number(row.get("dynamic_score"))
            if at and score is not None and score >= 0.70 and any(abs((at - failed).total_seconds()) <= 30 for failed in failure_times):
                threshold_near_failure += 1
        return {
            "requests": requests,
            "successes": max(0, requests - failures),
            "failures": failures,
            "failure_rate_percent": round(100 * failures / requests, 3) if requests else None,
            "success_rate_percent": round(100 * (requests - failures) / requests, 3) if requests else None,
            "median_latency_seconds": _number(provider.get("median_latency_seconds")),
            "p95_latency_seconds": _number(provider.get("p95_latency_seconds")),
            "average_latency_seconds": _number(provider.get("average_latency_seconds")),
            "max_latency_seconds": _number(provider.get("max_latency_seconds")),
            "median_quote_age_seconds": _number(provider.get("median_quote_age_seconds")),
            "p95_quote_age_seconds": _number(provider.get("p95_quote_age_seconds")),
            "average_quote_age_seconds": _number(provider.get("average_quote_age_seconds", metrics.get("average_quote_age"))),
            "max_quote_age_seconds": _number(metrics.get("max_quote_age")),
            "threshold_crossings_within_30s_of_failure": threshold_near_failure,
            "raw_percentiles_unavailable": not all(
                provider.get(name) is not None for name in (
                    "median_latency_seconds", "p95_latency_seconds",
                    "median_quote_age_seconds", "p95_quote_age_seconds",
                )
            ),
            "scope_note": "Provider counters cover the persisted watcher process; event counts cover the selected session.",
        }

    @staticmethod
    def _terminal_rejections(analysis: Mapping[str, Any], candidate_events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        counter: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        hard = analysis.get("rejections", {}).get("TERMINAL_HARD_REJECTION", {}).get("reasons", {})
        for reason, count in hard.items():
            classification = reconsideration_class(reason)
            counter[classification] += int(count)
            reasons[f"{classification}:{reason}"] += int(count)
        for row in candidate_events:
            if row.get("event") != "SHADOW_ENTRY_BLOCKED":
                continue
            reason = str(row.get("reason") or "UNKNOWN")
            classification = reconsideration_class(reason)
            counter[classification] += 1
            reasons[f"{classification}:{reason}"] += 1
        return {"counts": dict(counter), "details": dict(reasons)}

    @staticmethod
    def _scanner_coverage(
        cycles: Sequence[Mapping[str, Any]], scanner: Sequence[Mapping[str, Any]],
        analyzed: Sequence[tuple[int, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        per_cycle = [len(row.get("scanner_candidates", [])) for row in cycles]
        symbols = [str(row.get("symbol") or "").upper() for row in scanner if row.get("symbol")]
        repeats = Counter(symbols)
        return {
            "cycles": len(cycles),
            "results_per_cycle": per_cycle,
            "average_results_per_cycle": round(mean(per_cycle), 3) if per_cycle else None,
            "total_results": len(scanner),
            "unique_symbols": len(set(symbols)),
            "repeat_observations": sum(max(0, count - 1) for count in repeats.values()),
            "analyzed": len(analyzed),
            "persisted_but_not_analyzed": max(0, len(scanner) - len(analyzed)),
            "excluded_by_top_n": None,
            "eligible_outside_top_n": None,
            "coverage_note": "Rows outside the persisted scanner candidate set were not recorded; their eligibility is unavailable.",
        }

    @staticmethod
    def _position_blocks(candidate_events: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        result = Counter({
            "max_concurrent_positions": 0,
            "capital_availability": 0,
            "portfolio_exposure": 0,
            "risk_budget": 0,
            "sector_concentration": 0,
            "other_risk": 0,
        })
        for row in [*candidate_events, *events]:
            reason = str(row.get("reason") or row.get("rejection_reason") or "").upper()
            if "MAX_POSITIONS" in reason or "SIMULTANEOUS" in reason:
                result["max_concurrent_positions"] += 1
            elif "BUYING_POWER" in reason or "CAPITAL" in reason:
                result["capital_availability"] += 1
            elif "EXPOSURE" in reason:
                result["portfolio_exposure"] += 1
            elif "RISK_REJECTED" in reason or "DAILY_LOSS" in reason or "MAX_TRADES" in reason:
                result["risk_budget"] += 1
            elif "SECTOR" in reason:
                result["sector_concentration"] += 1
        return dict(result)

    @staticmethod
    def _frequency(funnel: Mapping[str, Any], hours: float | None) -> dict[str, Any]:
        counts = funnel.get("counts", {})
        def rate(name: str) -> float | None:
            return round(float(counts.get(name, 0)) / hours, 3) if hours else None
        return {
            "observed_hours": round(hours, 3) if hours else None,
            "scanner_candidates_per_hour": rate("scanner_candidates"),
            "fast_watch_admissions_per_hour": rate("fast_watch_admitted"),
            "trade_candidates_per_hour": rate("confirmed_threshold_crossings"),
            "shadow_entries_per_hour": rate("shadow_entries"),
            "shadow_entries_per_session": counts.get("shadow_entries", 0),
        }

    @staticmethod
    def _llm_cache(cycles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        hits = misses = 0
        for cycle in cycles:
            trace = cycle.get("llm_reasoning", {})
            trace = trace.get("trace", {}) if isinstance(trace, Mapping) else {}
            diagnostics = trace.get("diagnostics", {}) if isinstance(trace, Mapping) else {}
            rows = diagnostics.get("per_candidate", {}) if isinstance(diagnostics, Mapping) else {}
            if not isinstance(rows, Mapping):
                continue
            for value in rows.values():
                if not isinstance(value, Mapping):
                    continue
                if value.get("cache_hit") is True:
                    hits += 1
                else:
                    misses += 1
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate_percent": round(100 * hits / total, 2) if total else None,
        }
