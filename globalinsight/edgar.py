"""Ticker resolution and SEC submissions/filings lookups.

No document fetching here - that's filings.py. This module only deals in
the small, fast JSON endpoints (company_tickers.json, submissions.json) so
that pipeline.wave2() can stay well under a second.
"""

from dataclasses import replace

from . import http
from .config import TTL_SUBMISSIONS, TTL_TICKERS
from .models import Company, FilingRef

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


class UnknownTicker(Exception):
    """Raised when a ticker isn't in SEC's company_tickers.json.

    This is also how ETFs (SPY, QQQ, ...) surface: company_tickers.json only
    covers SEC-registered operating-company filers, not fund tickers, so an
    ETF ticker legitimately resolves to "unknown" here. Callers (pipeline.py)
    are expected to catch this and degrade to a quote-only result rather than
    treat it as an error.
    """


def _archive_dir_url(cik: int, accession: str) -> str:
    """Base URL of a filing's document directory (no trailing filename)."""
    accession_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"


def index_json_url(cik: int, accession: str) -> str:
    """URL of a filing's index.json - the manifest of every file it contains."""
    return _archive_dir_url(cik, accession) + "index.json"


def document_url(cik: int, accession: str, document_name: str) -> str:
    """URL of one specific document (primary or exhibit) within a filing."""
    return _archive_dir_url(cik, accession) + document_name


def _raw_ticker_rows(ttl: float | None = TTL_TICKERS) -> dict:
    """The raw company_tickers.json payload: {"0": {"cik_str", "ticker", "title"}, ...}."""
    return http.get_json(TICKERS_URL, ttl=ttl)


def load_ticker_map(ttl: float | None = TTL_TICKERS) -> dict[str, int]:
    """Return ``{TICKER: cik_int}`` for every SEC-registered filer.

    Args:
        ttl: Cache TTL in seconds (default 24h - see config.TTL_TICKERS).

    Returns:
        A dict of ~10,400 uppercase ticker symbols to integer CIKs.
    """
    rows = _raw_ticker_rows(ttl)
    return {row["ticker"].upper(): int(row["cik_str"]) for row in rows.values()}


def resolve(ticker: str) -> Company:
    """Resolve a ticker symbol to a ``Company``.

    Args:
        ticker: A stock ticker, case-insensitive (e.g. "nvda" or "NVDA").

    Returns:
        The resolved Company (ticker, cik, registered name).

    Raises:
        UnknownTicker: If the ticker isn't in SEC's company_tickers.json -
            this is the expected path for ETFs, which don't have SEC filer
            CIKs the way operating companies do.
    """
    symbol = ticker.upper()
    rows = _raw_ticker_rows()
    for row in rows.values():
        if row["ticker"].upper() == symbol:
            return Company(ticker=symbol, cik=int(row["cik_str"]), name=row["title"])
    raise UnknownTicker(f"{ticker!r} is not an SEC-registered filer ticker")


def get_submissions(cik: int) -> dict:
    """Fetch and parse submissions.json for a CIK (cached 24h).

    Args:
        cik: The company's SEC CIK number.

    Returns:
        The parsed submissions JSON, including the ``filings.recent``
        parallel-array structure that latest_filing()/eight_ks_since() read.
    """
    url = SUBMISSIONS_URL.format(cik=str(cik).zfill(10))
    return http.get_json(url, ttl=TTL_SUBMISSIONS)


def _rows(subs: dict) -> list[FilingRef]:
    """Turn filings.recent's parallel arrays into a list of FilingRef.

    Fact #6: filings.recent holds ~1000 filings, always enough for a <=12
    month 10-K lookback, so filings.files[] (the older overflow shard) is
    intentionally never consulted.
    """
    recent = subs["filings"]["recent"]
    cik = int(subs["cik"])
    n = len(recent["form"])
    items_list = recent.get("items", [""] * n)
    report_dates = recent.get("reportDate", [""] * n)
    out = []
    for i in range(n):
        accession = recent["accessionNumber"][i]
        primary_doc = recent["primaryDocument"][i]
        raw_items = items_list[i] if i < len(items_list) else ""
        item_codes = [code.strip() for code in raw_items.split(",") if code.strip()]
        out.append(
            FilingRef(
                form=recent["form"][i],
                accession=accession,
                filing_date=recent["filingDate"][i],
                report_date=report_dates[i] if i < len(report_dates) else "",
                primary_document=primary_doc,
                url=document_url(cik, accession, primary_doc) if primary_doc else "",
                items=item_codes,
            )
        )
    return out


def latest_filing(subs: dict, form: str) -> FilingRef | None:
    """Most recent filing of the given form.

    Args:
        subs: Parsed submissions JSON from get_submissions().
        form: A form type, e.g. "10-K", "20-F", "8-K".

    Returns:
        The newest matching FilingRef, or None if there isn't one. Relies on
        filings.recent being newest-first (index 0 == most recent), which is
        how SEC always returns it.
    """
    for filing in _rows(subs):
        if filing.form == form:
            return filing
    return None


def eight_ks_since(subs: dict, since_date: str) -> list[FilingRef]:
    """8-Ks filed strictly after ``since_date``, each carrying its item codes.

    Args:
        subs: Parsed submissions JSON from get_submissions().
        since_date: An ISO date string ("YYYY-MM-DD"); typically the annual
            report's filing_date, to capture everything disclosed since.

    Returns:
        Matching FilingRefs, newest first, with `.items` populated from
        submissions.json's `items` array (fact #5 - no document parsing
        needed to know what an 8-K is about).
    """
    return [
        f for f in _rows(subs) if f.form == "8-K" and f.filing_date > since_date
    ]


def with_url(filing: FilingRef, cik: int) -> FilingRef:
    """Return a copy of ``filing`` with its URL recomputed for ``cik``.

    _rows() already stamps the URL using the CIK found in submissions.json,
    so this is only needed if a FilingRef is ever carried across companies
    (it isn't, in this codebase) - kept as a small, explicit escape hatch
    rather than leaving URL construction implicit.
    """
    return replace(filing, url=document_url(cik, filing.accession, filing.primary_document))
