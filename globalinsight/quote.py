"""Yahoo Finance quote lookup.

Yahoo returns HTTP 429 frequently - this is empirically confirmed, not a
hypothetical edge case - and it is not an SEC endpoint, so it uses its own
browser-like User-Agent rather than the SEC contact-address header (SEC's
header would look like a bot signature to Yahoo and likely fare worse).
Every failure mode (429, timeout, malformed payload) resolves to
``Quote(available=False, error=...)`` rather than raising, so a quote outage
never blanks the rest of the brief.
"""

from datetime import datetime, timezone

from . import http
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


def get_quote(ticker: str) -> Quote:
    """Fetch a point-in-time quote for ``ticker``.

    Args:
        ticker: A stock or ETF ticker symbol.

    Returns:
        A Quote. On any failure (429, network error, unexpected payload
        shape), returns ``Quote(available=False, error=<reason>)`` instead
        of raising - callers must be able to render the rest of the page
        without a quote.
    """
    symbol = ticker.upper()
    try:
        chart = http.get_json(
            _CHART_URL.format(ticker=symbol), ttl=TTL_QUOTE, headers=_HEADERS
        )
        meta = chart["chart"]["result"][0]["meta"]
    except Exception as exc:
        return Quote(
            ticker=symbol,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    price = meta.get("regularMarketPrice")
    previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = price - previous_close if price is not None and previous_close else None
    change_percent = (
        change / previous_close * 100 if change is not None and previous_close else None
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

    # Market cap / P-E live on a separate endpoint that 401s/429s more often
    # than the chart one - a failure here should cost only those two fields,
    # never the price we already have.
    try:
        summary = http.get_json(
            _QUOTE_SUMMARY_URL.format(ticker=symbol), ttl=TTL_QUOTE, headers=_HEADERS
        )
        result = summary["quoteSummary"]["result"][0]
        detail = result.get("summaryDetail", {})
        price_module = result.get("price", {})
        quote.market_cap = (price_module.get("marketCap") or {}).get("raw") or (
            detail.get("marketCap") or {}
        ).get("raw")
        quote.pe_ratio = (detail.get("trailingPE") or {}).get("raw")
    except Exception:
        pass

    return quote
