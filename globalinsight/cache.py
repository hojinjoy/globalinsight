"""Disk cache for every network call in the package.

Keyed by a hash of the URL (plus an optional cache-namespace suffix so the
same URL can be cached under a different TTL policy for different callers).
Layout: ``.cache/<sha256-of-key>.json``.

Concurrency: writes go through a single process-wide lock and land via a
write-to-temp-then-``os.replace`` so a reader never observes a half-written
file, and the bounded thread pool in filings.py can safely race on the cache.
This is not designed for multi-process safety (no file locking across OS
processes), only for the concurrent-threads-within-one-process case this
project actually has.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import CACHE_DIR

_lock = threading.Lock()


def _path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{digest}.json"


def get(key: str, ttl: float | None) -> Any | None:
    """Return the cached value for ``key``, or None on a miss or expiry.

    Args:
        key: Cache key - callers pass the URL (see http.py).
        ttl: Seconds after which the entry is considered stale. None means
            the entry never expires (used for immutable SEC filing documents).

    Returns:
        The previously cached JSON-serializable value, or None.
    """
    path = _path(key)
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if ttl is not None and (time.time() - payload.get("ts", 0)) > ttl:
        return None
    return payload.get("data")


def set(key: str, value: Any) -> Any:
    """Persist ``value`` under ``key`` and return it, for call-site chaining."""
    path = _path(key)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    payload = json.dumps({"ts": time.time(), "data": value})
    with _lock:
        tmp.write_text(payload)
        os.replace(tmp, path)
    return value


def age(key: str) -> float | None:
    """Seconds since ``key`` was written, or None if it isn't cached."""
    path = _path(key)
    try:
        return time.time() - json.loads(path.read_text()).get("ts", 0)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
