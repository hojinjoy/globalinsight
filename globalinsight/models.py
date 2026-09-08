"""Typed data objects shared across the package.

Plain dataclasses rather than Pydantic: nothing here needs validation beyond
what the type checker gives us, and it keeps the non-LLM half of the package
free of an extra dependency. ``Citation`` is the load-bearing one - every
narrative claim the synthesis step emits is required to carry one, which is
the entire compliance story for this tool.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Company:
    """A resolved SEC filer (or, for an ETF-style ticker, just a ticker)."""

    ticker: str
    cik: int
    name: str


@dataclass
class Quote:
    """A point-in-time market quote. ``available=False`` on any failure -
    Yahoo's frequent 429s must never take down the rest of the brief."""

    ticker: str
    available: bool = True
    error: str | None = None
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    week52_low: float | None = None
    week52_high: float | None = None
    currency: str = "USD"
    as_of: str | None = None


@dataclass(frozen=True)
class Citation:
    """The citation trail for one fact.

    `accession` + `url` let a reader open the exact filing; `form`/`item`/
    `filed_date` let them read the citation without following the link.
    """

    form: str
    item: str
    filed_date: str
    accession: str
    url: str


@dataclass(frozen=True)
class FinancialPoint:
    """One fiscal year's value for one XBRL concept, with its source filing."""

    fiscal_year: int
    value: float
    citation: Citation


@dataclass
class FinancialSeries:
    """A concept (Revenue, NetIncomeLoss, ...) across up to 3 fiscal years."""

    concept: str
    unit: str
    points: list[FinancialPoint] = field(default_factory=list)
    stale: bool = False
    """True if this concept's newest data point lags more than a year
    behind the filer's other concepts (e.g. a filer that simply stopped
    tagging GrossProfit years ago). Set instead of silently dropping the
    series so the UI can render an explicit "not reported in recent
    filings" state rather than making a fetch failure and "filer stopped
    reporting this" look identical."""


@dataclass
class FilingRef:
    """One row from submissions.json's filings.recent parallel arrays."""

    form: str
    accession: str
    filing_date: str
    report_date: str
    primary_document: str
    url: str
    items: list[str] = field(default_factory=list)


@dataclass
class BriefSection:
    """One section of the synthesized brief: a heading, its content, and the
    citations backing every claim within it.
    """

    heading: str
    content: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Brief:
    """The full advisor brief - deliberately small so it fits one screen for
    an advisor scanning it mid-call. Every section is independently optional
    so a partial synthesis (or a citation that fails validation) degrades
    one section, not the whole brief.
    """

    ticker: str
    business_line: BriefSection
    """One sentence, cited to the 10-K: how the company makes money. Uses
    BriefSection (content/citations lists of length 0 or 1) rather than a
    bare string so an unvalidated citation degrades to "no content" the
    same way every other section does - see ``dropped_citations``."""
    take: BriefSection
    risks: BriefSection
    whats_changed: BriefSection
    dropped_citations: list[str] = field(default_factory=list)
    """Accession numbers the model cited that did not match any filing
    actually supplied in the synthesis context (a hallucinated-but-plausible
    citation). Each such bullet is dropped rather than rendered as if it
    were sourced - this list is what makes that failure visible instead of
    silent. Empty in the normal case."""
