"""Tests for globalinsight.cache: disk cache with TTL, no wall-clock reliance.

time.time() is monkeypatched throughout so TTL expiry is exercised via a
controlled fake clock, never by sleeping.
"""

from globalinsight import cache


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now


def test_set_then_get_roundtrips_value():
    cache.set("k1", {"hello": "world"})
    assert cache.get("k1", ttl=None) == {"hello": "world"}


def test_get_missing_key_returns_none():
    assert cache.get("does-not-exist", ttl=None) is None


def test_get_returns_none_on_corrupted_cache_file(monkeypatch):
    path = cache._path("bad-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert cache.get("bad-key", ttl=None) is None


def test_ttl_none_never_expires(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(cache.time, "time", clock.time)
    cache.set("permanent", "value")
    clock.now += 10_000_000  # ~115 days later
    assert cache.get("permanent", ttl=None) == "value"


def test_ttl_expiry_boundary(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(cache.time, "time", clock.time)
    cache.set("quote", 42)

    clock.now += 59
    assert cache.get("quote", ttl=60) == 42  # still fresh

    clock.now += 2  # total age now 61s > 60s ttl
    assert cache.get("quote", ttl=60) is None  # expired


def test_age_reflects_fake_clock_delta(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(cache.time, "time", clock.time)
    cache.set("aged", "x")
    clock.now += 42
    assert cache.age("aged") == 42


def test_age_returns_none_for_missing_key():
    assert cache.age("never-written") is None


def test_set_returns_value_for_call_site_chaining():
    result = cache.set("chain", [1, 2, 3])
    assert result == [1, 2, 3]


def test_different_keys_do_not_collide():
    cache.set("json:https://example.com/a", {"a": 1})
    cache.set("https://example.com/a", "raw-text")
    assert cache.get("json:https://example.com/a", ttl=None) == {"a": 1}
    assert cache.get("https://example.com/a", ttl=None) == "raw-text"
