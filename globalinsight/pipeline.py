"""Orchestration: the three waves an advisor sees, in order.

    wave2(ticker)   ~0.45s cold  quote + financials + filing index (no docs)
    wave3(ctx)      seconds      10-K + tiered 8-K document text
    build_brief()                wave2 + wave3 (+ optional LLM synthesis)

Every block is wrapped so a single failure degrades only its own piece of
the result - a missing quote, a missing companyfacts series, or one failed
8-K must never blank the rest of the brief. Edge cases (ADRs filing 20-F,
ETFs with no SEC filings at all, fresh IPOs with only an S-1) are handled as
explicit, typed results rather than exceptions bubbling out of build_brief.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import edgar, filings, financials as financials_mod, quote as quote_mod
from .models import Citation, Company, FilingRef, FinancialSeries, Quote
from .synthesize import SynthesisContext, generate_brief


@dataclass
class Wave2Result:
    """Output of wave2(): everything retrievable without fetching a document."""

    ticker: str
    company: Company | None = None
    quote: Quote | None = None
    financials: dict[str, FinancialSeries] = field(default_factory=dict)
    annual_filing: FilingRef | None = None
    eight_ks: list[FilingRef] = field(default_factory=list)
    other_filings: list[FilingRef] = field(default_factory=list)
    """Filled only when there is no 10-K/20-F/40-F on file yet (e.g. a
    recent IPO that has so far only filed an S-1)."""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_sec_filer(self) -> bool:
        """False for tickers with no SEC CIK at all - the ETF case (SPY, QQQ)."""
        return self.company is not None


@dataclass
class Wave3Result:
    """Output of wave3(): the fetched document text layered on top of wave2."""

    tenk_text: str | None = None
    tenk_filing: FilingRef | None = None
    eight_k_texts: dict[str, str] = field(default_factory=dict)
    """accession -> cleaned exhibit text, tier 1/2 8-Ks only."""
    errors: dict[str, str] = field(default_factory=dict)


def wave2(ticker: str) -> Wave2Result:
    """Quote + financials + filing index for ``ticker``. No document fetching.

    Fires the quote lookup (Yahoo) and, if the ticker resolves to an SEC
    filer, the two SEC lookups (submissions.json, companyfacts.json)
    concurrently - that's the "only 2 parallel SEC requests" this wave
    makes, which is what keeps it fast (~0.45s cold) even though the whole
    stack is otherwise a synchronous request-per-call design.

    Args:
        ticker: A stock ticker.

    Returns:
        A Wave2Result. If the ticker isn't an SEC-registered filer (an ETF
        like SPY/QQQ), ``company`` is None and only ``quote`` is populated -
        this is the expected quote-only degradation, not an error.
    """
    result = Wave2Result(ticker=ticker.upper())

    try:
        company = edgar.resolve(ticker)
    except edgar.UnknownTicker as exc:
        # Expected for ETFs: no SEC filer CIK, so no filings are possible.
        # The ticker may still be a valid, quotable instrument.
        result.errors["resolve"] = str(exc)
        result.quote = quote_mod.get_quote(ticker)
        return result

    result.company = company

    def _fetch_quote() -> Quote:
        return quote_mod.get_quote(ticker)

    def _fetch_submissions() -> dict:
        return edgar.get_submissions(company.cik)  # SEC request 1

    def _fetch_financials() -> dict[str, FinancialSeries]:
        return financials_mod.get_financials(company.cik)  # SEC request 2

    with ThreadPoolExecutor(max_workers=3) as pool:
        quote_future = pool.submit(_fetch_quote)
        submissions_future = pool.submit(_fetch_submissions)
        financials_future = pool.submit(_fetch_financials)

        result.quote = quote_future.result()

        try:
            submissions = submissions_future.result()
        except Exception as exc:
            submissions = None
            result.errors["submissions"] = f"{type(exc).__name__}: {exc}"

        try:
            result.financials = financials_future.result()
        except Exception as exc:
            result.errors["financials"] = f"{type(exc).__name__}: {exc}"

    if submissions is not None:
        annual = (
            edgar.latest_filing(submissions, "10-K")  # US domestic filer
            or edgar.latest_filing(submissions, "20-F")  # ADR
            or edgar.latest_filing(submissions, "40-F")  # Canadian issuer
        )
        if annual is not None:
            result.annual_filing = annual
            result.eight_ks = edgar.eight_ks_since(submissions, annual.filing_date)
        else:
            # No annual report yet - e.g. a recent IPO. Surface whatever the
            # filer *has* registered (an S-1 is the common case) rather than
            # silently returning nothing.
            s1 = edgar.latest_filing(submissions, "S-1")
            result.other_filings = [s1] if s1 else []

    return result


def wave3(ctx: Wave2Result) -> Wave3Result:
    """Fetch the 10-K and tiered 8-K document text for a wave2() result.

    Args:
        ctx: A Wave2Result (from wave2()).

    Returns:
        A Wave3Result. Empty/default if ``ctx`` has no SEC filer (ETF case)
        or no annual filing on record (fresh-IPO case) - wave3 has nothing
        to fetch in either case, which is not an error.
    """
    result = Wave3Result()
    if ctx.company is None or ctx.annual_filing is None:
        return result

    try:
        fetched = filings.fetch_10k_text(ctx.company)
        if fetched is not None:
            result.tenk_text, result.tenk_filing = fetched
    except Exception as exc:
        result.errors["10-K"] = f"{type(exc).__name__}: {exc}"

    if ctx.eight_ks:
        try:
            fetched_8ks = filings.fetch_tiered_8ks(ctx.company, ctx.eight_ks)
            result.eight_k_texts = {
                accession: text for accession, text in fetched_8ks.items() if text
            }
        except Exception as exc:
            result.errors["8-Ks"] = f"{type(exc).__name__}: {exc}"

    return result


def _filing_citation(filing: FilingRef, item: str = "") -> Citation:
    return Citation(
        form=filing.form,
        item=item,
        filed_date=filing.filing_date,
        accession=filing.accession,
        url=filing.url,
    )


def build_synthesis_context(
    wave2_result: Wave2Result, wave3_result: Wave3Result
) -> SynthesisContext | None:
    """Reshape wave2+wave3 output into synthesize.SynthesisContext.

    Returns None if there's nothing to synthesize from (no SEC filer, or no
    10-K text was fetched).
    """
    if wave2_result.company is None or wave3_result.tenk_text is None:
        return None
    eight_k_citations = {
        filing.accession: _filing_citation(filing, ",".join(filing.items))
        for filing in wave2_result.eight_ks
    }
    return SynthesisContext(
        company=wave2_result.company,
        quote=wave2_result.quote,
        financials=wave2_result.financials,
        tenk_text=wave3_result.tenk_text,
        tenk_citation=(
            _filing_citation(wave3_result.tenk_filing) if wave3_result.tenk_filing else None
        ),
        eight_k_texts=wave3_result.eight_k_texts,
        eight_k_citations=eight_k_citations,
    )


@dataclass
class BriefResult:
    """The full pipeline's output: everything retrieved, plus the narrative
    brief if synthesis was requested and succeeded.
    """

    wave2: Wave2Result
    wave3: Wave3Result
    brief: Any | None = None
    synthesis_error: str | None = None


def build_brief(ticker: str, synthesize: bool = False, client: Any | None = None) -> BriefResult:
    """Run the full pipeline for ``ticker``.

    Args:
        ticker: A stock ticker.
        synthesize: If True, also call the Claude synthesis step
            (synthesize.generate_brief). Requires ANTHROPIC_API_KEY (or an
            injected ``client``) - defaults to False so the pipeline is
            fully runnable without a key, per the package's design
            constraint.
        client: Optional Anthropic-compatible client to inject (tests only;
            production code should leave this as None and rely on
            synthesize.py's lazy construction).

    Returns:
        A BriefResult with wave2/wave3 data always populated (subject to
        their own internal failure isolation) and ``brief`` populated only
        if ``synthesize`` was True and the call succeeded.
    """
    wave2_result = wave2(ticker)
    wave3_result = wave3(wave2_result)
    result = BriefResult(wave2=wave2_result, wave3=wave3_result)

    if synthesize:
        ctx = build_synthesis_context(wave2_result, wave3_result)
        if ctx is None:
            result.synthesis_error = "no 10-K text available to synthesize from"
        else:
            try:
                result.brief = generate_brief(ctx, client=client)
            except Exception as exc:
                result.synthesis_error = f"{type(exc).__name__}: {exc}"

    return result
