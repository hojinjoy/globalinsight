"""Shared fixtures for the globalinsight test suite.

Hard constraints enforced here for every test in the suite:
- No real network access: httpx.get is replaced with a function that raises
  if anything ever tries to call it without having been explicitly
  monkeypatched by the test itself.
- No ANTHROPIC_API_KEY: explicitly removed from the environment.
- No shared on-disk cache pollution: cache.CACHE_DIR is redirected to a
  fresh tmp_path per test.
- No real time.sleep in the shared SEC rate limiter: the module-global
  token bucket in http.py is replaced with a huge-capacity one so ordinary
  tests never block on it; tests that specifically exercise the rate
  limiter construct and control their own _TokenBucket instance.
"""

import httpx
import pytest

from globalinsight import cache, http, store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test path reaches real httpx.get."""

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "real network access attempted via httpx.get() during a test - "
            "all HTTP must be faked at the http.py boundary"
        )

    monkeypatch.setattr(httpx, "get", _blocked)


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """This environment has no Anthropic credentials - keep it that way."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the disk cache to a throwaway directory for every test."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)


@pytest.fixture(autouse=True)
def isolated_ticker_store(tmp_path, monkeypatch):
    """Redirect the SQLite ticker store (store.py) to a throwaway DB file
    for every test - mirrors isolated_cache above, so tests never share
    ticker-store state (rows, soft deletes, refresh cooldown) with each
    other or with a real on-disk store.
    """
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tickers.db")


@pytest.fixture(autouse=True)
def fast_rate_limiter(monkeypatch):
    """Replace the shared SEC rate limiter with an effectively unlimited one.

    Tests that specifically exercise _TokenBucket accounting/backoff behavior
    build and drive their own bucket instance instead of relying on this.
    """
    monkeypatch.setattr(http, "_bucket", http._TokenBucket(rate=1e9, capacity=1e9))
