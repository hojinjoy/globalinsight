"""Tests for globalinsight.quote: Yahoo Finance quote lookup.

Every failure mode must resolve to Quote(available=False, error=...) rather
than raising - Yahoo's frequent 429s must never blank the rest of the brief.
"""

from globalinsight import config, http
from globalinsight.quote import get_quote


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
