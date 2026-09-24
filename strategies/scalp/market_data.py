"""Read-only shared-universe cache, independent of momentum/LLM admission."""
from copy import deepcopy
from pathlib import Path
from threading import RLock
import json
from datetime import datetime, timedelta, timezone

import config


def _benchmark_feature(row):
    candles = row.get('candles', []) if isinstance(row, dict) else []
    valid = [item for item in candles if isinstance(item, dict)]
    closes = [item.get('close') for item in valid]
    result = closes[-1] / closes[-4] - 1 if (
        len(closes) >= 4 and isinstance(closes[-1], (int, float))
        and isinstance(closes[-4], (int, float)) and closes[-4]
    ) else None
    latest = valid[-1] if valid else {}
    try:
        began = datetime.fromisoformat(str(latest.get('begins_at')).replace('Z', '+00:00'))
        interval = float(latest.get('interval_seconds', 300))
        closed = (began.astimezone(timezone.utc) + timedelta(seconds=interval)).isoformat()
    except (TypeError, ValueError):
        closed = None
    return result, closed


class ScalpMarketDataCache:
    def __init__(self, snapshot_path, context_store=None):
        self.path, self.context_store = Path(snapshot_path), context_store
        self.lock, self._mtime, self._rows = RLock(), None, {}
        self._benchmarks = {}

    def _refresh(self):
        try: mtime = self.path.stat().st_mtime_ns
        except OSError: return
        if mtime == self._mtime: return
        try: value = json.loads(self.path.read_text())
        except (OSError, ValueError): return
        if not isinstance(value, dict): return
        # General rows are loaded first; the strategy-owned scalp row wins if
        # the same symbol appears in more than one snapshot section.
        rows = []
        for section in ('shadow_position_data', 'candidate_data', 'scalp_candidate_data'):
            section_rows = value.get(section, [])
            if isinstance(section_rows, list):
                rows.extend(section_rows)
        self._rows = {
            str(row.get('symbol','')).upper(): deepcopy(row)
            for row in rows if isinstance(row, dict) and row.get('symbol')
        }
        benchmark_rows = value.get('market', {}).get('benchmarks', [])
        self._benchmarks = {
            str(row.get('symbol', '')).upper(): deepcopy(row)
            for row in benchmark_rows
            if isinstance(row, dict) and row.get('symbol')
        } if isinstance(benchmark_rows, list) else {}
        spy_return, spy_at = _benchmark_feature(self._benchmarks.get('SPY', {}))
        qqq_return, qqq_at = _benchmark_feature(self._benchmarks.get('QQQ', {}))
        for row in self._rows.values():
            row['spy_return_3bar'] = spy_return
            row['qqq_return_3bar'] = qqq_return
            row['spy_feature_timestamp'] = spy_at
            row['qqq_feature_timestamp'] = qqq_at
        self._mtime = mtime

    def symbols(self):
        with self.lock:
            self._refresh()
            symbols = list(config.SCALP_DISCOVERY_SYMBOLS)
            symbols.extend(self._rows)
            if self.context_store is not None:
                with self.context_store.lock:
                    symbols.extend(self.context_store.contexts)
            return list(dict.fromkeys(str(symbol).upper() for symbol in symbols))[
                :config.SCALP_DISCOVERY_MAX_SYMBOLS
            ]

    def get(self, symbol):
        with self.lock:
            self._refresh()
            key = symbol.upper()
            result = deepcopy(self._rows.get(key, self._benchmarks.get(key, {})))
        # Slow context is optional background only. It fills absent values and
        # never makes stale quote/micro bars fresh.
        if self.context_store is not None:
            with self.context_store.lock:
                context = self.context_store.contexts.get(symbol.upper())
                background = context.pre_execution_market_data() if context else {}
            for name, value in background.items():
                if result.get(name) is None: result[name] = value
        return result

    @property
    def source(self):
        return config.SCALP_DISCOVERY_SOURCE
