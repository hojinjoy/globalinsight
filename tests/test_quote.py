"""Tests for globalinsight.quote: Yahoo Finance quote lookup.

Every failure mode must resolve to Quote(available=False, error=...) rather
than raising - Yahoo's frequent 429s must never blank the rest of the brief.
"""

import httpx
import pytest

from globalinsight import config, http
from globalinsight import quote as quote_module
from globalinsight.quote import _get_crumb as _real_get_crumb
from globalinsight.quote import get_quote

# Real crumb behavior (network + disk-cache) is exercised by the dedicated
# _get_crumb tests below; every other test fakes it out so the suite never
# makes a real request to Yahoo just to authenticate the summary call.
_FAKE_CRUMB = ("test-crumb", "A3=test-cookie")


@pytest.fixture(autouse=True)
def _fake_crumb(monkeypatch):
    monkeypatch.setattr(quote_module, "_get_crumb", lambda force_refresh=False: _FAKE_CRUMB)


def _chart_payload(
    price=131.26,
    prev_close=128.99,
    week_low=86.62,
    week_high=153.13,
    currency="USD",
    market_time=1_700_000_000,
):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": price,
                        "chartPreviousClose": prev_close,
                        "fiftyTwoWeekLow": week_low,
                        "fiftyTwoWeekHigh": week_high,
                        "currency": currency,
                        "regularMarketTime": market_time,
                    }
                }
            ]
        }
    }


def _summary_payload(market_cap=3_200_000_000_000, pe_ratio=55.4):
    return {
        "quoteSummary": {
            "result": [
                {
                    "price": {"marketCap": {"raw": market_cap}},
                    "summaryDetail": {"trailingPE": {"raw": pe_ratio}},
                }
            ]
        }
    }


def test_successful_quote_full_fields(monkeypatch):
    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return _chart_payload()
        return _summary_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("nvda")
    assert q.ticker == "NVDA"
    assert q.available is True
    assert q.error is None
    assert q.price == 131.26
    assert q.change == 131.26 - 128.99
    assert q.change_percent == (131.26 - 128.99) / 128.99 * 100
    assert q.market_cap == 3_200_000_000_000
    assert q.pe_ratio == 55.4
    assert q.week52_low == 86.62
    assert q.week52_high == 153.13
    assert q.currency == "USD"
    assert q.as_of == "2023-11-14T22:13:20+00:00"  # deterministic from fixed timestamp


def test_chart_failure_returns_unavailable_not_raise(monkeypatch):
    """The empirically-confirmed Yahoo 429 case: must degrade cleanly."""

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(http, "get_json", boom)

    q = get_quote("NVDA")
    assert q.available is False
    assert q.price is None
    assert "429" in q.error


def test_malformed_chart_payload_returns_unavailable(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda *a, **k: {"chart": {"result": []}})
    q = get_quote("NVDA")
    assert q.available is False
    assert q.error is not None


def test_summary_failure_degrades_only_market_cap_and_pe(monkeypatch):
    """Market cap / P-E live on a flakier endpoint; a failure there must cost
    only those two fields, never the price already fetched from chart."""

    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return _chart_payload()
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("NVDA")
    assert q.available is True
    assert q.price == 131.26
    assert q.market_cap is None
    assert q.pe_ratio is None


def test_previous_close_falls_back_to_previousClose_key(monkeypatch):
    payload = _chart_payload()
    del payload["chart"]["result"][0]["meta"]["chartPreviousClose"]
    payload["chart"]["result"][0]["meta"]["previousClose"] = 100.0
    payload["chart"]["result"][0]["meta"]["regularMarketPrice"] = 105.0

    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return payload
        return _summary_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)
    q = get_quote("NVDA")
    assert q.change == 5.0


def test_missing_market_time_leaves_as_of_none(monkeypatch):
    payload = _chart_payload()
    del payload["chart"]["result"][0]["meta"]["regularMarketTime"]

    monkeypatch.setattr(
        http,
        "get_json",
        lambda url, ttl=None, headers=None: payload if "chart" in url else _summary_payload(),
    )
    q = get_quote("NVDA")
    assert q.as_of is None


def test_ticker_is_uppercased_even_on_failure(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    q = get_quote("nvda")
    assert q.ticker == "NVDA"


def test_uses_quote_ttl(monkeypatch):
    seen_ttls = []

    def fake_get_json(url, ttl=None, headers=None):
        seen_ttls.append(ttl)
        return _chart_payload() if "chart" in url else _summary_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)
    get_quote("NVDA")
    assert seen_ttls == [config.TTL_QUOTE, config.TTL_QUOTE]


def test_uses_browser_like_headers_not_sec_headers(monkeypatch):
    """Yahoo isn't an SEC endpoint - it should get a browser-like UA, not the
    SEC contact-address header (which would look like a bot signature)."""
    seen_headers = []

    def fake_get_json(url, ttl=None, headers=None):
        seen_headers.append(headers)
        return _chart_payload() if "chart" in url else _summary_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)
    get_quote("NVDA")
    for headers in seen_headers:
        assert headers["User-Agent"] != config.SEC_USER_AGENT
        assert "Mozilla" in headers["User-Agent"]


# --- H3: the whole body must be guarded, not just the chart fetch -----------


def test_malformed_timestamp_returns_unavailable(monkeypatch):
    """An out-of-range market_time raises OverflowError from
    datetime.fromtimestamp - that must degrade to available=False like any
    other malformed-payload failure, not escape get_quote."""
    payload = _chart_payload(market_time=99_999_999_999_999_999_999)

    monkeypatch.setattr(
        http,
        "get_json",
        lambda url, ttl=None, headers=None: payload if "chart" in url else _summary_payload(),
    )
    q = get_quote("NVDA")
    assert q.available is False
    assert q.price is None
    assert q.error is not None


def test_malformed_price_type_returns_unavailable(monkeypatch):
    """A non-numeric price raises TypeError out of the change/change_percent
    arithmetic - that must degrade to available=False, not raise."""
    payload = _chart_payload()
    payload["chart"]["result"][0]["meta"]["regularMarketPrice"] = "not-a-number"

    monkeypatch.setattr(
        http,
        "get_json",
        lambda url, ttl=None, headers=None: payload if "chart" in url else _summary_payload(),
    )
    q = get_quote("NVDA")
    assert q.available is False
    assert q.price is None
    assert q.error is not None


# --- M4: market cap / P-E must never be a silent except/pass ----------------


def test_summary_failure_records_diagnostic_error_not_silent(monkeypatch):
    """A market-data fetch failure must leave a trace on Quote.error (even
    though available stays True) so it's distinguishable from Yahoo
    genuinely having no P/E for a ticker - never a bare except/pass."""

    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return _chart_payload()
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("NVDA")
    assert q.available is True
    assert q.price == 131.26
    assert q.market_cap is None
    assert q.pe_ratio is None
    assert q.error is not None
    assert "market data" in q.error.lower()


def test_crumb_unavailable_degrades_market_data_only(monkeypatch):
    """If the cookie+crumb handshake itself fails (Yahoo unreachable), the
    summary endpoint must not even be attempted, and price data must still
    come through untouched."""
    monkeypatch.setattr(quote_module, "_get_crumb", lambda force_refresh=False: None)

    def fake_get_json(url, ttl=None, headers=None):
        assert "chart" in url, "summary should never be attempted without a crumb"
        return _chart_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("NVDA")
    assert q.available is True
    assert q.price == 131.26
    assert q.market_cap is None
    assert q.pe_ratio is None
    assert q.error is not None
    assert "crumb" in q.error.lower()


def test_summary_401_refreshes_crumb_and_retries(monkeypatch):
    """A cached crumb can be invalidated server-side before our TTL expires
    it. A 401/403 on the summary call should trigger exactly one
    refresh-and-retry, and succeed if the fresh crumb is valid."""
    creds_seen = []
    summary_calls = {"count": 0}

    def fake_get_crumb(force_refresh=False):
        creds_seen.append(force_refresh)
        return ("fresh-crumb", "A3=y") if force_refresh else ("stale-crumb", "A3=x")

    monkeypatch.setattr(quote_module, "_get_crumb", fake_get_crumb)

    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return _chart_payload()
        summary_calls["count"] += 1
        if "fresh-crumb" not in url:
            request = httpx.Request("GET", url)
            response = httpx.Response(401, request=request, text="Invalid Crumb")
            raise httpx.HTTPStatusError("401", request=request, response=response)
        return _summary_payload()

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("NVDA")
    assert summary_calls["count"] == 2
    assert creds_seen == [False, True]
    assert q.market_cap == 3_200_000_000_000
    assert q.pe_ratio == 55.4
    assert q.error is None


def test_summary_non_auth_error_does_not_retry(monkeypatch):
    """A plain failure (timeout, 429, generic exception) is not a crumb
    problem - it should be recorded once, not retried."""
    creds_seen = []
    summary_calls = {"count": 0}

    def fake_get_crumb(force_refresh=False):
        creds_seen.append(force_refresh)
        return ("crumb", "A3=x")

    monkeypatch.setattr(quote_module, "_get_crumb", fake_get_crumb)

    def fake_get_json(url, ttl=None, headers=None):
        if "chart" in url:
            return _chart_payload()
        summary_calls["count"] += 1
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(http, "get_json", fake_get_json)

    q = get_quote("NVDA")
    assert summary_calls["count"] == 1
    assert creds_seen == [False]
    assert q.market_cap is None
    assert q.error is not None


# --- _get_crumb: the handshake itself, mocked at the httpx.Client level -----


def test_get_crumb_handshake_success_and_caches(monkeypatch, tmp_path):
    from globalinsight import cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)

    client_instances = []

    class _FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            self.cookies = {"A3": "cookie-value"}
            self.calls = []
            client_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, *a, **k):
            self.calls.append(url)
            if "getcrumb" in url:
                return _FakeResponse("abc123crumb")
            return _FakeResponse("")

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = _real_get_crumb()
    assert result == ("abc123crumb", "A3=cookie-value")
    assert len(client_instances) == 1
    assert any("getcrumb" in u for u in client_instances[0].calls)

    # A second call within the TTL must hit the cache, not open a new client.
    result2 = _real_get_crumb()
    assert result2 == result
    assert len(client_instances) == 1


def test_get_crumb_handshake_failure_returns_none(monkeypatch, tmp_path):
    from globalinsight import cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)

    class _FailingClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _FailingClient)

    assert _real_get_crumb() is None
