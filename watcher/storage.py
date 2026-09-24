"""Small atomic diagnostics and bounded JSONL activity feed."""
import json
import os
import tempfile
from pathlib import Path

import config


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def event(path: Path, kind: str, now, *, max_bytes=None, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = config.FAST_EVENT_LOG_MAX_BYTES if max_bytes is None else max_bytes
    if path.exists() and path.stat().st_size >= limit:
        os.replace(path, path.with_suffix(".jsonl.1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": now.isoformat(), "event": kind, **fields}) + "\n")
