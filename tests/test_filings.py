"""Tests for globalinsight.filings: 10-K/8-K document fetching and tiering.

These guard the highest-value empirical invariants in the codebase:
- 8-K exhibits (not just the content-free primaryDocument stub) are fetched.
- XBRL-viewer artifacts (R1.htm, R2.htm, ...) are excluded.
- The tier policy caps and metadata-only behavior are exactly as specified.
- Middle truncation keeps head AND tail (financial tables live in the tail).
- One bad exhibit/8-K must not sink the others (failure isolation).
"""

import pytest

from globalinsight import edgar, filings, http
from globalinsight.config import TIER1_CAP_TOKENS, TIER2_CAP_TOKENS
from globalinsight.models import Company, FilingRef

COMPANY = Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")


def _filing(accession="0001045810-24-000050", items=None, filing_date="2024-05-22"):
    return FilingRef(
        form="8-K",
        accession=accession,
        filing_date=filing_date,
        report_date="",
        primary_document="form8k.htm",
        url=edgar.document_url(COMPANY.cik, accession, "form8k.htm"),
        items=items or [],
    )


def _index_json(names: list[str]) -> dict:
    return {"directory": {"item": [{"name": name} for name in names]}}


# --- tier policy ---------------------------------------------------------


@pytest.mark.parametrize("item", ["2.02"])
def test_tier1_items(item):
    assert filings.determine_tier([item]) == 1


@pytest.mark.parametrize("item", ["5.02", "1.01", "2.03"])
def test_tier2_items(item):
    assert filings.determine_tier([item]) == 2


@pytest.mark.parametrize("item", ["8.01", "7.01", "5.07"])
def test_tier3_items(item):
    assert filings.determine_tier([item]) == 3


def test_unknown_item_codes_default_to_tier3():
    assert filings.determine_tier(["9.99"]) == 3
    assert filings.determine_tier([]) == 3


def test_multiple_items_takes_highest_priority_tier():
    # 2.02 (tier 1) beats 8.01 (tier 3) when both are present.
    assert filings.determine_tier(["8.01", "2.02"]) == 1
    assert filings.determine_tier(["8.01", "5.02"]) == 2


# --- 8-K exhibit fetching: the highest-value invariant ------------------------


def test_exhibits_are_fetched_and_xbrl_viewer_artifacts_excluded(monkeypatch):
    """The core regression guard: fetching only primaryDocument returns a
    content-free stub (~997 tokens on a real filing) and silently empties
    the brief. Exhibits (EX-99.1, EX-99.2) MUST be fetched; R\\d+.htm
    (XBRL-viewer renderings) MUST be excluded.
    """
    filing = _filing(items=["2.02"])  # tier 1, so a body is fetched at all

    index = _index_json(
        ["form8k.htm", "ex991.htm", "ex992.htm", "R1.htm", "R2.htm", "R10.htm"]
    )

    def fake_get_json(url, ttl=None, headers=None):
        assert url == edgar.index_json_url(COMPANY.cik, filing.accession)
        return index

    fetched_urls = []

    def fake_get_text(url, ttl=None, headers=None):
        fetched_urls.append(url)
        if "form8k" in url:
            return "<p>stub cover page - item checkboxes only</p>"
        if "ex991" in url:
            return "<p>PRESS RELEASE: record quarterly earnings</p>"
        if "ex992" in url:
            return "<p>Prepared remarks transcript</p>"
        if url.startswith("R"):  # pragma: no cover - must never be reached
            return "<p>XBRL viewer rendering - must not be fetched</p>"
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(http, "get_json", fake_get_json)
    monkeypatch.setattr(http, "get_text", fake_get_text)

    text = filings.fetch_8k_text(COMPANY, filing)

    assert text is not None
    assert "record quarterly earnings" in text
    assert "Prepared remarks transcript" in text
    assert "XBRL viewer rendering" not in text
    assert not any("R1.htm" in u or "R2.htm" in u or "R10.htm" in u for u in fetched_urls)


def test_exhibit_naming_pattern_excludes_only_r_plus_digits(monkeypatch):
    """Sanity-check the regex boundary: a real exhibit that happens to start
    with 'R' but isn't the R<digits>.htm XBRL-viewer pattern must still be
    fetched."""
    filing = _filing(items=["2.02"])
    index = _index_json(["Report99.htm", "R5.htm"])

    monkeypatch.setattr(http, "get_json", lambda *a, **k: index)

    def fake_get_text(url, ttl=None, headers=None):
        if "Report99" in url:
            return "<p>legit content</p>"
        raise AssertionError(f"R5.htm should have been excluded, got fetch: {url}")

    monkeypatch.setattr(http, "get_text", fake_get_text)
    text = filings.fetch_8k_text(COMPANY, filing)
    assert "legit content" in text


def test_one_bad_exhibit_does_not_sink_the_others(monkeypatch):
    filing = _filing(items=["2.02"])
    index = _index_json(["ex991.htm", "ex992.htm"])
    monkeypatch.setattr(http, "get_json", lambda *a, **k: index)

    def fake_get_text(url, ttl=None, headers=None):
        if "ex991" in url:
            raise RuntimeError("SEC 500 for this one exhibit")
        return "<p>good exhibit survives</p>"

    monkeypatch.setattr(http, "get_text", fake_get_text)
    text = filings.fetch_8k_text(COMPANY, filing)
    assert "good exhibit survives" in text


# --- tier policy wired into fetch_8k_text -------------------------------------


def test_tier3_filing_triggers_zero_document_fetches(monkeypatch):
    filing = _filing(items=["8.01"])

    def fail_get_json(*a, **k):
        raise AssertionError("tier-3 filing must never fetch index.json")

    def fail_get_text(*a, **k):
        raise AssertionError("tier-3 filing must never fetch a document body")

    monkeypatch.setattr(http, "get_json", fail_get_json)
    monkeypatch.setattr(http, "get_text", fail_get_text)

    assert filings.fetch_8k_text(COMPANY, filing) is None


def test_tier1_cap_applied(monkeypatch):
    filing = _filing(items=["2.02"])
    index = _index_json(["ex991.htm"])
    monkeypatch.setattr(http, "get_json", lambda *a, **k: index)
    huge_html = "<p>" + ("word " * 500_000) + "</p>"  # far bigger than the cap
    monkeypatch.setattr(http, "get_text", lambda *a, **k: huge_html)

    text = filings.fetch_8k_text(COMPANY, filing)
    from globalinsight import clean

    assert clean.count_tokens_approx(text) <= TIER1_CAP_TOKENS + 50  # marker overhead
    assert "truncated" in text


def test_tier2_cap_applied(monkeypatch):
    filing = _filing(items=["5.02"])
    index = _index_json(["ex991.htm"])
    monkeypatch.setattr(http, "get_json", lambda *a, **k: index)
    huge_html = "<p>" + ("word " * 500_000) + "</p>"
    monkeypatch.setattr(http, "get_text", lambda *a, **k: huge_html)

    text = filings.fetch_8k_text(COMPANY, filing)
    from globalinsight import clean

    assert clean.count_tokens_approx(text) <= TIER2_CAP_TOKENS + 50


# --- middle truncation ---------------------------------------------------


def test_truncate_middle_keeps_head_and_tail_drops_middle():
    head = "HEAD" * 20
    middle = "MIDDLE" * 5000  # the bulk, should be dropped
    tail = "FINANCIAL_TABLE_TAIL" * 20
    text = head + middle + tail

    result = filings.truncate_middle(text, max_tokens=100)

    assert result.startswith("HEAD")
    assert result.endswith(text[-50:])  # exact trailing characters preserved
    assert "FINANCIAL_TABLE_TAIL" in result
    assert "truncated" in result
    # The middle padding should be gone (or drastically reduced).
    assert result.count("MIDDLE") < middle.count("MIDDLE")


def test_truncate_middle_tail_survives_where_financial_tables_live():
    """This is the specific invariant called out for regression: naive
    tail-truncation (the 'simpler', wrong choice) would throw away exactly
    the financial tables that live at the end of a press release."""
    head = "Q3 press release narrative. " * 200
    tail_marker = "TOTAL REVENUE: $130,497 million (financial table)"
    text = head + tail_marker

    result = filings.truncate_middle(text, max_tokens=50)
    assert tail_marker in result


def test_truncate_middle_no_op_under_cap():
    text = "short document"
    assert filings.truncate_middle(text, max_tokens=1000) == text


def test_truncate_middle_marker_reports_dropped_token_count():
    text = "x" * 4000  # 1000 tokens approx
    result = filings.truncate_middle(text, max_tokens=100)
    assert "[... truncated 900 tokens ...]" in result


# --- 10-K / annual filing fetch -----------------------------------------------


def test_fetch_10k_text_prefers_10k_then_20f_then_40f(monkeypatch):
    calls = {"forms_checked": []}

    def fake_latest_filing(subs, form):
        calls["forms_checked"].append(form)
        if form == "20-F":
            return FilingRef(
                form="20-F",
                accession="acc-1",
                filing_date="2024-04-01",
                report_date="",
                primary_document="annual.htm",
                url="https://example.com/annual.htm",
            )
        return None

    monkeypatch.setattr(edgar, "get_submissions", lambda cik: {"cik": str(cik)})
    monkeypatch.setattr(edgar, "latest_filing", fake_latest_filing)
    monkeypatch.setattr(http, "get_text", lambda *a, **k: "<p>ADR annual report body</p>")

    result = filings.fetch_10k_text(COMPANY)
    assert result is not None
    text, filing = result
    assert filing.form == "20-F"
    assert "ADR annual report body" in text
    # 10-K tried first, then 20-F found - never even asked for 40-F.
    assert calls["forms_checked"][:2] == ["10-K", "20-F"]


def test_fetch_10k_text_returns_none_when_no_annual_filing_exists(monkeypatch):
    """Fresh-IPO case: an S-1 exists but no 10-K/20-F/40-F yet."""
    monkeypatch.setattr(edgar, "get_submissions", lambda cik: {"cik": str(cik)})
    monkeypatch.setattr(edgar, "latest_filing", lambda subs, form: None)
    assert filings.fetch_10k_text(COMPANY) is None


def test_fetch_10k_text_uses_permanent_ttl(monkeypatch):
    from globalinsight.config import TTL_PERMANENT

    filing = FilingRef(
        form="10-K",
        accession="acc-1",
        filing_date="2026-03-15",
        report_date="",
        primary_document="nvda-10k.htm",
        url="https://example.com/nvda-10k.htm",
    )
    monkeypatch.setattr(edgar, "get_submissions", lambda cik: {})
    monkeypatch.setattr(edgar, "latest_filing", lambda subs, form: filing if form == "10-K" else None)

    seen = {}

    def fake_get_text(url, ttl=None, headers=None):
        seen["ttl"] = ttl
        return "<p>body</p>"

    monkeypatch.setattr(http, "get_text", fake_get_text)
    filings.fetch_10k_text(COMPANY)
    assert seen["ttl"] == TTL_PERMANENT


# --- fetch_tiered_8ks: concurrent fan-out + failure isolation -----------------


def test_fetch_tiered_8ks_isolates_one_failure(monkeypatch):
    good1 = _filing(accession="acc-good-1", items=["2.02"])
    bad = _filing(accession="acc-bad", items=["1.01"])
    good2 = _filing(accession="acc-good-2", items=["5.02"])

    def fake_fetch_8k_text(company, filing):
        if filing.accession == "acc-bad":
            raise RuntimeError("SEC 500 while fetching this 8-K's exhibits")
        return f"text for {filing.accession}"

    monkeypatch.setattr(filings, "fetch_8k_text", fake_fetch_8k_text)

    results = filings.fetch_tiered_8ks(COMPANY, [good1, bad, good2])

    assert results["acc-good-1"] == "text for acc-good-1"
    assert results["acc-good-2"] == "text for acc-good-2"
    assert results["acc-bad"] is None  # failed 8-K reported as None, not raised


def test_fetch_tiered_8ks_tier3_entries_are_none(monkeypatch):
    tier3 = _filing(accession="acc-tier3", items=["8.01"])
    monkeypatch.setattr(http, "get_json", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("tier-3 must not fetch index.json")
    ))
    results = filings.fetch_tiered_8ks(COMPANY, [tier3])
    assert results == {"acc-tier3": None}
