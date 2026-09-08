"""Yahoo Finance quote lookup.

Yahoo returns HTTP 429 frequently - this is empirically confirmed, not a
hypothetical edge case - and it is not an SEC endpoint, so it uses its own
browser-like User-Agent rather than the SEC contact-address header (SEC's
header would look like a bot signature to Yahoo and likely fare worse).
Every failure mode (429, timeout, malformed payload) resolves to
``Quote(available=False, error=...)`` rather than raising, so a quote outage
never blanks the rest of the brief.

Market cap / P-E come from the ``quoteSummary`` endpoint, which as of late
2025 requires a cookie+crumb handshake (GET a Yahoo page for cookies, GET
``/v1/test/getcrumb`` with those cookies, then pass ``?crumb=...`` on the
quoteSummary request using the same cookies) - an unauthenticated request now
gets a hard 401 "Invalid Crumb" for every ticker. The chart endpoint (price,
change, 52-week range) does not need this and is unaffected. The crumb is
cached (``_CRUMB_TTL``) since it is reusable across calls; a request that
still 401s with a cached crumb gets one refresh-and-retry, since Yahoo can
invalidate a crumb server-side before our TTL says it's stale.

A failure fetching market cap / P-E costs only those two fields - it must
never take down the price data already fetched from the chart endpoint - but
it is never swallowed silently either: it's recorded on ``Quote.error`` (even
though ``available`` stays True), so a fetch failure is visible to anyone who
inspects the quote instead of disappearing into a bare ``except: pass``. A
value Yahoo genuinely doesn't report (e.g. an ETF has no P/E) is left as a
plain None with ``error`` untouched, so it isn't mistaken for a failure.
"""

from datetime import datetime, timezone
from urllib.parse import quote as _url_quote

import httpx

from . import cache, http
from .config import TTL_QUOTE
from .models import Quote

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1d"
_QUOTE_SUMMARY_URL = (
    "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    "?modules=summaryDetail,price"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
# The crumb handshake talks to Yahoo pages and a plain-text crumb endpoint,
# none of which speak JSON - sending "Accept: application/json" at
# getcrumb empirically gets a 406 "Not Acceptable" instead of the crumb.
_HANDSHAKE_HEADERS = {"User-Agent": _HEADERS["User-Agent"]}

# Cookie+crumb handshake for quoteSummary. Deliberately NOT routed through
# http.py: that module's get_text/get_json make one stateless request per
# call and never expose Set-Cookie back to the caller, so it cannot carry a
# cookie jar across the fc.yahoo.com -> getcrumb -> quoteSummary sequence.
# This is the one place quote.py talks to Yahoo directly instead of through
# the shared HTTP layer; everything else (chart, and quoteSummary itself
# once we have a crumb) still goes through http.get_json.
_CRUMB_CACHE_KEY = "yahoo:crumb_v1"
_CRUMB_TTL = 1800  # 30 min - crumbs are reusable "for a while", not forever


def _get_crumb(force_refresh: bool = False) -> tuple[str, str] | None:
    """Return ``(crumb, cookie_header)`` for the quoteSummary handshake.

    Cached for ``_CRUMB_TTL`` seconds via the shared disk cache. Returns None
    if the handshake itself fails (network error, unexpected response) -
    callers must treat that as "market data unavailable this cycle" rather
    than raising.
    """
    if not force_refresh:
        cached = cache.get(_CRUMB_CACHE_KEY, _CRUMB_TTL)
        if cached:
            crumb, cookie_header = cached
            return crumb, cookie_header

    try:
        with httpx.Client(
            headers=_HANDSHAKE_HEADERS, timeout=15.0, follow_redirects=True
        ) as client:
            # Any Yahoo page sets the session cookie the crumb endpoint
            # checks; fc.yahoo.com is the lightest one (404s but still sets
            # it). finance.yahoo.com is hit too since fc.yahoo.com alone has
            # empirically 406'd the crumb request on its own.
            client.get("https://fc.yahoo.com")
            client.get("https://finance.yahoo.com")
            resp = client.get("https://query2.finance.yahoo.com/v1/test/getcrumb")
            resp.raise_for_status()
            crumb = resp.text.strip()
            cookie_header = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    except Exception:
        return None

    # A crumb is a short opaque token; an error response is JSON starting
    # with "{". Guard against caching a failure that didn't raise.
    if not crumb or not cookie_header or crumb.startswith("{"):
        return None

    cache.set(_CRUMB_CACHE_KEY, [crumb, cookie_header])
    return crumb, cookie_header


def _fetch_market_data(symbol: str, quote: Quote) -> None:
    """Populate ``quote.market_cap`` / ``quote.pe_ratio`` in place.

    Any failure (crumb handshake, 401/403 even after one refresh, timeout,
    malformed payload) leaves both fields None and records why on
    ``quote.error`` - never raises, and never leaves the failure invisible.
    """
    creds = _get_crumb()
    if creds is None:
        quote.error = "market data unavailable: could not obtain Yahoo crumb"
        return

    for attempt in range(2):
        crumb, cookie_header = creds
        try:
            url = (
                f"{_QUOTE_SUMMARY_URL.format(ticker=symbol)}"
                f"&crumb={_url_quote(crumb, safe='')}"
            )
            headers = {**_HEADERS, "Cookie": cookie_header}
            summary = http.get_json(url, ttl=TTL_QUOTE, headers=headers)
            result = summary["quoteSummary"]["result"][0]
            detail = result.get("summaryDetail", {})
            price_module = result.get("price", {})
            quote.market_cap = (price_module.get("marketCap") or {}).get("raw") or (
                detail.get("marketCap") or {}
            ).get("raw")
            quote.pe_ratio = (detail.get("trailingPE") or {}).get("raw")
            return
        except Exception as exc:
            is_auth_error = (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in (401, 403)
            )
            if attempt == 0 and is_auth_error:
                refreshed = _get_crumb(force_refresh=True)
                if refreshed is not None:
                    creds = refreshed
                    continue
            quote.error = f"market data unavailable: {type(exc).__name__}: {exc}"
            return


def get_quote(ticker: str) -> Quote:
    """Fetch a point-in-time quote for ``ticker``.

    Args:
        ticker: A stock or ETF ticker symbol.

    Returns:
        A Quote. On any failure (429, network error, unexpected payload
        shape - including a malformed timestamp or price field) returns
        ``Quote(available=False, error=<reason>)`` instead of raising -
        callers must be able to render the rest of the page without a quote.
        A failure limited to market cap / P-E leaves ``available=True`` with
        those two fields None and a note on ``error`` instead.
    """
    symbol = ticker.upper()
    # Yahoo spells share classes with a dash (BRK-B) where SEC filings and client
    # statements print a dot (BRK.B). Try the symbol as given FIRST so that
    # international suffixes (7203.T, VOD.L) are not mangled, then fall back to
    # the dash form only if the original 404s.
    candidates = [symbol]
    if "." in symbol or "/" in symbol:
        candidates.append(symbol.replace(".", "-").replace("/", "-"))
    try:
        chart = None
        meta: dict = {}
        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                attempt = http.get_json(
                    _CHART_URL.format(ticker=candidate), ttl=TTL_QUOTE, headers=_HEADERS
                )
                attempt_meta = attempt["chart"]["result"][0]["meta"]
            except Exception as exc:  # try the next spelling before giving up
                last_exc = exc
                continue
            # Yahoo answers the dot form with 200 and a null price rather than a
            # 404, so an exception alone is not enough to trigger the fallback -
            # only accept a candidate that actually carries a price.
            chart, meta, symbol = attempt, attempt_meta, candidate
            if attempt_meta.get("regularMarketPrice") is not None:
                break
        if chart is None:
            raise last_exc  # type: ignore[misc]

        price = meta.get("regularMarketPrice")
        previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        change = (
            price - previous_close if price is not None and previous_close else None
        )
        change_percent = (
            change / previous_close * 100
            if change is not None and previous_close
            else None
        )
        market_time = meta.get("regularMarketTime")
        as_of = (
            datetime.fromtimestamp(market_time, tz=timezone.utc).isoformat()
            if market_time
            else None
        )

        quote = Quote(
            ticker=symbol,
            available=True,
            price=price,
            change=change,
            change_percent=change_percent,
            week52_low=meta.get("fiftyTwoWeekLow"),
            week52_high=meta.get("fiftyTwoWeekHigh"),
            currency=meta.get("currency", "USD"),
            as_of=as_of,
        )
    except Exception as exc:
        return Quote(
            ticker=symbol,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    # Market cap / P-E live on a separate endpoint that needs its own
    # cookie+crumb handshake and 401s/429s more often than the chart one - a
    # failure here must cost only those two fields, never the price we
    # already have.
    _fetch_market_data(symbol, quote)

    return quote
