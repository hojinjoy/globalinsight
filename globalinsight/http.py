"""The only module in this package allowed to make network calls.

Everything else (edgar.py, filings.py, financials.py, quote.py) must go
through ``get_text`` / ``get_json`` here, so the rest of the package can be
unit-tested with fakes instead of real HTTP.

Two independent concerns are handled centrally:

- A shared token-bucket rate limiter, because SEC's 10 req/sec limit is a
  global ceiling, not a per-thread one. A per-call ``time.sleep(0.1)`` looks
  right in isolation but under-throttles the moment two threads are in
  flight at once (e.g. the bounded 8-K fetch pool in filings.py) - each
  thread sleeps 0.1s independently and the *combined* rate blows past
  10/sec. A single bucket shared across every thread is the only way to cap
  the actual combined rate.
- Retries with backoff on 429/5xx, since SEC and Yahoo both do this under
  load and a naive client would surface a transient hiccup as a hard failure.
"""

import threading
import time
from typing import Any

import httpx

from . import cache
from .config import SEC_RATE_PER_SEC, SEC_USER_AGENT

_DEFAULT_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF = 0.5


class _TokenBucket:
    """Shared rate limiter: at most ``rate`` tokens/second, refilled continuously.

    Deliberately not a per-call sleep. A sleep-based throttle only bounds the
    rate of the thread that calls it; with N concurrent threads each doing
    their own sleep(1/rate), the *combined* request rate is N times too fast.
    A single bucket object, guarded by one lock and shared by every caller
    regardless of thread, is what actually keeps the combined rate under the
    ceiling.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = rate
        self._capacity = capacity if capacity is not None else rate
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            time.sleep(wait)


_bucket = _TokenBucket(SEC_RATE_PER_SEC)


def _fetch_text(url: str, headers: dict[str, str]) -> str:
    backoff = _BASE_BACKOFF
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        _bucket.acquire()
        try:
            response = httpx.get(
                url, headers=headers, timeout=60.0, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if response.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2
            continue

        response.raise_for_status()
        return response.text
    raise last_exc if last_exc else RuntimeError(f"unreachable: {url}")


def get_text(
    url: str,
    *,
    ttl: float | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch ``url`` as text, through the cache and the shared rate limiter.

    Args:
        url: The URL to fetch. Also used as the cache key.
        ttl: Cache TTL in seconds; None caches forever (use for anything
            under /Archives/, which is immutable once filed).
        headers: Overrides the default SEC headers - e.g. quote.py passes a
            browser-like User-Agent for Yahoo Finance, which does not want
            (and may penalize) the SEC contact-address header.

    Returns:
        The response body as text.
    """
    cached = cache.get(url, ttl)
    if cached is not None:
        return cached
    text = _fetch_text(url, headers or _DEFAULT_HEADERS)
    return cache.set(url, text)


def get_json(
    url: str,
    *,
    ttl: float | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Fetch ``url`` and parse it as JSON, through the cache and rate limiter.

    The parsed object (not the raw text) is what's cached, so repeated reads
    don't pay a re-parse cost.
    """
    cache_key = f"json:{url}"
    cached = cache.get(cache_key, ttl)
    if cached is not None:
        return cached
    text = _fetch_text(url, headers or _DEFAULT_HEADERS)
    import json

    data = json.loads(text)
    return cache.set(cache_key, data)
