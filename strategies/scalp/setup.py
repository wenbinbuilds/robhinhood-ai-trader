"""Durable micro-episode identity; re-entry requires new structural evidence."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
import json

from strategies.scalp.models import ScalpEpisode
from watcher.storage import atomic_json


class ScalpSetupController:
    def __init__(self, path):
        self.path, self.lock = Path(path), RLock()
        self.active, self.closed = {}, set()
        self._load()

    @staticmethod
    def evidence_key(symbol, setup_type, evidence_timestamp, features):
        structural = (round(features.recent_high or 0, 6), round(features.recent_low or 0, 6))
        return f'{symbol.upper()}:{setup_type}:{evidence_timestamp}:{structural}'

    def episode(self, symbol, setup_type, evidence, evidence_timestamp, features, *, now):
        key = self.evidence_key(symbol, setup_type, evidence_timestamp, features)
        identity = 'SCALP-' + symbol.upper() + '-' + sha256(key.encode()).hexdigest()[:24]
        with self.lock:
            current = self.active.get(symbol.upper())
            if current and current.episode_id == identity:
                return current
            if identity in self.closed:
                return ScalpEpisode('SCALP', identity, symbol.upper(), setup_type,
                                    now.astimezone(timezone.utc).isoformat(), evidence_timestamp,
                                    tuple(evidence), 'CLOSED')
            if current and current.episode_id != identity:
                self.closed.add(current.episode_id)
            episode = ScalpEpisode('SCALP', identity, symbol.upper(), setup_type,
                                   now.astimezone(timezone.utc).isoformat(), evidence_timestamp,
                                   tuple(evidence), 'FORMING')
            self.active[symbol.upper()] = episode
            self.save()
            return episode

    def close(self, episode_id, symbol):
        with self.lock:
            self.closed.add(episode_id)
            current = self.active.get(symbol.upper())
            if current and current.episode_id == episode_id: self.active.pop(symbol.upper(), None)
            self.save()

    def _load(self):
        try: value = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, ValueError): return
        if value.get('schema_version') != 1: return
        self.closed = set(value.get('closed_episode_ids', []))
        for row in value.get('active_episodes', []):
            try:
                episode = ScalpEpisode(**{**row, 'evidence': tuple(row.get('evidence', ()))})
                self.active[episode.symbol] = episode
            except (TypeError, KeyError): continue

    def save(self):
        atomic_json(self.path, {'schema_version': 1,
            'active_episodes': [asdict(x) for x in self.active.values()],
            'closed_episode_ids': sorted(self.closed)})
