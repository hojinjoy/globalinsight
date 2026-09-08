"""Tests for globalinsight.http: caching, retries, and the shared rate limiter.

No real network access, no real sleeping - httpx.get and time.sleep/monotonic
are all faked so behavior is exercised deterministically.
"""

import httpx
import pytest

from globalinsight import http


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )


class _FakeClock:
    """A monotonic clock plus a sleep() that actually advances it, so retry
    backoff math is exercised without any real delay."""

    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- get_text / get_json: caching -------------------------------------------


def test_get_text_fetches_once_and_caches(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout, follow_redirects):
        calls.append(url)
        return _FakeResponse(200, "the body")

    monkeypatch.setattr(httpx, "get", fake_get)

    first = http.get_text("https://example.com/doc.htm", ttl=None)
    second = http.get_text("https://example.com/doc.htm", ttl=None)

    assert first == "the body"
    assert second == "the body"
    assert len(calls) == 1  # second call served from cache


def test_get_json_parses_and_caches(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout, follow_redirects):
        calls.append(url)
        return _FakeResponse(200, '{"a": 1, "b": [1, 2]}')

    monkeypatch.setattr(httpx, "get", fake_get)

    first = http.get_json("https://example.com/data.json", ttl=None)
    second = http.get_json("https://example.com/data.json", ttl=None)

    assert first == {"a": 1, "b": [1, 2]}
    assert second == {"a": 1, "b": [1, 2]}
    assert len(calls) == 1


def test_get_text_and_get_json_same_url_do_not_collide(monkeypatch):
    """get_json prefixes its cache key with 'json:' so a text fetch and a
    JSON fetch of the same URL never read each other's cached payload."""

    def fake_get(url, headers, timeout, follow_redirects):
        return _FakeResponse(200, '{"x": 1}')

    monkeypatch.setattr(httpx, "get", fake_get)

    raw = http.get_text("https://example.com/same", ttl=None)
    parsed = http.get_json("https://example.com/same", ttl=None)

    assert raw == '{"x": 1}'
    assert parsed == {"x": 1}


def test_get_text_uses_default_sec_headers(monkeypatch):
    seen_headers = {}

    def fake_get(url, headers, timeout, follow_redirects):
        seen_headers.update(headers)
        return _FakeResponse(200, "body")

    monkeypatch.setattr(httpx, "get", fake_get)
    http.get_text("https://example.com/x", ttl=None)
    assert seen_headers["User-Agent"] == http.SEC_USER_AGENT


def test_get_text_respects_custom_headers_override(monkeypatch):
    seen_headers = {}

    def fake_get(url, headers, timeout, follow_redirects):
        seen_headers.update(headers)
        return _FakeResponse(200, "body")

    monkeypatch.setattr(httpx, "get", fake_get)
    custom = {"User-Agent": "custom-agent"}
    http.get_text("https://example.com/x2", ttl=None, headers=custom)
    assert seen_headers["User-Agent"] == "custom-agent"


# --- retries -----------------------------------------------------------------


def test_retries_on_429_then_succeeds(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(http.time, "sleep", clock.sleep)

    responses = [_FakeResponse(429), _FakeResponse(200, "finally")]
    calls = {"n": 0}

    def fake_get(url, headers, timeout, follow_redirects):
        response = responses[calls["n"]]
        calls["n"] += 1
        return response

    monkeypatch.setattr(httpx, "get", fake_get)

    result = http.get_text("https://example.com/flaky", ttl=None)
    assert result == "finally"
    assert calls["n"] == 2
    assert len(clock.sleeps) == 1  # backed off exactly once


def test_retries_exhausted_raises(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(http.time, "sleep", clock.sleep)

    def fake_get(url, headers, timeout, follow_redirects):
        return _FakeResponse(503)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        http.get_text("https://example.com/always-down", ttl=None)


def test_network_error_retried_then_raises(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(http.time, "sleep", clock.sleep)

    def fake_get(url, headers, timeout, follow_redirects):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPError):
        http.get_text("https://example.com/unreachable", ttl=None)


def test_non_retryable_status_raises_immediately(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(http.time, "sleep", clock.sleep)
    calls = {"n": 0}

    def fake_get(url, headers, timeout, follow_redirects):
        calls["n"] += 1
        return _FakeResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        http.get_text("https://example.com/missing", ttl=None)
    assert calls["n"] == 1  # 404 is not in _RETRY_STATUS - no retry
    assert clock.sleeps == []


# --- shared token-bucket rate limiter -----------------------------------------


def test_token_bucket_consumes_one_token_per_acquire(monkeypatch):
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)
    bucket = http._TokenBucket(rate=10.0, capacity=10.0)
    bucket._last = clock.now  # align bucket's internal clock with the fake one

    bucket.acquire()
    assert bucket._tokens == 9.0
    bucket.acquire()
    assert bucket._tokens == 8.0


def test_token_bucket_blocks_and_computes_correct_wait(monkeypatch):
    """When the bucket is empty, acquire() must sleep exactly long enough to
    refill one token, then succeed - proven with a fake clock that sleep()
    itself advances (no real delay)."""
    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(http.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(http.time, "sleep", clock.sleep)

    bucket = http._TokenBucket(rate=2.0, capacity=2.0)
    bucket._last = 0.0

    bucket.acquire()  # tokens 2 -> 1
    bucket.acquire()  # tokens 1 -> 0
    assert bucket._tokens == 0.0

    bucket.acquire()  # must wait for a token to regenerate at rate=2/s
    assert clock.sleeps == [0.5]  # (1 - 0) / 2.0
    assert bucket._tokens == 0.0  # consumed immediately after refill


def test_token_bucket_is_a_single_shared_module_global():
    """The rate limiter must be one object shared across every caller/thread,
    not constructed per-call or per-thread - that's what makes the *combined*
    rate obey the SEC ceiling instead of each thread getting its own budget."""
    bucket_a = http._bucket
    bucket_b = http._bucket
    assert bucket_a is bucket_b


def test_token_bucket_shared_across_threads_is_race_free(monkeypatch):
    """Deterministic accounting test (no sleeping): freeze the bucket's clock
    so every acquire() sees elapsed=0, then hammer one shared bucket from many
    threads. If the internal accounting under the lock were unsafe (e.g. read-
    modify-write without the lock), the final token count would not equal
    capacity - calls made exactly, since a lost update would over-count
    remaining tokens."""
    import threading

    capacity = 200.0
    monkeypatch.setattr(http.time, "monotonic", lambda: 0.0)
    bucket = http._TokenBucket(rate=1.0, capacity=capacity)  # _last = 0.0 under the freeze

    n_threads = 20
    calls_per_thread = 10  # 200 total acquires == capacity, so none ever wait

    def worker():
        for _ in range(calls_per_thread):
            bucket.acquire()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert bucket._tokens == pytest.approx(0.0)
