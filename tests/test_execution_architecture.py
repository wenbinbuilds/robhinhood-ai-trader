import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from jsonschema import Draft202012Validator

import config
from execution.base import ExecutionRouter
from execution.execution_guard import (
    ExecutionContext,
    ExecutionGuard,
    KillSwitchStatus,
    read_kill_switch,
)
from execution.models import TradePlan
from execution.order_state import ExecutionAuditLog, ExecutionState, ExecutionStateStore
from execution.position_watcher import (
    FastPositionWatcher,
    ProtectiveExitStateMachine,
    cap_exit_quantity,
)
from execution.reconciliation import BrokerReconciler, NormalizedBrokerState
from execution.robinhood_executor import LiveExecutionInputs, RobinhoodExecutor
from execution.status import execution_status_snapshot

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def plan(**updates: Any) -> TradePlan:
    values = {
        "trade_id": "trade-123",
        "symbol": "ACME",
        "side": "BUY",
        "strategy": "INTRADAY_MOMENTUM_V1",
        "decision_timestamp": NOW.isoformat(),
        "decision_price": 100.0,
        "entry_type": "MARKET",
        "entry_price": 100.0,
        "quantity": 4,
        "notional": 400.0,
        "stop_price": 98.0,
        "target_price": 104.0,
        "risk_per_share": 2.0,
        "maximum_expected_loss": 8.0,
        "risk_reward_ratio": 2.0,
        "coordinator_score": 0.8,
        "technical_score": 0.9,
        "news_score": 0.7,
        "sector_score": 0.7,
        "market_score": 0.8,
        "thesis": "momentum continuation",
        "invalidation_condition": "momentum breaks",
        "market_data_timestamp": NOW.isoformat(),
    }
    values.update(updates)
    return TradePlan(**values)


def guard_context(**updates: Any) -> ExecutionContext:
    values = {
        "mode": "LIVE_AUTONOMOUS",
        "account_equity": 10_000.0,
        "agentic_account_count": 1,
        "account_state": "active",
        "market_status": "OPEN",
        "is_regular_session": True,
        "snapshot_timestamp": NOW.isoformat(),
        "quote_timestamp": NOW.isoformat(),
        "bid": 99.99,
        "ask": 100.01,
        "symbol_tradable": True,
        "existing_position": False,
        "conflicting_order": False,
        "open_positions": 0,
        "trades_today": 0,
        "daily_realized_pnl": 0.0,
        "daily_loss_blocked": False,
        "risk_result": {"approved": True},
        "kill_switch": KillSwitchStatus(False, "UNBLOCKED"),
        "live_trading_enabled": True,
        "robinhood_execution_enabled": True,
        "confirmation_token": "human-token",
        "expected_confirmation_value": "human-token",
    }
    values.update(updates)
    return ExecutionContext(**values)


class RecordingExecutor:
    def __init__(self, status: str = "FILLED") -> None:
        self.calls: list[str] = []
        self.status = status

    def execute(self, trade_plan: TradePlan, *args: Any, **kwargs: Any):
        from execution.models import ExecutionResult

        self.calls.append(trade_plan.trade_id)
        return ExecutionResult(
            trade_id=trade_plan.trade_id,
            symbol=trade_plan.symbol,
            requested_action=trade_plan.side,
            order_type=trade_plan.entry_type,
            requested_quantity=trade_plan.quantity,
            requested_price=trade_plan.entry_price,
            status=self.status,
            timestamp=NOW.isoformat(),
            reconciliation_state="TEST",
        )


class FakeClient:
    def __init__(self, *, submission: Mapping[str, Any] | None = None) -> None:
        self.calls: list[str] = []
        self.orders: list[Mapping[str, Any]] = []
        self.positions: list[Mapping[str, Any]] = []
        self.accounts: list[Mapping[str, Any]] = [
            {"is_agentic_account": True, "state": "active", "account_reference": "opaque"}
        ]
        self.tradable = True
        self.submission = dict(submission or {"order_id": "safe-order-id", "state": "SUBMITTED"})
        self.place_error: Exception | None = None

    def get_accounts(self): self.calls.append("get_accounts"); return self.accounts
    def get_portfolio(self, account_reference): self.calls.append("get_portfolio"); return {"portfolio_value": 10_000}
    def get_equity_positions(self, account_reference): self.calls.append("get_equity_positions"); return self.positions
    def get_equity_orders(self, account_reference): self.calls.append("get_equity_orders"); return self.orders
    def get_equity_quotes(self, symbols): self.calls.append("get_equity_quotes"); return {"bid": 99.99, "ask": 100.01, "quote_as_of": NOW.isoformat()}
    def get_equity_tradability(self, symbol): self.calls.append("get_equity_tradability"); return {"tradable": self.tradable}
    def review_equity_order(self, request): self.calls.append("review_equity_order"); return {"approved": True, "warnings": []}
    def place_equity_order(self, request):
        self.calls.append("place_equity_order")
        if self.place_error is not None:
            self.orders = [{"symbol": "ACME", "state": "submitted", "client_trade_id": "trade-123"}]
            raise self.place_error
        return self.submission
    def cancel_equity_order(self, request): self.calls.append("cancel_equity_order"); return {}


class FakeSchemaAdapter:
    schema_verified = True

    def build_review_request(self, trade_plan, account_reference):
        return {"schema_bound_test_request": True, "quantity": trade_plan.quantity}

    def review_accepted(self, response): return response.get("approved") is True

    def build_place_request(self, trade_plan, account_reference, review_response):
        return {"schema_bound_test_request": True, "quantity": trade_plan.quantity}

    def normalize_submission(self, response): return response


def enable_live_for_mock_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MODE", "LIVE_AUTONOMOUS")
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "ROBINHOOD_EXECUTION_ENABLED", True)
    monkeypatch.setattr(config, "LIVE_CONFIRMATION_TOKEN", "human-token")
    monkeypatch.setattr(config, "LIVE_CONFIRMATION_EXPECTED_VALUE", "human-token")


def live_inputs(**updates: Any) -> LiveExecutionInputs:
    values = {
        "mode": "LIVE_AUTONOMOUS",
        "snapshot_timestamp": NOW.isoformat(),
        "market_status": "OPEN",
        "is_regular_session": True,
        "trades_today": 0,
        "daily_realized_pnl": 0.0,
        "trading_date": "2026-09-10",
        "confirmation_token": "human-token",
    }
    values.update(updates)
    return LiveExecutionInputs(**values)


def live_executor(tmp_path: Path, client: FakeClient, schema: Any = None) -> RobinhoodExecutor:
    kill = tmp_path / "kill.json"
    kill.write_text('{"trading_blocked": false}\n', encoding="utf-8")
    return RobinhoodExecutor(
        client,
        schema_adapter=schema,
        state_store=ExecutionStateStore(tmp_path / "state.json"),
        audit_log=ExecutionAuditLog(tmp_path / "audit.jsonl"),
        kill_switch_path=kill,
    )


def test_shadow_mode_routes_only_to_shadow_executor() -> None:
    shadow = RecordingExecutor()
    live = RecordingExecutor()
    result = ExecutionRouter(shadow, live).route(
        "SHADOW_TRADING", plan(), now=NOW, market_data={}
    )
    assert result.status == "FILLED"
    assert shadow.calls == ["trade-123"]
    assert live.calls == []


def test_repository_live_defaults_are_all_blocking() -> None:
    assert config.MODE == "SHADOW_TRADING"
    assert config.LIVE_TRADING_ENABLED is False
    assert config.ROBINHOOD_EXECUTION_ENABLED is False
    assert config.LIVE_CONFIRMATION_TOKEN is None
    assert config.LIVE_CONFIRMATION_EXPECTED_VALUE is None
    status = read_kill_switch("state/live_kill_switch.json")
    assert status.trading_blocked is True


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("ANALYSIS_ONLY", "ANALYSIS_ONLY"), ("UNKNOWN", "UNKNOWN_MODE")],
)
def test_nonexecuting_modes_fail_closed(mode: str, reason: str) -> None:
    live = RecordingExecutor()
    result = ExecutionRouter(robinhood_executor=live).route(mode, plan(), now=NOW)
    assert result.errors == (reason,)
    assert live.calls == []


def test_review_only_cannot_reach_order_executor() -> None:
    live = RecordingExecutor()
    result = ExecutionRouter(robinhood_executor=live).route("REVIEW_ONLY", plan(), now=NOW)
    assert result.status == "REJECTED"
    assert live.calls == []


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"agentic_account_count": 0}, "AGENTIC_ACCOUNT_AMBIGUOUS"),
        ({"agentic_account_count": 2}, "AGENTIC_ACCOUNT_AMBIGUOUS"),
        ({"quote_timestamp": (NOW - timedelta(minutes=5)).isoformat()}, "STALE_QUOTE"),
        ({"market_status": "CLOSED", "is_regular_session": False}, "MARKET_CLOSED"),
        ({"market_status": None}, "MARKET_STATUS_UNKNOWN"),
        ({"risk_result": {"approved": False}}, "RISK_REJECTED"),
        ({"daily_loss_blocked": True}, "DAILY_LOSS_LIMIT"),
        ({"open_positions": config.MAX_LIVE_OPEN_POSITIONS}, "MAX_POSITIONS"),
        ({"trades_today": config.MAX_LIVE_TRADES_PER_DAY}, "MAX_TRADES"),
        ({"existing_position": True}, "DUPLICATE_POSITION"),
        ({"conflicting_order": True}, "DUPLICATE_ORDER"),
        ({"symbol_tradable": False}, "SYMBOL_NOT_TRADABLE"),
    ],
)
def test_execution_guard_rejections(updates: Mapping[str, Any], expected: str) -> None:
    result = ExecutionGuard().evaluate(plan(), guard_context(**updates), now=NOW)
    assert result.allowed is False
    assert expected in result.reasons


def test_stale_trade_plan_is_rejected() -> None:
    stale = plan(decision_timestamp=(NOW - timedelta(minutes=5)).isoformat())
    result = ExecutionGuard().evaluate(stale, guard_context(), now=NOW)
    assert "EXPIRED_TRADE_PLAN" in result.reasons


def test_kill_switch_missing_malformed_and_blocked(tmp_path: Path) -> None:
    missing = read_kill_switch(tmp_path / "missing.json")
    assert missing.trading_blocked and missing.status == "KILL_SWITCH_MISSING"
    malformed_path = tmp_path / "bad.json"
    malformed_path.write_text("not json", encoding="utf-8")
    assert read_kill_switch(malformed_path).status == "KILL_SWITCH_MALFORMED"
    blocked_path = tmp_path / "blocked.json"
    blocked_path.write_text('{"trading_blocked": true}', encoding="utf-8")
    assert read_kill_switch(blocked_path).status == "BLOCKED"


@pytest.mark.parametrize("contents", [None, "not json", '{"wrong": true}'])
def test_unusable_kill_switch_blocks_before_broker_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str | None,
) -> None:
    enable_live_for_mock_test(monkeypatch)
    kill = tmp_path / "kill.json"
    if contents is not None:
        kill.write_text(contents, encoding="utf-8")
    client = FakeClient()
    executor = RobinhoodExecutor(
        client,
        schema_adapter=FakeSchemaAdapter(),
        state_store=ExecutionStateStore(tmp_path / "state.json"),
        audit_log=ExecutionAuditLog(tmp_path / "audit.jsonl"),
        kill_switch_path=kill,
    )
    result = executor.execute(plan(), live_inputs(), now=NOW)
    assert any(item.startswith("KILL_SWITCH_") for item in result.errors)
    assert client.calls == []


def test_live_mode_alone_is_insufficient_and_does_not_read_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MODE", "LIVE_AUTONOMOUS")
    client = FakeClient()
    executor = live_executor(tmp_path, client, FakeSchemaAdapter())
    result = executor.execute(plan(), live_inputs(), now=NOW)
    assert "LIVE_TRADING_DISABLED" in result.errors
    assert "ROBINHOOD_EXECUTION_DISABLED" in result.errors
    assert client.calls == []


@pytest.mark.parametrize(
    ("live_enabled", "robinhood_enabled", "token", "expected"),
    [
        (True, False, "human-token", "ROBINHOOD_EXECUTION_DISABLED"),
        (False, True, "human-token", "LIVE_TRADING_DISABLED"),
        (True, True, None, "LIVE_CONFIRMATION_INVALID"),
    ],
)
def test_each_live_gate_is_independently_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_enabled: bool,
    robinhood_enabled: bool,
    token: str | None,
    expected: str,
) -> None:
    monkeypatch.setattr(config, "MODE", "LIVE_AUTONOMOUS")
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", live_enabled)
    monkeypatch.setattr(config, "ROBINHOOD_EXECUTION_ENABLED", robinhood_enabled)
    monkeypatch.setattr(config, "LIVE_CONFIRMATION_EXPECTED_VALUE", "human-token")
    client = FakeClient()
    result = live_executor(tmp_path, client, FakeSchemaAdapter()).execute(
        plan(), live_inputs(confirmation_token=token), now=NOW
    )
    assert expected in result.errors
    assert client.calls == []


def test_schema_adapter_is_required_before_review_or_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_live_for_mock_test(monkeypatch)
    client = FakeClient()
    result = live_executor(tmp_path, client, None).execute(plan(), live_inputs(), now=NOW)
    assert result.errors == ("ORDER_SCHEMA_UNAVAILABLE",)
    assert "review_equity_order" not in client.calls
    assert "place_equity_order" not in client.calls


def test_mock_review_precedes_mock_submission_and_partial_fill_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_live_for_mock_test(monkeypatch)
    client = FakeClient(submission={
        "order_id": "safe-order-id", "state": "PARTIALLY_FILLED", "filled_quantity": 2
    })
    executor = live_executor(tmp_path, client, FakeSchemaAdapter())
    result = executor.execute(plan(), live_inputs(), now=NOW)
    assert result.status == "PARTIALLY_FILLED"
    assert client.calls.index("review_equity_order") < client.calls.index("place_equity_order")
    intent = executor.state_store.intents["trade-123"]
    assert intent.confirmed_filled_quantity == 2


def test_submission_timeout_is_unknown_and_not_blindly_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_live_for_mock_test(monkeypatch)
    client = FakeClient()
    client.place_error = TimeoutError("simulated")
    executor = live_executor(tmp_path, client, FakeSchemaAdapter())
    first = executor.execute(plan(), live_inputs(), now=NOW)
    assert first.status == "UNKNOWN"
    assert executor.state_store.intents["trade-123"].reconciliation_required is True
    client.place_error = None
    second = executor.execute(plan(), live_inputs(), now=NOW + timedelta(seconds=1))
    assert "RECONCILIATION_REQUIRED" in second.errors
    assert client.calls.count("place_equity_order") == 1


def test_manual_position_change_is_detected() -> None:
    store_intent = type("Intent", (), {
        "state": ExecutionState.FILLED,
        "robinhood_order_id": "safe-order-id",
    })()
    result = BrokerReconciler().reconcile(
        plan(),
        NormalizedBrokerState(
            accounts=[{"is_agentic_account": True, "state": "active", "account_reference": "opaque"}],
            portfolio={}, positions=[], orders=[], quote={}, tradability={},
        ),
        local_intent=store_intent,
    )
    assert result.manual_or_external_state_change is True
    assert "MANUAL_OR_EXTERNAL_STATE_CHANGE" in result.warnings


def test_exit_quantity_cannot_exceed_confirmed_position() -> None:
    assert cap_exit_quantity(10, 3.5) == 3.5
    request = FastPositionWatcher.evaluate(
        trade_id="trade-123", symbol="ACME", latest_price=97.5,
        stop_price=98.0, target_price=104.0, confirmed_position_quantity=3.5,
    )
    assert request is not None and request.requested_quantity == 3.5


@pytest.mark.parametrize(
    ("entry_state", "expected_action"),
    [
        (ExecutionState.REJECTED, "NO_POSITION"),
        (ExecutionState.CANCELED, "NO_POSITION"),
        (ExecutionState.SUBMITTED, "RECONCILE_BEFORE_EXIT"),
        (ExecutionState.UNKNOWN, "RECONCILE_BEFORE_EXIT"),
    ],
)
def test_protective_exit_waits_for_confirmed_fill(
    entry_state: str, expected_action: str
) -> None:
    decision = ProtectiveExitStateMachine.evaluate(
        trade_id="trade-123", symbol="ACME", entry_state=entry_state,
        confirmed_filled_quantity=0, broker_position_quantity=0,
        latest_price=97.0, quote_timestamp=NOW.isoformat(), stop_price=98,
        target_price=104, now=NOW, max_quote_age_seconds=120,
    )
    assert decision.action == expected_action
    assert decision.exit_request is None


def test_protective_exit_partial_fill_is_capped_to_broker_position() -> None:
    decision = ProtectiveExitStateMachine.evaluate(
        trade_id="trade-123", symbol="ACME",
        entry_state=ExecutionState.PARTIALLY_FILLED,
        confirmed_filled_quantity=4, broker_position_quantity=2.5,
        latest_price=97.0, quote_timestamp=NOW.isoformat(), stop_price=98,
        target_price=104, now=NOW, max_quote_age_seconds=120,
    )
    assert decision.action == "REQUEST_PROTECTIVE_EXIT"
    assert decision.exit_request is not None
    assert decision.exit_request.requested_quantity == 2.5


def test_protective_exit_stale_quote_and_manual_close_require_reconciliation() -> None:
    stale = ProtectiveExitStateMachine.evaluate(
        trade_id="trade-123", symbol="ACME", entry_state=ExecutionState.FILLED,
        confirmed_filled_quantity=4, broker_position_quantity=4,
        latest_price=97.0,
        quote_timestamp=(NOW - timedelta(minutes=5)).isoformat(),
        stop_price=98, target_price=104, now=NOW, max_quote_age_seconds=120,
    )
    assert stale.action == "STALE_PRICE_BLOCKED"
    manual = ProtectiveExitStateMachine.evaluate(
        trade_id="trade-123", symbol="ACME", entry_state=ExecutionState.FILLED,
        confirmed_filled_quantity=4, broker_position_quantity=0,
        latest_price=97.0, quote_timestamp=NOW.isoformat(),
        stop_price=98, target_price=104, now=NOW, max_quote_age_seconds=120,
    )
    assert "MANUAL_OR_EXTERNAL_STATE_CHANGE" in manual.warnings


def test_daily_loss_block_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = ExecutionStateStore(path)
    store.mark_daily_loss_blocked("2026-09-10", now=NOW)
    assert ExecutionStateStore(path).is_daily_loss_blocked("2026-09-10") is True


def test_execution_audit_matches_schema_and_contains_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_live_for_mock_test(monkeypatch)
    client = FakeClient()
    executor = live_executor(tmp_path, client, None)
    executor.execute(plan(), live_inputs(), now=NOW)
    event = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    schema = json.loads(Path("schemas/execution_audit.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(event))
    assert "human-token" not in json.dumps(event)


def test_dashboard_execution_projection_is_read_only(tmp_path: Path) -> None:
    kill = tmp_path / "kill.json"
    kill.write_text('{"trading_blocked": true}', encoding="utf-8")
    status = execution_status_snapshot(
        state_path=tmp_path / "missing-state.json",
        audit_path=tmp_path / "missing-audit.jsonl",
        kill_switch_path=kill,
    )
    assert status["trading_blocked"] is True
    assert status["controls"] == []
    assert not any(key.startswith("set_") or key.startswith("enable_") for key in status)
