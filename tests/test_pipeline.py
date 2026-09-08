"""Tests for globalinsight.pipeline: wave orchestration and failure isolation.

Every network-touching call is monkeypatched at the module-attribute level
pipeline.py itself uses (pipeline.edgar, pipeline.quote_mod,
pipeline.financials_mod, pipeline.filings, pipeline.generate_brief) so pure
logic in edgar.py (latest_filing, eight_ks_since) can still run for real
against small constructed submissions fixtures.
"""

from unittest.mock import MagicMock

from globalinsight import edgar, pipeline
from globalinsight.models import Company, FilingRef, FinancialSeries, Quote


def _submissions(forms, accessions, filing_dates, primary_docs, items=None, cik=1045810):
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
                "reportDate": [""] * n,
            }
        },
    }


COMPANY = Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")


def _quote_ok():
    return Quote(ticker="NVDA", available=True, price=131.26)


# --- wave2 ---------------------------------------------------------------


def test_wave2_etf_degrades_to_quote_only(monkeypatch):
    monkeypatch.setattr(
        pipeline.edgar, "resolve", MagicMock(side_effect=edgar.UnknownTicker("SPY"))
    )
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())

    result = pipeline.wave2("SPY")

    assert result.is_sec_filer is False
    assert result.company is None
    assert result.quote.available is True
    assert "resolve" in result.errors


def test_wave2_full_success_with_annual_filing_and_8ks(monkeypatch):
    subs = _submissions(
        forms=["10-K", "8-K", "8-K"],
        accessions=["acc-10k", "acc-8k-1", "acc-8k-2"],
        filing_dates=["2026-03-15", "2026-06-01", "2025-01-01"],
        primary_docs=["nvda-10k.htm", "e1.htm", "e2.htm"],
        items=["", "2.02", "8.01"],
    )
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(
        pipeline.financials_mod,
        "get_financials",
        lambda cik: {"Revenue": FinancialSeries(concept="Revenue", unit="USD", points=[])},
    )

    result = pipeline.wave2("NVDA")

    assert result.is_sec_filer is True
    assert result.company == COMPANY
    assert result.annual_filing.accession == "acc-10k"
    # Only the 8-K filed after the 10-K's filing_date (2026-03-15) qualifies.
    assert [f.accession for f in result.eight_ks] == ["acc-8k-1"]
    assert "Revenue" in result.financials
    assert result.errors == {}


def test_wave2_submissions_failure_isolated_from_financials(monkeypatch):
    """Missing/failed submissions must not prevent financials or quote from
    populating - and vice versa."""
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)

    def boom(cik):
        raise RuntimeError("SEC 500")

    monkeypatch.setattr(pipeline.edgar, "get_submissions", boom)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(
        pipeline.financials_mod,
        "get_financials",
        lambda cik: {"Revenue": FinancialSeries(concept="Revenue", unit="USD", points=[])},
    )

    result = pipeline.wave2("NVDA")

    assert "submissions" in result.errors
    assert result.annual_filing is None
    assert result.quote.available is True
    assert "Revenue" in result.financials  # financials unaffected


def test_wave2_financials_failure_isolated_from_submissions(monkeypatch):
    """Missing companyfacts -> financials absent, but the rest of wave2
    (quote, filing index) still returns."""
    subs = _submissions(
        forms=["10-K"], accessions=["acc-10k"], filing_dates=["2026-03-15"],
        primary_docs=["nvda-10k.htm"],
    )
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())

    def boom(cik):
        raise RuntimeError("companyfacts 404")

    monkeypatch.setattr(pipeline.financials_mod, "get_financials", boom)

    result = pipeline.wave2("NVDA")

    assert "financials" in result.errors
    assert result.financials == {}
    assert result.annual_filing.accession == "acc-10k"
    assert result.quote.available is True


def test_wave2_adr_falls_back_to_20f(monkeypatch):
    subs = _submissions(
        forms=["20-F"], accessions=["acc-20f"], filing_dates=["2026-04-01"],
        primary_docs=["annual.htm"],
    )
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(pipeline.financials_mod, "get_financials", lambda cik: {})

    result = pipeline.wave2("TSM")
    assert result.annual_filing.form == "20-F"


def test_wave2_fresh_ipo_surfaces_s1(monkeypatch):
    subs = _submissions(
        forms=["S-1"], accessions=["acc-s1"], filing_dates=["2026-01-01"],
        primary_docs=["s1.htm"],
    )
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(pipeline.financials_mod, "get_financials", lambda cik: {})

    result = pipeline.wave2("NEWCO")
    assert result.annual_filing is None
    assert len(result.other_filings) == 1
    assert result.other_filings[0].accession == "acc-s1"


# --- wave3 -----------------------------------------------------------------


def _wave2_with_annual(eight_ks=None):
    return pipeline.Wave2Result(
        ticker="NVDA",
        company=COMPANY,
        quote=_quote_ok(),
        annual_filing=FilingRef(
            form="10-K", accession="acc-10k", filing_date="2026-03-15",
            report_date="", primary_document="nvda-10k.htm", url="https://x/10k.htm",
        ),
        eight_ks=eight_ks or [],
    )


def test_wave3_noop_when_no_company(monkeypatch):
    ctx = pipeline.Wave2Result(ticker="SPY")
    fetch_mock = MagicMock()
    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", fetch_mock)

    result = pipeline.wave3(ctx)

    assert result.tenk_text is None
    fetch_mock.assert_not_called()


def test_wave3_noop_when_no_annual_filing():
    ctx = pipeline.Wave2Result(ticker="NEWCO", company=COMPANY, annual_filing=None)
    result = pipeline.wave3(ctx)
    assert result.tenk_text is None
    assert result.eight_k_texts == {}


def test_wave3_fetches_10k_and_8ks(monkeypatch):
    filing = FilingRef(
        form="10-K", accession="acc-10k", filing_date="2026-03-15",
        report_date="", primary_document="nvda-10k.htm", url="https://x/10k.htm",
    )
    eight_k = FilingRef(
        form="8-K", accession="acc-8k-1", filing_date="2026-06-01",
        report_date="", primary_document="e1.htm", url="https://x/e1.htm", items=["2.02"],
    )
    ctx = _wave2_with_annual(eight_ks=[eight_k])
    ctx.annual_filing = filing

    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", lambda c: ("10-K TEXT", filing))
    monkeypatch.setattr(
        pipeline.filings, "fetch_tiered_8ks", lambda c, ks: {"acc-8k-1": "8-K TEXT"}
    )

    result = pipeline.wave3(ctx)
    assert result.tenk_text == "10-K TEXT"
    assert result.tenk_filing == filing
    assert result.eight_k_texts == {"acc-8k-1": "8-K TEXT"}


def test_wave3_filters_out_none_8k_results(monkeypatch):
    """Tier-3 8-Ks (and failed fetches) come back as None from
    fetch_tiered_8ks and must be dropped, not stored as None values."""
    dummy_8k = FilingRef(
        form="8-K", accession="acc-tier3", filing_date="2026-07-01", report_date="",
        primary_document="e.htm", url="https://x", items=["8.01"],
    )
    ctx = _wave2_with_annual(eight_ks=[dummy_8k])
    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", lambda c: None)
    monkeypatch.setattr(
        pipeline.filings,
        "fetch_tiered_8ks",
        lambda c, ks: {"acc-tier3": None, "acc-tier1": "has text"},
    )
    result = pipeline.wave3(ctx)
    assert result.eight_k_texts == {"acc-tier1": "has text"}


def test_wave3_10k_failure_isolated_from_8k_fetch(monkeypatch):
    ctx = _wave2_with_annual(eight_ks=[])

    def boom_10k(c):
        raise RuntimeError("SEC 500 on 10-K")

    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", boom_10k)
    monkeypatch.setattr(pipeline.filings, "fetch_tiered_8ks", lambda c, ks: {"a": "8k text"})

    result = pipeline.wave3(ctx)
    assert "10-K" in result.errors
    assert result.tenk_text is None
    # 8-K fetch still ran and is not blanked by the 10-K failure.
    eight_k = FilingRef(
        form="8-K", accession="a", filing_date="2026-06-01", report_date="",
        primary_document="e.htm", url="https://x", items=["2.02"],
    )
    ctx2 = _wave2_with_annual(eight_ks=[eight_k])
    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", boom_10k)
    monkeypatch.setattr(pipeline.filings, "fetch_tiered_8ks", lambda c, ks: {"a": "8k text"})
    result2 = pipeline.wave3(ctx2)
    assert result2.eight_k_texts == {"a": "8k text"}


def test_wave3_8k_failure_isolated_from_10k(monkeypatch):
    """One failed 8-K fetch (fetch_tiered_8ks itself raising) must not blank
    the 10-K text already fetched."""
    filing = FilingRef(
        form="10-K", accession="acc-10k", filing_date="2026-03-15",
        report_date="", primary_document="nvda-10k.htm", url="https://x/10k.htm",
    )
    eight_k = FilingRef(
        form="8-K", accession="acc-8k-1", filing_date="2026-06-01",
        report_date="", primary_document="e1.htm", url="https://x/e1.htm", items=["2.02"],
    )
    ctx = _wave2_with_annual(eight_ks=[eight_k])
    ctx.annual_filing = filing

    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", lambda c: ("10-K TEXT", filing))

    def boom_8k(c, ks):
        raise RuntimeError("thread pool blew up")

    monkeypatch.setattr(pipeline.filings, "fetch_tiered_8ks", boom_8k)

    result = pipeline.wave3(ctx)
    assert result.tenk_text == "10-K TEXT"
    assert "8-Ks" in result.errors


# --- build_synthesis_context -------------------------------------------------


def test_build_synthesis_context_none_when_no_company():
    wave2 = pipeline.Wave2Result(ticker="SPY")
    wave3 = pipeline.Wave3Result()
    assert pipeline.build_synthesis_context(wave2, wave3) is None


def test_build_synthesis_context_none_when_no_tenk_text():
    wave2 = pipeline.Wave2Result(ticker="NVDA", company=COMPANY)
    wave3 = pipeline.Wave3Result(tenk_text=None)
    assert pipeline.build_synthesis_context(wave2, wave3) is None


def test_build_synthesis_context_includes_metadata_only_8k_citations():
    filing = FilingRef(
        form="10-K", accession="acc-10k", filing_date="2026-03-15",
        report_date="", primary_document="nvda-10k.htm", url="https://x/10k.htm",
    )
    tier1_8k = FilingRef(
        form="8-K", accession="acc-tier1", filing_date="2026-06-01", report_date="",
        primary_document="e1.htm", url="https://x/e1.htm", items=["2.02"],
    )
    tier3_8k = FilingRef(
        form="8-K", accession="acc-tier3", filing_date="2026-07-01", report_date="",
        primary_document="e2.htm", url="https://x/e2.htm", items=["8.01"],
    )
    wave2 = pipeline.Wave2Result(
        ticker="NVDA", company=COMPANY, quote=_quote_ok(),
        annual_filing=filing, eight_ks=[tier1_8k, tier3_8k],
    )
    wave3 = pipeline.Wave3Result(
        tenk_text="10-K body", tenk_filing=filing,
        eight_k_texts={"acc-tier1": "8-K body"},
    )

    ctx = pipeline.build_synthesis_context(wave2, wave3)

    assert ctx.tenk_text == "10-K body"
    assert ctx.tenk_citation.accession == "acc-10k"
    assert ctx.eight_k_texts == {"acc-tier1": "8-K body"}
    # Both 8-Ks get citations, including the tier-3 metadata-only one.
    assert set(ctx.eight_k_citations) == {"acc-tier1", "acc-tier3"}
    assert ctx.eight_k_citations["acc-tier3"].item == "8.01"


# --- build_brief -------------------------------------------------------------


def test_build_brief_without_synthesize_does_not_call_generate_brief(monkeypatch):
    monkeypatch.setattr(pipeline.edgar, "resolve", MagicMock(side_effect=edgar.UnknownTicker()))
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    gen_mock = MagicMock()
    monkeypatch.setattr(pipeline, "generate_brief", gen_mock)

    result = pipeline.build_brief("SPY", synthesize=False)

    assert result.brief is None
    assert result.synthesis_error is None
    gen_mock.assert_not_called()


def test_build_brief_synthesize_with_no_10k_text_sets_error(monkeypatch):
    monkeypatch.setattr(pipeline.edgar, "resolve", MagicMock(side_effect=edgar.UnknownTicker()))
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    gen_mock = MagicMock()
    monkeypatch.setattr(pipeline, "generate_brief", gen_mock)

    result = pipeline.build_brief("SPY", synthesize=True)

    assert result.brief is None
    assert result.synthesis_error == "no 10-K text available to synthesize from"
    gen_mock.assert_not_called()


def test_build_brief_synthesize_success_passes_client_through(monkeypatch):
    subs = _submissions(
        forms=["10-K"], accessions=["acc-10k"], filing_dates=["2026-03-15"],
        primary_docs=["nvda-10k.htm"],
    )
    filing = edgar.latest_filing(subs, "10-K")
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(pipeline.financials_mod, "get_financials", lambda cik: {})
    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", lambda c: ("10-K body", filing))
    monkeypatch.setattr(pipeline.filings, "fetch_tiered_8ks", lambda c, ks: {})

    fake_brief = object()
    gen_mock = MagicMock(return_value=fake_brief)
    monkeypatch.setattr(pipeline, "generate_brief", gen_mock)

    sentinel_client = object()
    result = pipeline.build_brief("NVDA", synthesize=True, client=sentinel_client)

    assert result.brief is fake_brief
    assert result.synthesis_error is None
    gen_mock.assert_called_once()
    _, kwargs = gen_mock.call_args
    assert kwargs["client"] is sentinel_client


def test_build_brief_synthesis_failure_sets_error_not_raise(monkeypatch):
    subs = _submissions(
        forms=["10-K"], accessions=["acc-10k"], filing_dates=["2026-03-15"],
        primary_docs=["nvda-10k.htm"],
    )
    filing = edgar.latest_filing(subs, "10-K")
    monkeypatch.setattr(pipeline.edgar, "resolve", lambda t: COMPANY)
    monkeypatch.setattr(pipeline.edgar, "get_submissions", lambda cik: subs)
    monkeypatch.setattr(pipeline.quote_mod, "get_quote", lambda t: _quote_ok())
    monkeypatch.setattr(pipeline.financials_mod, "get_financials", lambda cik: {})
    monkeypatch.setattr(pipeline.filings, "fetch_10k_text", lambda c: ("10-K body", filing))
    monkeypatch.setattr(pipeline.filings, "fetch_tiered_8ks", lambda c, ks: {})

    def boom(ctx, client=None):
        raise RuntimeError("model refusal")

    monkeypatch.setattr(pipeline, "generate_brief", boom)

    result = pipeline.build_brief("NVDA", synthesize=True)
    assert result.brief is None
    assert "model refusal" in result.synthesis_error
