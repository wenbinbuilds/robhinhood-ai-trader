"""Durable micro-episode identity; re-entry requires new structural evidence."""
from dataclasses import asdict, replace
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
        self.active, self.closed, self.closed_details = {}, set(), {}
        self._load()

    @staticmethod
    def evidence_key(symbol, setup_type, evidence_timestamp, features):
        structural = (round(features.recent_high or 0, 6), round(features.recent_low or 0, 6))
        return f'{symbol.upper()}:{setup_type}:{evidence_timestamp}:{structural}'

    def episode(self, symbol, setup_type, evidence, evidence_timestamp, features, *, now):
        key = self.evidence_key(symbol, setup_type, evidence_timestamp, features)
        identity = 'SCALP-' + symbol.upper() + '-' + sha256(key.encode()).hexdigest()[:24]
        updated_at = now.astimezone(timezone.utc).isoformat()
        with self.lock:
            current = self.active.get(symbol.upper())
            if current and current.episode_id == identity:
                current = replace(
                    current, last_updated_at=updated_at,
                    structural_fingerprint=key,
                    structure_changed=False, new_episode_allowed=True,
                )
                self.active[symbol.upper()] = current
                self.save()
                return current
            if identity in self.closed:
                detail = self.closed_details.get(identity, {})
                return ScalpEpisode('SCALP', identity, symbol.upper(), setup_type,
                                    detail.get('created_at', updated_at), evidence_timestamp,
                                    tuple(evidence), 'CLOSED',
                                    detail.get('last_updated_at', updated_at), key,
                                    detail.get('initial_structural_fingerprint', key),
                                    False, False, None,
                                    'EPISODE_ID_PREVIOUSLY_CLOSED')
            if current and current.episode_id != identity:
                self.closed.add(current.episode_id)
                self.closed_details[current.episode_id] = asdict(current)
            previous = current
            if previous is None:
                previous_detail = next((
                    detail for detail in reversed(list(self.closed_details.values()))
                    if detail.get('symbol') == symbol.upper()
                ), None)
                if previous_detail:
                    try:
                        previous = ScalpEpisode(**{
                            **previous_detail,
                            'evidence': tuple(previous_detail.get('evidence', ())),
                        })
                    except (TypeError, KeyError):
                        previous = None
            episode = ScalpEpisode('SCALP', identity, symbol.upper(), setup_type,
                                   updated_at, evidence_timestamp,
                                   tuple(evidence), 'FORMING', updated_at, key, key,
                                   bool(previous and previous.structural_fingerprint != key),
                                   True, None, None)
            self.active[symbol.upper()] = episode
            self.save()
            return episode

    def close(self, episode_id, symbol):
        with self.lock:
            self.closed.add(episode_id)
            current = self.active.get(symbol.upper())
            if current and current.episode_id == episode_id:
                self.closed_details[episode_id] = asdict(current)
                self.active.pop(symbol.upper(), None)
            self.save()

    def _load(self):
        try: value = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, ValueError): return
        if value.get('schema_version') != 1: return
        self.closed = set(value.get('closed_episode_ids', []))
        self.closed_details = dict(value.get('closed_episode_details', {}))
        for row in value.get('active_episodes', []):
            try:
                episode = ScalpEpisode(**{**row, 'evidence': tuple(row.get('evidence', ()))})
                self.active[episode.symbol] = episode
            except (TypeError, KeyError): continue

    def save(self):
        atomic_json(self.path, {'schema_version': 1,
            'active_episodes': [asdict(x) for x in self.active.values()],
            'closed_episode_ids': sorted(self.closed),
            'closed_episode_details': self.closed_details})
