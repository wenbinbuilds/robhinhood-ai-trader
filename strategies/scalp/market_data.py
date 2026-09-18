"""Read-only scanner snapshot cache, independent of momentum/LLM admission."""
from pathlib import Path
from threading import RLock
import json


class ScalpMarketDataCache:
    def __init__(self, snapshot_path, context_store=None):
        self.path, self.context_store = Path(snapshot_path), context_store
        self.lock, self._mtime, self._rows = RLock(), None, {}

    def _refresh(self):
        try: mtime = self.path.stat().st_mtime_ns
        except OSError: return
        if mtime == self._mtime: return
        try: value = json.loads(self.path.read_text())
        except (OSError, ValueError): return
        rows = value.get('candidate_data', []) if isinstance(value, dict) else []
        if not isinstance(rows, list): return
        self._rows = {str(row.get('symbol','')).upper(): dict(row)
                      for row in rows if isinstance(row, dict) and row.get('symbol')}
        self._mtime = mtime

    def symbols(self):
        with self.lock: self._refresh(); return sorted(self._rows)

    def get(self, symbol):
        with self.lock: self._refresh(); result = dict(self._rows.get(symbol.upper(), {}))
        # Slow context is optional background only. It fills absent values and
        # never makes stale quote/micro bars fresh.
        if self.context_store is not None:
            with self.context_store.lock:
                context = self.context_store.contexts.get(symbol.upper())
                background = context.pre_execution_market_data() if context else {}
            for name, value in background.items():
                if result.get(name) is None: result[name] = value
        return result
