"""Read-only execution status projection for dashboards and diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config
from execution.execution_guard import read_kill_switch
from execution.order_state import ExecutionStateStore, TERMINAL_STATES, ExecutionState


def execution_status_snapshot(
    *,
    state_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    kill_switch_path: str | Path | None = None,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """Return display-only state; this module intentionally has no mutations."""

    kill = read_kill_switch(kill_switch_path or config.LIVE_KILL_SWITCH_PATH)
    try:
        store = ExecutionStateStore(state_path or config.LIVE_EXECUTION_STATE_PATH)
        pending = [
            item.to_dict() for item in store.intents.values()
            if ExecutionState(item.state) not in TERMINAL_STATES
        ]
        state_status = "AVAILABLE"
    except (OSError, ValueError, json.JSONDecodeError):
        pending = []
        state_status = "INVALID"
    events: list[dict[str, Any]] = []
    last_reconciliation_time: str | None = None
    path = Path(audit_path or config.EXECUTION_AUDIT_LOG_PATH)
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-recent_limit:]:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
                    if event.get("broker_reconciliation_result") is not None:
                        last_reconciliation_time = str(event.get("timestamp"))
    except (OSError, json.JSONDecodeError):
        events = []
    return {
        "mode": config.MODE,
        "live_trading_enabled": config.LIVE_TRADING_ENABLED,
        "robinhood_execution_enabled": config.ROBINHOOD_EXECUTION_ENABLED,
        "kill_switch": kill.status,
        "trading_blocked": kill.trading_blocked,
        "execution_state_status": state_status,
        "last_reconciliation_time": last_reconciliation_time,
        "pending_execution_states": pending,
        "recent_execution_audit_events": events,
        "controls": [],
    }
