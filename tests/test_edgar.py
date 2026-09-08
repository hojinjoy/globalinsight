"""Tests for globalinsight.edgar: ticker resolution and submissions parsing.

All HTTP is faked by monkeypatching globalinsight.http.get_json - edgar.py
never touches httpx directly.
"""

import pytest

from globalinsight import config, edgar, http
from globalinsight.models import Company

TICKER_ROWS = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}


def _submissions(
    forms, accessions, filing_dates, primary_docs, items=None, report_dates=None, cik=1045810
):
    n = len(forms)
    return {
        "cik": str(cik),
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions,
                "filingDate": filing_dates,
                "primaryDocument": primary_docs,
                "items": items if items is not None else [""] * n,
                "reportDate": report_dates if report_dates is not None else [""] * n,
            }
        },
    }


# --- ticker resolution ---------------------------------------------------


def test_resolve_known_ticker(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda *a, **k: TICKER_ROWS)
    company = edgar.resolve("nvda")
    assert company == Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")


def test_resolve_unknown_ticker_raises(monkeypatch):
    """ETFs (SPY, QQQ, ...) are not in company_tickers.json - this is the
    expected, non-error path pipeline.py degrades to quote-only for."""
    monkeypatch.setattr(http, "get_json", lambda *a, **k: TICKER_ROWS)
    with pytest.raises(edgar.UnknownTicker):
        edgar.resolve("SPY")


def test_resolve_uses_tickers_ttl(monkeypatch):
    seen = {}

    def fake_get_json(url, ttl=None, headers=None):
        seen["ttl"] = ttl
        return TICKER_ROWS

    monkeypatch.setattr(http, "get_json", fake_get_json)
    edgar.resolve("NVDA")
    assert seen["ttl"] == config.TTL_TICKERS


def test_load_ticker_map(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda *a, **k: TICKER_ROWS)
    mapping = edgar.load_ticker_map()
    assert mapping == {"NVDA": 1045810, "AAPL": 320193}


# --- submissions -----------------------------------------------------------


def test_get_submissions_url_and_ttl(monkeypatch):
    seen = {}

    def fake_get_json(url, ttl=None, headers=None):
        seen["url"] = url
        seen["ttl"] = ttl
        return _submissions([], [], [], [])

    monkeypatch.setattr(http, "get_json", fake_get_json)
    edgar.get_submissions(1045810)
    assert seen["url"] == "https://data.sec.gov/submissions/CIK0001045810.json"
    assert seen["ttl"] == config.TTL_SUBMISSIONS


def test_rows_parses_items_and_computes_urls():
    subs = _submissions(
        forms=["8-K"],
        accessions=["0001045810-24-000123"],
        filing_dates=["2024-05-01"],
        primary_docs=["form8k.htm"],
        items=["2.02,9.01"],
    )
    rows = edgar._rows(subs)
    assert len(rows) == 1
    row = rows[0]
    assert row.items == ["2.02", "9.01"]
    assert row.url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000123/form8k.htm"
    )


def test_rows_handles_empty_primary_document():
    subs = _submissions(
        forms=["4"],
        accessions=["0001045810-24-000001"],
        filing_dates=["2024-01-01"],
        primary_docs=[""],
    )
    rows = edgar._rows(subs)
    assert rows[0].url == ""


def test_latest_filing_returns_newest_first_match():
    subs = _submissions(
        forms=["8-K", "10-K", "10-K"],
        accessions=["a1", "a2", "a3"],
        filing_dates=["2024-06-01", "2024-02-20", "2023-02-15"],
        primary_docs=["x.htm", "y.htm", "z.htm"],
    )
    filing = edgar.latest_filing(subs, "10-K")
    assert filing.accession == "a2"  # first (newest) 10-K row, not a3


def test_latest_filing_returns_none_when_absent():
    subs = _submissions(
        forms=["8-K"], accessions=["a1"], filing_dates=["2024-01-01"], primary_docs=["x.htm"]
    )
    assert edgar.latest_filing(subs, "10-K") is None


def test_latest_filing_adr_falls_back_to_20f():
    """ADRs file 20-F instead of 10-K - callers try 10-K, then 20-F, then
    40-F; edgar.latest_filing just needs to find the 20-F when asked."""
    subs = _submissions(
        forms=["20-F"],
        accessions=["a1"],
        filing_dates=["2024-03-01"],
        primary_docs=["annual.htm"],
    )
    assert edgar.latest_filing(subs, "10-K") is None
    filing = edgar.latest_filing(subs, "20-F")
    assert filing is not None
    assert filing.form == "20-F"


def test_eight_ks_since_filters_form_and_date():
    subs = _submissions(
        forms=["8-K", "8-K", "10-K", "8-K"],
        accessions=["a1", "a2", "a3", "a4"],
        filing_dates=["2024-06-01", "2024-01-01", "2024-03-01", "2024-04-15"],
        primary_docs=["a.htm", "b.htm", "c.htm", "d.htm"],
    )
    result = edgar.eight_ks_since(subs, since_date="2024-03-01")
    accessions = {f.accession for f in result}
    # a2 (2024-01-01) is before the cutoff; a3 is a 10-K, not an 8-K.
    assert accessions == {"a1", "a4"}


def test_eight_ks_since_strictly_after_not_equal():
    subs = _submissions(
        forms=["8-K"],
        accessions=["same-day"],
        filing_dates=["2024-03-01"],
        primary_docs=["a.htm"],
    )
    result = edgar.eight_ks_since(subs, since_date="2024-03-01")
    assert result == []


def test_with_url_recomputes_for_given_cik():
    subs = _submissions(
        forms=["10-K"],
        accessions=["0000320193-24-000123"],
        filing_dates=["2024-11-01"],
        primary_docs=["aapl-10k.htm"],
    )
    filing = edgar.latest_filing(subs, "10-K")
    moved = edgar.with_url(filing, cik=999)
    assert moved.url == (
        "https://www.sec.gov/Archives/edgar/data/999/000032019324000123/aapl-10k.htm"
    )


def test_index_json_url_and_document_url():
    assert edgar.index_json_url(1045810, "0001045810-24-000010") == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000010/index.json"
    )
    assert edgar.document_url(1045810, "0001045810-24-000010", "ex991.htm") == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000010/ex991.htm"
    )
