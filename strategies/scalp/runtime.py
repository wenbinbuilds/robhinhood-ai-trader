"""Wires entry and position controllers without any LLM dependency."""
import config
from strategies.scalp.execution import ScalpEntryController
from strategies.scalp.position import ScalpPositionController


class ScalpRuntime:
    def __init__(self, portfolio, data_lookup, *, universe=None,
                 setup_path=config.SCALP_STATE_PATH,
                 events_path=config.SCALP_EVENT_LOG_PATH):
        self.data_lookup = data_lookup
        self.universe = universe or (lambda: [])
        self.entry = ScalpEntryController(portfolio, setup_path=setup_path, events_path=events_path)
        self.positions = ScalpPositionController(
            portfolio, setup_controller=self.entry.setup_controller,
            events=self.entry.events, engine=self.entry.engine)

    def on_quotes(self, quotes, *, now):
        # Position exits run first. A just-closed episode is permanently closed;
        # entry then needs a genuinely different evidence key.
        exits = self.positions.process_quotes(quotes, self.data_lookup, now=now)
        entries = self.entry.on_quotes(quotes, self.data_lookup, now=now)
        return {'exits': exits, 'entries': entries}

    def symbols(self, now=None):
        return list(self.universe())
