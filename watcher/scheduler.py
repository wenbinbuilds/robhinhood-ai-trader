"""Main-thread slow worker plus an independent deterministic watcher thread."""
from datetime import datetime, timedelta, timezone
from time import monotonic
from threading import Event
import config
from watcher.storage import atomic_json, event


def next_cycle_delay(started: float, finished: float, target: float, start_to_start=True) -> float:
    return max(0.0, started + target - finished) if start_to_start else target


class SlowMarketLoop:
    def __init__(self, cycle, *, watcher=None, has_positions=lambda: False,
                 status_path, stop_event=None, clock=None, timer=monotonic, events_path=None):
        self.cycle, self.watcher = cycle, watcher
        self.has_positions, self.status_path = has_positions, status_path
        self.stop_event = stop_event or Event()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timer = timer
        self.events_path = events_path

    def run(self):
        try:
            if self.watcher:
                self.watcher.start()
            while not self.stop_event.is_set():
                started, wall_start = self.timer(), self.clock()
                target = wall_start + timedelta(seconds=config.SLOW_CYCLE_TARGET_SECONDS)
                atomic_json(self.status_path, dict(status="RUNNING", cycle_started_at=wall_start.isoformat(),
                                                  next_cycle_target=target.isoformat()))
                code, result = self.cycle()
                finished = self.timer()
                delay = next_cycle_delay(started, finished, config.SLOW_CYCLE_TARGET_SECONDS,
                                         config.SLOW_LOOP_START_TO_START_SCHEDULING)
                timing = dict((result or {}).get("cycle_timing", {}))
                timing.update(cycle_started_at=wall_start.isoformat(), total_cycle_duration=finished-started,
                              next_cycle_target=(self.clock() + timedelta(seconds=delay)).isoformat(),
                              status="WAITING" if code == 0 else "RESEARCH_FAILED")
                for key in ("snapshot_duration", "reasoning_duration", "local_analysis_duration"):
                    timing.setdefault(key, None)
                atomic_json(self.status_path, timing)
                if self.events_path is not None:
                    event(self.events_path, "SLOW_CYCLE_TIMING", self.clock(), **timing)
                closed = (result or {}).get("market_context", {}).get("effective_regular_session") is False
                if not self.has_positions() and (code != 0 or closed):
                    return code
                # A research failure must NOT stop protection of an open position.
                self.stop_event.wait(delay)
        except KeyboardInterrupt:
            print("\nSlow and fast shadow loops stopped cleanly.")
        finally:
            self.stop_event.set()
            if self.watcher:
                self.watcher.stop()
            # Do not leave an old next-target timestamp implying another cycle
            # is scheduled after a clean shutdown.
            from watcher.status import read_object
            final = read_object(self.status_path)
            final.update(status="STOPPED", next_cycle_target=None)
            atomic_json(self.status_path, final)
        return 0
