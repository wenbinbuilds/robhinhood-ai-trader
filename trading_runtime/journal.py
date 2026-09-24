"""Append-only durable facts. Replay builds projections, never executes intents."""
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4
import json
import os


class RuntimeEventType(str, Enum):
    CANDIDATE_DISCOVERED = 'CANDIDATE_DISCOVERED'
    SLOW_ALPHA_READY = 'SLOW_ALPHA_READY'
    FAST_WATCH_ADMITTED = 'FAST_WATCH_ADMITTED'
    SETUP_FORMING = 'SETUP_FORMING'
    THRESHOLD_CONFIRMATION_UPDATED = 'THRESHOLD_CONFIRMATION_UPDATED'
    THRESHOLD_CROSSED = 'THRESHOLD_CROSSED'
    GEOMETRY_VALIDATED = 'GEOMETRY_VALIDATED'
    GEOMETRY_REJECTED = 'GEOMETRY_REJECTED'
    RISK_APPROVED = 'RISK_APPROVED'
    RISK_REJECTED = 'RISK_REJECTED'
    PORTFOLIO_APPROVED = 'PORTFOLIO_APPROVED'
    PORTFOLIO_REJECTED = 'PORTFOLIO_REJECTED'
    SHADOW_POSITION_OPENED = 'SHADOW_POSITION_OPENED'
    POSITION_CLOSED = 'POSITION_CLOSED'
    STATE_RECONCILED = 'STATE_RECONCILED'
    INFRASTRUCTURE_BLOCKED = 'INFRASTRUCTURE_BLOCKED'
    INFRASTRUCTURE_RECOVERED = 'INFRASTRUCTURE_RECOVERED'
    STOP_HIT = 'STOP_HIT'
    TARGET_HIT = 'TARGET_HIT'
    END_OF_DAY_EXIT = 'END_OF_DAY_EXIT'
    MISSED_EOD_RECOVERY_EXIT = 'MISSED_EOD_RECOVERY_EXIT'
    SCALP_TIME_EXIT = 'SCALP_TIME_EXIT'
    SCALP_RECOVERY_TIME_EXIT = 'SCALP_RECOVERY_TIME_EXIT'


@dataclass(frozen=True)
class RuntimeEvent:
    event: RuntimeEventType
    timestamp: str
    symbol: str
    episode_id: str
    research_cycle_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)


class EventJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self._ids = {row['event_id'] for row in self.read()}

    def read(self):
        if not self.path.exists():
            return []
        rows = []
        seen = set()
        with self.path.open() as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)  # Corruption is visible, never silent replay.
                if not isinstance(row, dict):
                    raise ValueError(
                        f"event journal line {line_number} is not an object"
                    )
                if 'event_id' not in row:
                    if self._legacy_closed_trade(row):
                        # Older deployments could append the closed-trade
                        # ledger shape to this path. The canonical portfolio
                        # outbox will recreate the corresponding event after
                        # startup; replaying the raw trade as an event would be
                        # ambiguous and unsafe.
                        continue
                    raise ValueError(
                        f"event journal line {line_number} has no event_id"
                    )
                if row['event_id'] not in seen:
                    rows.append(row)
                    seen.add(row['event_id'])
        return rows

    @staticmethod
    def _legacy_closed_trade(row):
        return all(row.get(name) for name in (
            'trade_id', 'episode_id', 'exit_timestamp', 'exit_reason',
        ))

    def validation(self, result):
        from trading_runtime.setup_controller import SetupController
        row = result.log_record
        identity = (row['timestamp'], row['symbol'], row.get('episode_id') or '', row.get('research_cycle_id'))
        if result.geometry_decision is not None:
            self.append(RuntimeEvent(RuntimeEventType.GEOMETRY_VALIDATED if result.geometry_decision.valid else RuntimeEventType.GEOMETRY_REJECTED,
                                     *identity, asdict(result.geometry_decision)))
        if result.risk_decision is not None:
            self.append(RuntimeEvent(RuntimeEventType.RISK_APPROVED if result.risk_decision.approved else RuntimeEventType.RISK_REJECTED,
                                     *identity, asdict(result.risk_decision)))
        if result.reason and SetupController.infrastructure_reason(result.reason):
            self.append(RuntimeEvent(RuntimeEventType.INFRASTRUCTURE_BLOCKED, *identity, dict(row)))

    def append(self, event: RuntimeEvent):
        row = asdict(event)
        row['event'] = event.event.value
        return self.append_row(row)

    def append_market_event(self, event):
        row = event.to_dict()
        row['event'] = row.pop('event_type')
        row['research_cycle_id'] = row.pop('cycle_id')
        return self.append_row(row)

    def append_row(self, row):
        if not isinstance(row, dict) or not row.get('event_id'):
            raise ValueError("event journal rows require event_id")
        with self.lock:
            if row['event_id'] in self._ids:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a') as handle:
                handle.write(json.dumps(row, allow_nan=False, sort_keys=True) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self._ids.add(row['event_id'])
            return True

    def replay(self):
        episodes, positions, closed = {}, {}, set()
        for row in self.read():
            episode = row['episode_id']
            kind = row['event']
            if kind == 'SHADOW_POSITION_OPENED' and episode not in closed:
                positions[episode] = row['payload']
            elif kind == 'POSITION_CLOSED':
                positions.pop(episode, None)
                closed.add(episode)
            episodes[episode] = row
        return dict(episodes=episodes, positions=positions, closed_episodes=sorted(closed))
