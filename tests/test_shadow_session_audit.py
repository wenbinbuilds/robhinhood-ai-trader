import json
from pathlib import Path

import runner  # Establishes the project's existing import order.
import config
from shadow.session_audit import (
    ShadowSessionAudit,
    reconsideration_class,
    rejection_class,
)


DATE = "2026-09-17"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def candidate(symbol: str, slow: float, *, hard: str | None = None) -> dict:
    rules = [
        {
            "rule_name": "STOP_REFERENCE_AVAILABLE", "actual": 99.0,
            "status": "PASS", "classification": "STRUCTURAL_TRADE_HARD",
        },
        {
            "rule_name": "MINIMUM_STOP_DISTANCE", "actual": 0.01,
            "status": "PASS", "classification": "STRUCTURAL_TRADE_HARD",
        },
        {
            "rule_name": "RESISTANCE_ABOVE_ENTRY",
            "actual": {"entry": 100.0, "resistance": 102.0},
            "status": "PASS", "classification": "STRUCTURAL_TRADE_HARD",
        },
        {
            "rule_name": "MINIMUM_RISK_REWARD", "actual": 2.0,
            "status": "PASS", "classification": "RISK_HARD",
        },
    ]
    if hard:
        item = next(row for row in rules if row["rule_name"] == hard)
        item["status"] = "FAIL"
        if hard == "MINIMUM_RISK_REWARD":
            item["actual"] = 1.2
    failures = [hard] if hard else []
    return {
        "symbol": symbol,
        "entry": None if hard else 100.0,
        "stop": None if hard else 99.0,
        "target": None if hard else 102.0,
        "risk_reward_ratio": None if hard else 2.0,
        "technical_disposition": "REJECTED" if hard else "MONITORABLE",
        "coordinator_decision": {
            "combined_score": slow, "technical_score": 0.8,
            "qualitative_score": 0.5, "vetoes": [],
        },
        "deterministic_technical_metrics": {
            "current_price": 100.0,
            "technical_validation": {
                "rules": rules,
                "true_hard_gate_failures": failures,
                "signal_quality_failures": ["MACD"],
            },
        },
    }


def build_session(tmp_path: Path) -> ShadowSessionAudit:
    cycle = {
        "timestamp": f"{DATE}T14:00:00+00:00",
        "scanner_candidates": [
            {"symbol": "PASS"}, {"symbol": "LOW"}, {"symbol": "RR"},
        ],
        "analyzed_candidates": [
            candidate("PASS", 0.65), candidate("LOW", 0.55),
            candidate("RR", 0.8, hard="MINIMUM_RISK_REWARD"),
        ],
    }
    write_jsonl(tmp_path / "logs" / f"{DATE}.jsonl", [cycle])
    write_jsonl(tmp_path / "logs" / "candidate_score_history.jsonl", [
        {"timestamp": f"{DATE}T14:01:00+00:00", "event": "CANDIDATE_SCORE", "symbol": "PASS", "slow_score": 0.65, "live_score": 0.8, "dynamic_score": 0.71, "price": 100.0},
        {"timestamp": f"{DATE}T14:01:20+00:00", "event": "CANDIDATE_SCORE", "symbol": "PASS", "slow_score": 0.65, "live_score": 0.85, "dynamic_score": 0.73, "price": 100.1},
        {"timestamp": f"{DATE}T14:06:20+00:00", "event": "CANDIDATE_SCORE", "symbol": "PASS", "slow_score": 0.65, "live_score": 0.85, "dynamic_score": 0.74, "price": 101.0},
    ])
    write_jsonl(tmp_path / "logs" / "fast_candidate_watcher.jsonl", [
        {"timestamp": f"{DATE}T14:01:21+00:00", "event": "PRE_EXECUTION_REFRESH", "symbol": "PASS", "status": "APPROVED"},
        {"timestamp": f"{DATE}T14:01:22+00:00", "event": "WATCH_TO_SHADOW_POSITION", "symbol": "PASS"},
    ])
    write_jsonl(tmp_path / "logs" / "market_events.jsonl", [
        {"timestamp": f"{DATE}T14:01:21+00:00", "event": "TRADE_CANDIDATE", "symbol": "PASS", "event_id": "a"},
        {"timestamp": f"{DATE}T14:01:21+00:00", "event": "RISK_APPROVED", "symbol": "PASS", "event_id": "b"},
        {"timestamp": f"{DATE}T14:01:22+00:00", "event": "SHADOW_ENTRY", "symbol": "PASS", "event_id": "c"},
    ])
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "fast_watcher_status.json").write_text(json.dumps({
        "heartbeat": f"{DATE}T14:10:00+00:00",
        "provider_metrics": {
            "request_count": 100, "failure_count": 2,
            "average_latency_seconds": 0.2, "max_latency_seconds": 1.1,
            "average_quote_age_seconds": 0.4,
        },
        "metrics": {"max_quote_age": 2.0},
    }), encoding="utf-8")
    return ShadowSessionAudit(tmp_path)


def test_trade_funnel_rejections_and_session_summary(tmp_path: Path) -> None:
    report = build_session(tmp_path).summarize(DATE)
    counts = report["funnel"]["counts"]
    assert counts["scanner_candidates"] == 3
    assert counts["slow_analyses_completed"] == 3
    assert counts["true_hard_rejected"] == 1
    assert counts["slow_score_below_0.60"] == 1
    assert counts["fast_watch_admitted"] == 1
    assert counts["confirmed_threshold_crossings"] == 1
    assert counts["pre_execution_passed"] == 1
    assert counts["shadow_entries"] == 1
    assert report["rejections"]["TERMINAL_HARD_REJECTION"]["reasons"] == {
        "MINIMUM_RISK_REWARD": 1
    }
    assert report["hard_gates"]["MINIMUM_RISK_REWARD"]["blocked"] == 1


def test_score_buckets_fast_watch_and_quote_metrics(tmp_path: Path) -> None:
    report = build_session(tmp_path).summarize(DATE)
    assert report["slow_score_buckets"]["0.55-0.60"]["count"] == 1
    assert report["dynamic_score_buckets"]["buckets"]["0.70-0.72"]["samples"] == 1
    watched = report["fast_watch_opportunities"][0]
    assert watched["symbol"] == "PASS"
    assert watched["maximum_dynamic_score"] == 0.74
    assert watched["confirmation_opportunities"] == 2
    assert report["quote_reliability"]["success_rate_percent"] == 98.0
    assert report["quote_reliability"]["median_latency_seconds"] is None


def test_counterfactual_is_read_only_and_does_not_modify_thresholds(tmp_path: Path) -> None:
    audit = build_session(tmp_path)
    state = tmp_path / "state" / "fast_watcher_status.json"
    before = state.read_bytes()
    thresholds = (
        config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE,
        config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
        config.MIN_RISK_REWARD_RATIO,
        config.FAST_ENTRY_CONFIRMATION_UPDATES,
    )
    report = audit.summarize(DATE)
    lowered = next(
        row for row in report["counterfactuals"]
        if row["watch_threshold"] == 0.55 and row["trade_threshold"] == 0.72
    )
    assert lowered["additional_watch_opportunities"] == 1
    assert lowered["hypothetical_pnl"] is None
    assert state.read_bytes() == before
    assert thresholds == (
        config.WATCHLIST_MIN_SLOW_CONTEXT_SCORE,
        config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD,
        config.MIN_RISK_REWARD_RATIO,
        config.FAST_ENTRY_CONFIRMATION_UPDATES,
    )


def test_rejection_lifecycle_classification() -> None:
    assert rejection_class("QUOTE_UNAVAILABLE") == "INFRASTRUCTURE_FAILURE"
    assert rejection_class("MARKET_CLOSED") == "TEMPORARY_BLOCK"
    assert rejection_class("MACD") == "SIGNAL_QUALITY_WARNING"
    assert reconsideration_class("RISK_REWARD") == "REEVALUATABLE_NEXT_SLOW_CYCLE"
    assert reconsideration_class("UNSUPPORTED_ASSET") == "PERMANENT"


def test_auditor_has_no_llm_or_robinhood_dependency(tmp_path: Path, monkeypatch) -> None:
    build_session(tmp_path)
    monkeypatch.setattr(
        runner, "refresh_and_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("broker path called")),
    )
    report = ShadowSessionAudit(tmp_path).summarize(DATE)
    assert report["session_date"] == DATE

