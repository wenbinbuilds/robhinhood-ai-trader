"""Durable scalp setup state machine and structural duplicate protection.

An episode represents one market structure, not one classifier observation.
Soft entry failures leave it FORMING. Only a real structural reset can replace
it, and only an ENTERED/RESOLVED episode duplicate-protects that structure.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
import json

from strategies.scalp.models import ScalpEpisode
from watcher.storage import atomic_json


class ScalpSetupController:
    ACTIVE_STATES = frozenset({'FORMING', 'READY', 'ENTERED'})
    TERMINAL_STATES = frozenset({'INVALIDATED', 'EXPIRED', 'RESOLVED'})
    ALLOWED_TRANSITIONS = {
        'FORMING': frozenset({'READY', 'INVALIDATED', 'EXPIRED'}),
        'READY': frozenset({'FORMING', 'ENTERED', 'INVALIDATED', 'EXPIRED'}),
        'ENTERED': frozenset({'RESOLVED'}),
        'INVALIDATED': frozenset(),
        'EXPIRED': frozenset(),
        'RESOLVED': frozenset(),
    }

    def __init__(self, path, *, executed_episode_ids=()):
        self.path, self.lock = Path(path), RLock()
        self.active, self.closed, self.closed_details = {}, set(), {}
        self.executed_episode_ids = set(executed_episode_ids)
        self._load()

    @staticmethod
    def fingerprint_fields(setup_type, evidence_timestamp, features, bars=None):
        """Return the setup-specific fields used by the durable fingerprint."""

        def number(name, digits=6):
            value = getattr(features, name, None)
            return round(float(value), digits) if isinstance(value, (int, float)) else None

        completed = list(bars or [])
        prior_breakout_bars = completed[-6:-1]
        breakout_level = max(
            (float(row['high']) for row in prior_breakout_bars
             if isinstance(row.get('high'), (int, float))),
            default=number('recent_high'),
        )
        momentum_close = (
            round(float(completed[-1]['close']), 6)
            if completed and isinstance(completed[-1].get('close'), (int, float))
            else number('recent_high')
        )
        anchors = {
            'MICRO_BREAKOUT': ('PRIOR_COMPLETED_MICRO_HIGH', breakout_level),
            'MICRO_PULLBACK': ('EMA9_INTERACTION', number('ema9')),
            'EMA9_CONTINUATION': ('EMA9_TREND', number('ema9')),
            'VWAP_RECLAIM': ('VWAP_CROSS', number('vwap')),
            'MOMENTUM_BURST': ('LATEST_COMPLETED_CLOSE', momentum_close),
            'UNCLASSIFIED': ('LOCAL_RANGE', number('recent_high')),
        }
        anchor_type, anchor_price = anchors.get(
            setup_type, ('LOCAL_RANGE', number('recent_high'))
        )
        volume = number('volume_expansion')
        if volume is None:
            volume = number('breakout_volume_expansion')
        return {
            'setup_type': setup_type,
            'completed_bar_timestamp': evidence_timestamp,
            'anchor_type': anchor_type,
            'anchor_price': anchor_price,
            'ema9': number('ema9'),
            'ema20': number('ema20'),
            'vwap': number('vwap'),
            'local_high': number('recent_high'),
            'local_low': number('recent_low'),
            'volume_state': (
                'EXPANDED' if volume is not None and volume >= 1.2
                else 'NORMAL' if volume is not None else 'UNAVAILABLE'
            ),
            'volume_expansion': volume,
            'return_1': number('return_1'),
            'volume_acceleration': number('volume_acceleration'),
        }

    @classmethod
    def evidence_key(cls, symbol, setup_type, evidence_timestamp, features):
        fields = cls.fingerprint_fields(setup_type, evidence_timestamp, features)
        return f"{symbol.upper()}:" + json.dumps(
            fields, sort_keys=True, separators=(',', ':')
        )

    @staticmethod
    def _stamp(now):
        return now.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _transition(episode, state, now, reason):
        stamp = ScalpSetupController._stamp(now)
        history = (*episode.transition_history, {
            'timestamp': stamp, 'from': episode.state, 'to': state,
            'reason': reason,
        })
        terminal = state in ScalpSetupController.TERMINAL_STATES
        return replace(
            episode, state=state, last_updated_at=stamp,
            closed_at=stamp if terminal else episode.closed_at,
            close_reason=reason if terminal else episode.close_reason,
            stale_reason=(reason if state == 'RESOLVED' else None),
            transition_history=history,
        )

    @staticmethod
    def _structural_reset(previous, fields):
        """Return a setup-specific reset reason, never elapsed time alone."""

        old = previous.fingerprint_fields or previous.original_fingerprint_fields
        old_setup = old.get('setup_type', previous.setup_type)
        new_setup = fields.get('setup_type')
        price = fields.get('current_price')
        old_anchor = old.get('anchor_price')
        if old_setup == 'MICRO_BREAKOUT' and price is not None and old_anchor is not None:
            if price <= old_anchor:
                return 'BREAKOUT_PRICE_RETURNED_INSIDE_PRIOR_RANGE'
        if old_setup == 'MICRO_PULLBACK' and price and old.get('ema9'):
            if abs(price / old['ema9'] - 1) > .0015:
                return 'EMA_PULLBACK_INTERACTION_ENDED'
        if old_setup == 'EMA9_CONTINUATION' and price and old.get('ema9'):
            if price < old['ema9'] or (
                fields.get('ema9') is not None and fields.get('ema20') is not None
                and fields['ema9'] <= fields['ema20']
            ):
                return 'EMA_CONTINUATION_INVALIDATED'
        if old_setup == 'VWAP_RECLAIM' and price and old.get('vwap'):
            if price < old['vwap']:
                return 'VWAP_RECLAIM_LOST'
        if old_setup == 'MOMENTUM_BURST':
            if ((fields.get('return_1') or 0) <= .001
                    or (fields.get('volume_acceleration') or 0) <= 0):
                return 'MOMENTUM_BURST_ENDED'
        if old_setup == 'UNCLASSIFIED' and new_setup != 'UNCLASSIFIED':
            return 'NEW_CLASSIFIED_STRUCTURE'

        def materially_changed(name):
            before, after = old.get(name), fields.get(name)
            if before is None or after is None:
                return before != after
            tolerance = max(.01, abs(float(before)) * .0005)
            return abs(float(after) - float(before)) >= tolerance

        changed_levels = any(materially_changed(name) for name in (
            'anchor_price', 'local_high', 'local_low', 'ema9', 'ema20', 'vwap',
        ))
        new_bar = old.get('completed_bar_timestamp') != fields.get('completed_bar_timestamp')
        volume_event_changed = old.get('volume_state') != fields.get('volume_state')
        if new_bar and (changed_levels or volume_event_changed):
            return 'NEW_COMPLETED_BAR_STRUCTURE'
        if old_setup == new_setup and materially_changed('anchor_price'):
            return 'NEW_STRUCTURAL_REFERENCE_LEVEL'
        return None

    def _archive(self, episode):
        self.closed.add(episode.episode_id)
        self.closed_details[episode.episode_id] = asdict(episode)

    def _identity(self, symbol, key):
        base = 'SCALP-' + symbol.upper() + '-' + sha256(key.encode()).hexdigest()[:24]
        used = set(self.closed_details)
        used.update(item.episode_id for item in self.active.values())
        if base not in used:
            return base
        generation = 2
        while f'{base}-R{generation}' in used:
            generation += 1
        return f'{base}-R{generation}'

    def _recent_terminal(self, symbol):
        for detail in reversed(list(self.closed_details.values())):
            if detail.get('symbol') != symbol.upper():
                continue
            try:
                return self._from_dict(detail)
            except (TypeError, KeyError):
                continue
        return None

    @staticmethod
    def _from_dict(row):
        values = dict(row)
        values['evidence'] = tuple(values.get('evidence', ()))
        values['transition_history'] = tuple(values.get('transition_history', ()))
        allowed = ScalpEpisode.__dataclass_fields__
        return ScalpEpisode(**{key: value for key, value in values.items() if key in allowed})

    def episode(self, symbol, setup_type, evidence, evidence_timestamp, features, *, now,
                bars=None):
        fields = self.fingerprint_fields(
            setup_type, evidence_timestamp, features, bars=bars,
        )
        key = f"{symbol.upper()}:" + json.dumps(fields, sort_keys=True, separators=(',', ':'))
        observed = {**fields, 'current_price': getattr(features, 'price', None)}
        updated_at = self._stamp(now)
        with self.lock:
            current = self.active.get(symbol.upper())
            if current and current.structural_fingerprint == key:
                current = replace(
                    current, last_updated_at=updated_at,
                    fingerprint_fields=fields,
                    current_quote_price=observed.get('current_price'),
                    current_bar_timestamp=fields.get('completed_bar_timestamp'),
                    current_volume_expansion=fields.get('volume_expansion'),
                    structure_changed=False, new_episode_allowed=True,
                )
                self.active[symbol.upper()] = current
                return current

            previous = current or self._recent_terminal(symbol)
            reset_reason = self._structural_reset(previous, observed) if previous else None

            if current and reset_reason is None:
                # A classifier label can oscillate while the underlying range,
                # EMA/VWAP references, and completed bar remain the same.
                current = replace(
                    current, last_updated_at=updated_at,
                    structural_fingerprint=key, fingerprint_fields=fields,
                    current_quote_price=observed.get('current_price'),
                    current_bar_timestamp=fields.get('completed_bar_timestamp'),
                    current_volume_expansion=fields.get('volume_expansion'),
                    structure_changed=False, new_episode_allowed=True,
                )
                self.active[symbol.upper()] = current
                return current

            if (current is None and previous is not None
                    and previous.state in {'ENTERED', 'RESOLVED'}
                    and reset_reason is None):
                return replace(
                    previous, structural_fingerprint=key, fingerprint_fields=fields,
                    current_quote_price=observed.get('current_price'),
                    current_bar_timestamp=fields.get('completed_bar_timestamp'),
                    current_volume_expansion=fields.get('volume_expansion'),
                    structure_changed=False, new_episode_allowed=False,
                    stale_reason='SAME_STRUCTURE_AS_RESOLVED_EPISODE',
                    exact_block_reason='SAME_STRUCTURE_AS_RESOLVED_EPISODE',
                )

            if current:
                invalidated = self._transition(current, 'INVALIDATED', now, reset_reason)
                self._archive(invalidated)
                self.active.pop(symbol.upper(), None)

            identity = self._identity(symbol, key)
            episode = ScalpEpisode(
                strategy_id='SCALP', episode_id=identity, symbol=symbol.upper(),
                setup_type=setup_type, created_at=updated_at,
                evidence_timestamp=evidence_timestamp, evidence=tuple(evidence),
                state='FORMING', last_updated_at=updated_at,
                structural_fingerprint=key, initial_structural_fingerprint=key,
                structure_changed=bool(previous), new_episode_allowed=True,
                fingerprint_fields=fields, original_fingerprint_fields=dict(fields),
                current_quote_price=observed.get('current_price'),
                original_anchor_price=fields.get('anchor_price'),
                current_bar_timestamp=fields.get('completed_bar_timestamp'),
                original_bar_timestamp=fields.get('completed_bar_timestamp'),
                current_volume_expansion=fields.get('volume_expansion'),
                original_volume_expansion=fields.get('volume_expansion'),
                transition_history=({'timestamp': updated_at, 'from': None,
                                     'to': 'FORMING',
                                     'reason': reset_reason or 'STRUCTURE_DETECTED'},),
            )
            self.active[symbol.upper()] = episode
            self.save()
            return episode

    def record_latency_milestones(self, episode_id, symbol, milestones):
        """Persist each causal milestone once without rewriting state per quote."""

        observed = {
            str(name): str(value) for name, value in dict(milestones or {}).items()
            if value is not None
        }
        if not observed:
            with self.lock:
                return self.active.get(symbol.upper())
        with self.lock:
            current = self.active.get(symbol.upper())
            if current is None or current.episode_id != episode_id:
                return current
            merged = dict(current.latency_milestones)
            changed = False
            for name, value in observed.items():
                if not merged.get(name):
                    merged[name] = value
                    changed = True
            if changed:
                current = replace(current, latency_milestones=merged)
                self.active[symbol.upper()] = current
                self.save()
            return current

    def transition(self, episode_id, symbol, state, *, now, reason):
        if state not in self.ACTIVE_STATES | self.TERMINAL_STATES:
            raise ValueError(f'unsupported scalp episode state: {state}')
        with self.lock:
            current = self.active.get(symbol.upper())
            if current and current.episode_id == episode_id:
                if current.state == state:
                    return current
                if state not in self.ALLOWED_TRANSITIONS.get(current.state, ()):
                    raise ValueError(
                        f'invalid scalp episode transition: {current.state}->{state}'
                    )
                changed = self._transition(current, state, now, reason)
                if state in self.TERMINAL_STATES:
                    self._archive(changed)
                    self.active.pop(symbol.upper(), None)
                else:
                    self.active[symbol.upper()] = changed
                self.save()
                return changed
            return None

    def close(self, episode_id, symbol, *, now=None, reason='TRADE_COMPLETED'):
        current_time = now or datetime.now(timezone.utc)
        with self.lock:
            active = self.active.get(symbol.upper())
            if active and active.episode_id == episode_id:
                resolved = self._transition(active, 'RESOLVED', current_time, reason)
                self._archive(resolved)
                self.active.pop(symbol.upper(), None)
            elif episode_id in self.closed_details:
                old = self._from_dict(self.closed_details[episode_id])
                resolved = self._transition(old, 'RESOLVED', current_time, reason)
                self._archive(resolved)
            else:
                self.closed.add(episode_id)
            self.save()

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, ValueError):
            return
        if value.get('schema_version') not in {1, 2}:
            return
        self.closed = set(value.get('closed_episode_ids', []))
        for episode_id, row in value.get('closed_episode_details', {}).items():
            migrated = dict(row)
            if value.get('schema_version') == 1 and migrated.get('state') in {None, 'FORMING', 'CLOSED'}:
                migrated['state'] = (
                    'RESOLVED' if episode_id in self.executed_episode_ids else 'INVALIDATED'
                )
                migrated['close_reason'] = (
                    'LEGACY_TRADE_COMPLETED' if episode_id in self.executed_episode_ids
                    else 'LEGACY_STRUCTURE_SUPERSEDED'
                )
                migrated['stale_reason'] = None
            self.closed_details[episode_id] = migrated
        for row in value.get('active_episodes', []):
            try:
                episode = self._from_dict(row)
                self.active[episode.symbol] = episode
            except (TypeError, KeyError):
                continue

    def save(self):
        atomic_json(self.path, {
            'schema_version': 2,
            'active_episodes': [asdict(item) for item in self.active.values()],
            'closed_episode_ids': sorted(self.closed),
            'closed_episode_details': self.closed_details,
        })
