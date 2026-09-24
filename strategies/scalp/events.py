from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from trading_runtime.journal import EventJournal


EVENTS = {
    'ScalpCandidateDetected', 'ScalpSetupForming', 'ScalpEntryReady',
    'ScalpEntryBlocked', 'ScalpPositionOpened', 'ScalpStopHit',
    'ScalpTargetHit', 'ScalpMomentumExit', 'ScalpTimeExit',
    'ScalpPositionClosed',
    'ScalpEodExit', 'ScalpHardRiskExit', 'ScalpProfitProtectionExit',
    'ScalpPositionRecovered', 'ScalpPositionOverdue',
    'ScalpOverdueExitPending', 'ScalpRecoveryTimeExit',
}


class ScalpEventJournal:
    def __init__(self, path): self.journal = EventJournal(path)

    def emit(self, event, *, timestamp, symbol, episode_id, **payload):
        if event not in EVENTS: raise ValueError('unknown scalp event')
        row = {'event_id': uuid4().hex, 'event': event, 'strategy_id': 'SCALP',
               'episode_id': episode_id, 'symbol': symbol.upper(),
               'timestamp': timestamp.astimezone(timezone.utc).isoformat(),
               'payload': payload}
        self.journal.append_row(row)
        return row
