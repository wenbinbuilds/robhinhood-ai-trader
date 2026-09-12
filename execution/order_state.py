"""Persistent idempotency and audit state for future live execution."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class ExecutionState(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEWED = "REVIEWED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = {
    ExecutionState.CLOSED,
    ExecutionState.REJECTED,
    ExecutionState.CANCELED,
}


@dataclass
class ExecutionIntent:
    trade_id: str
    symbol: str
    state: str
    created_at: str
    updated_at: str
    requested_quantity: int
    confirmed_filled_quantity: float = 0.0
    robinhood_order_id: str | None = None
    reconciliation_required: bool = False
    last_error: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionIntent:
        return cls(**{
            name: value[name]
            for name in cls.__dataclass_fields__
            if name in value
        })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutionStateStore:
    """Atomic local state; it never contains account identifiers or secrets."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.intents: dict[str, ExecutionIntent] = {}
        self.daily_loss_blocks: dict[str, bool] = {}
        self.updated_at: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise ValueError("live execution state is invalid")
        raw_intents = value.get("intents", [])
        raw_blocks = value.get("daily_loss_blocks", {})
        if not isinstance(raw_intents, list) or not isinstance(raw_blocks, Mapping):
            raise ValueError("live execution state is malformed")
        intents = [ExecutionIntent.from_dict(item) for item in raw_intents]
        if any(item.state not in set(ExecutionState) for item in intents):
            raise ValueError("live execution state contains an invalid transition state")
        self.intents = {item.trade_id: item for item in intents}
        self.daily_loss_blocks = {
            str(key): bool(value) for key, value in raw_blocks.items() if value is True
        }
        self.updated_at = value.get("updated_at") if isinstance(value.get("updated_at"), str) else None

    def create_intent(
        self, trade_id: str, symbol: str, quantity: int, *, now: datetime
    ) -> ExecutionIntent:
        existing = self.intents.get(trade_id)
        if existing is not None:
            return existing
        timestamp = now.astimezone(timezone.utc).isoformat()
        intent = ExecutionIntent(
            trade_id=trade_id,
            symbol=symbol.upper(),
            state=ExecutionState.CREATED,
            created_at=timestamp,
            updated_at=timestamp,
            requested_quantity=quantity,
        )
        self.intents[trade_id] = intent
        self.save(now)
        return intent

    def transition(
        self,
        trade_id: str,
        state: ExecutionState,
        *,
        now: datetime,
        robinhood_order_id: str | None = None,
        confirmed_filled_quantity: float | None = None,
        reconciliation_required: bool | None = None,
        last_error: str | None = None,
    ) -> ExecutionIntent:
        intent = self.intents[trade_id]
        if ExecutionState(intent.state) in TERMINAL_STATES and state != ExecutionState(intent.state):
            raise ValueError("terminal execution state cannot transition")
        intent.state = state
        intent.updated_at = now.astimezone(timezone.utc).isoformat()
        if robinhood_order_id is not None:
            intent.robinhood_order_id = robinhood_order_id
        if confirmed_filled_quantity is not None:
            if confirmed_filled_quantity < 0 or confirmed_filled_quantity > intent.requested_quantity:
                raise ValueError("confirmed fill exceeds requested quantity")
            intent.confirmed_filled_quantity = confirmed_filled_quantity
        if reconciliation_required is not None:
            intent.reconciliation_required = reconciliation_required
        intent.last_error = last_error
        self.save(now)
        return intent

    def has_unresolved_symbol(self, symbol: str) -> bool:
        return any(
            item.symbol == symbol.upper()
            and (
                item.reconciliation_required
                or ExecutionState(item.state) not in TERMINAL_STATES
            )
            for item in self.intents.values()
        )

    def mark_daily_loss_blocked(self, trading_date: str, *, now: datetime) -> None:
        self.daily_loss_blocks[trading_date] = True
        self.save(now)

    def is_daily_loss_blocked(self, trading_date: str) -> bool:
        return self.daily_loss_blocks.get(trading_date) is True

    def save(self, now: datetime) -> None:
        self.updated_at = now.astimezone(timezone.utc).isoformat()
        payload = {
            "schema_version": 1,
            "updated_at": self.updated_at,
            "daily_loss_blocks": self.daily_loss_blocks,
            "intents": [item.to_dict() for item in self.intents.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


SENSITIVE_KEYS = {
    "account_number", "account_id", "rhs_account_number", "rhc_account_number",
    "access_token", "refresh_token", "token", "cookie", "password", "secret",
    "oauth", "authorization", "confirmation_token", "email", "phone",
    "legal_name", "user_id", "person_id",
}


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not any(
                marker in str(key).lower()
                for marker in SENSITIVE_KEYS
            )
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


class ExecutionAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: Mapping[str, Any]) -> None:
        safe = _sanitize(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
