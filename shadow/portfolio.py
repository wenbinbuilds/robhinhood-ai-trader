"""Atomic local shadow-portfolio persistence; no broker integration."""

from __future__ import annotations

import json
import os
import tempfile
from threading import RLock
from functools import wraps
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import config
from jsonschema import Draft202012Validator
from shadow.models import ShadowPosition, ShadowState, ShadowTrade


def synchronized(method):
    """Short local transactions only; never hold this lock across provider/LLM IO."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        lock = self.lock if isinstance(self, ShadowPortfolio) else self.portfolio.lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapped


class ShadowPortfolio:
    def __init__(
        self,
        state_path: str | Path = "state/shadow_portfolio.json",
        trades_path: str | Path = "state/shadow_trades.jsonl",
    ) -> None:
        self.state_path = Path(state_path)
        self.lock = RLock()
        self.trades_path = Path(trades_path)
        self._initialize_trade_history()
        self.state = self._load()
        from trading_runtime.journal import EventJournal
        self.journal = EventJournal(self.state_path.with_suffix('.events.jsonl'))
        self.recover_audit()

    def recover_audit(self):
        """Canonical records are an outbox; mirror missing facts, never execute."""
        from trading_runtime.journal import RuntimeEvent, RuntimeEventType
        for position in self.state.open_positions:
            self.journal.append(RuntimeEvent(
                RuntimeEventType.SHADOW_POSITION_OPENED, position.entry_timestamp,
                position.symbol, position.episode_id, position.research_cycle_id,
                position.to_dict(), event_id=position.entry_intent_id))
        for trade in self.state.closed_positions:
            payload = trade.to_dict()
            payload.update(position_id=trade.trade_id, entry_time=trade.entry_timestamp,
                           exit_time=trade.exit_timestamp, stop_at_exit=trade.stop,
                           target_at_exit=trade.target, realized_pnl=trade.net_pnl,
                           holding_seconds=trade.holding_time_minutes * 60)
            self.journal.append(RuntimeEvent(
                RuntimeEventType.POSITION_CLOSED, trade.exit_timestamp,
                trade.symbol, trade.episode_id, trade.research_cycle_id,
                payload, event_id=trade.exit_intent_id))
            if trade.exit_reason in RuntimeEventType._value2member_map_:
                self.journal.append(RuntimeEvent(RuntimeEventType(trade.exit_reason),
                    trade.exit_timestamp, trade.symbol, trade.episode_id, trade.research_cycle_id,
                    payload, event_id='reason:' + trade.exit_intent_id))
            self._append_trade(trade)

    def _initialize_trade_history(self) -> None:
        """Create the append-only trade log without truncating existing data."""

        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.trades_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.close(descriptor)

    def _new_state(self) -> ShadowState:
        capital = float(config.SHADOW_STARTING_CAPITAL)
        return ShadowState(
            schema_version=1,
            starting_capital=capital,
            cash=capital,
            equity=capital,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            daily_pnl=0.0,
            trading_date=None,
            trades_today=0,
            peak_equity=capital,
            maximum_drawdown=0.0,
            maximum_drawdown_percent=0.0,
        )

    def _load(self) -> ShadowState:
        if not self.state_path.exists():
            return self._new_state()
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "shadow_portfolio.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise ValueError(f"shadow portfolio schema violation: {errors[0].message}")
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise ValueError("shadow portfolio state is invalid or unsupported")
        return ShadowState(
            schema_version=1,
            starting_capital=float(value["starting_capital"]),
            cash=float(value["cash"]),
            equity=float(value["equity"]),
            realized_pnl=float(value["realized_pnl"]),
            unrealized_pnl=float(value["unrealized_pnl"]),
            daily_pnl=float(value["daily_pnl"]),
            trading_date=value.get("trading_date"),
            trades_today=int(value.get("trades_today", 0)),
            peak_equity=float(value.get("peak_equity", value["equity"])),
            maximum_drawdown=float(value.get("maximum_drawdown", 0)),
            maximum_drawdown_percent=float(value.get("maximum_drawdown_percent", 0)),
            open_positions=[ShadowPosition.from_dict(item) for item in value.get("open_positions", [])],
            closed_positions=[ShadowTrade.from_dict(item) for item in value.get("closed_positions", [])],
            benchmark_session=dict(value.get("benchmark_session", {})),
            updated_at=value.get("updated_at"),
        )

    @synchronized
    def snapshot(self) -> ShadowState:
        return deepcopy(self.state)

    @synchronized
    def begin_cycle(
        self,
        now: datetime,
        benchmarks: list[Mapping[str, object]],
        *,
        is_regular_session: bool,
    ) -> None:
        # Closed-market cycles are context-only. They must not roll daily limits
        # or establish a benchmark from an after-hours quote. Requiring a
        # confirmed open cycle also makes weekends and holidays fail closed.
        if not is_regular_session:
            return
        trading_date = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE)).date().isoformat()
        if self.state.trading_date != trading_date:
            self.state.trading_date = trading_date
            self.state.trades_today = 0
            self.state.daily_pnl = 0.0
            self.state.benchmark_session = {}
        for row in benchmarks:
            symbol = str(row.get("symbol", "")).upper()
            price = row.get("current_price")
            if symbol in {"SPY", "QQQ"} and isinstance(price, (int, float)):
                entry = self.state.benchmark_session.setdefault(
                    symbol, {"reference_price": float(price), "current_price": float(price)}
                )
                entry["current_price"] = float(price)

    @synchronized
    def has_symbol(self, symbol: str) -> bool:
        return any(item.symbol == symbol.upper() for item in self.state.open_positions)

    @synchronized
    def add_position(self, position: ShadowPosition) -> None:
        if self.has_symbol(position.symbol):
            raise ValueError("DUPLICATE_POSITION")
        if any(p.episode_id == position.episode_id for p in self.state.open_positions + self.state.closed_positions):
            raise ValueError('EPISODE_ALREADY_EXECUTED')
        previous = deepcopy(self.state)
        self.state.open_positions.append(position)
        self.state.cash = round(self.state.cash - position.notional_value, 4)
        self.state.trades_today += 1
        self.revalue()
        try:
            self.save()
        except Exception:
            self.state = previous
            raise
        self.recover_audit()

    @synchronized
    def close_position(self, trade_id: str, trade: ShadowTrade) -> None:
        position = next((item for item in self.state.open_positions if item.trade_id == trade_id), None)
        if position is None:
            return  # Idempotent second exit; never credit cash twice.
        previous = deepcopy(self.state)
        self.state.open_positions.remove(position)
        self.state.closed_positions.append(trade)
        self.state.cash = round(self.state.cash + trade.exit_price * trade.quantity, 4)
        self.state.realized_pnl = round(self.state.realized_pnl + trade.net_pnl, 4)
        self.state.daily_pnl = round(self.state.daily_pnl + trade.net_pnl, 4)
        self.revalue()
        try:
            self.save()
        except Exception:
            self.state = previous
            raise
        self.recover_audit()

    @synchronized
    def revalue(self, marks: Mapping[str, float] | None = None) -> None:
        marks = marks or {}
        unrealized = 0.0
        market_value = 0.0
        for position in self.state.open_positions:
            mark = marks.get(position.symbol, position.last_price or position.entry_price)
            position.last_price = float(mark)
            position.unrealized_pnl = round((float(mark) - position.entry_price) * position.quantity, 4)
            position.maximum_favorable_excursion = max(
                position.maximum_favorable_excursion, position.unrealized_pnl,
            )
            position.maximum_adverse_excursion = min(
                position.maximum_adverse_excursion, position.unrealized_pnl,
            )
            unrealized += position.unrealized_pnl
            market_value += float(mark) * position.quantity
        self.state.unrealized_pnl = round(unrealized, 4)
        self.state.equity = round(self.state.cash + market_value, 4)
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
        drawdown = max(0.0, self.state.peak_equity - self.state.equity)
        self.state.maximum_drawdown = max(self.state.maximum_drawdown, round(drawdown, 4))
        percent = drawdown / self.state.peak_equity if self.state.peak_equity else 0.0
        self.state.maximum_drawdown_percent = max(
            self.state.maximum_drawdown_percent, round(percent, 6)
        )

    @synchronized
    def save(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.state.updated_at = current.astimezone(timezone.utc).isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp", dir=self.state_path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.state.to_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _append_trade(self, trade: ShadowTrade) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        if self.trades_path.exists():
            with self.trades_path.open() as existing:
                if any(json.loads(line).get('trade_id') == trade.trade_id for line in existing if line.strip()):
                    return
        with self.trades_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trade.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
