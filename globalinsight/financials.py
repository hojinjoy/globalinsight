"""XBRL companyfacts -> a 3-fiscal-year financial trend.

Every value here is *retrieved*, not generated - this is the module that
makes it structurally impossible for the LLM synthesis step to hallucinate a
revenue figure, and it's why every point carries its source accession, form,
and filed date (the citation trail).
"""

from datetime import date

from . import http
from .config import TTL_COMPANYFACTS
from .models import Citation, FinancialPoint, FinancialSeries


def _filing_index_url(cik: int, accession: str) -> str:
    """The human-readable filing index page for an accession number."""
    if not accession:
        return ""
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"
        f"{accession}-index.htm"
    )

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Full-year figures are only ever reported on these forms. The "/A" amendment
# forms are included so a restatement filed by amendment isn't ignored -
# _annual_points' later-filed-wins logic already handles picking the
# restated value over the original once both forms are in scope.
_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A")

# Revenue's concept name varies by filer/era; every candidate tag is
# evaluated and the one holding the MOST RECENT annual data wins (not simply
# the first tag with any data at all - see get_financials). Net income and
# gross profit are far more standardized, so each keeps a single tag with a
# couple of very old synonyms as a fallback.
_CONCEPT_TAGS: dict[str, tuple[str, ...]] = {
    "Revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "NetIncomeLoss": ("NetIncomeLoss",),
    "GrossProfit": ("GrossProfit",),
    "EPS": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
}

# IFRS analogues of the same concepts, for the ~us-gaap-only foreign private
# issuers (TSM, ASML, SAP, Shell, Toyota, ...) that file 20-F/40-F under
# ifrs-full instead of us-gaap. Without this, every IFRS filer silently
# yields an empty dict even though _ANNUAL_FORMS already includes 20-F/40-F.
_CONCEPT_TAGS_IFRS: dict[str, tuple[str, ...]] = {
    "Revenue": ("Revenue",),
    "NetIncomeLoss": ("ProfitLoss",),
    "GrossProfit": ("GrossProfit",),
    "EPS": ("BasicEarningsLossPerShare", "DilutedEarningsLossPerShare"),
}


def get_companyfacts(cik: int, ttl: float | None = TTL_COMPANYFACTS) -> dict:
    """Fetch and parse companyfacts.json for a CIK (cached 24h)."""
    url = COMPANYFACTS_URL.format(cik=str(cik).zfill(10))
    return http.get_json(url, ttl=ttl)


def _fiscal_year_labels(facts_by_taxonomy: list[dict]) -> dict[str, int]:
    """Map a fiscal period's end date (ISO string) to the filer's OWN fiscal
    year label - not a value derived from a hardcoded month cutoff.

    dei:DocumentFiscalYearFocus (the number printed on a filing's cover
    page) is the textbook source for this, but SEC's companyfacts API does
    not actually surface it in practice (verified empirically against live
    NVDA/WMT/AAPL/ORCL companyfacts - their `facts.dei` only ever contains
    EntityCommonStockSharesOutstanding/EntityPublicFloat). What IS present
    on every fact, though, is `fy`: SEC's own fiscal-year-of-the-filing
    number, computed from that same dei tag at ingestion time. The task's
    warning against using `fy` directly is correct - every fact in a filing
    carries the SAME `fy` value regardless of which period it covers, so a
    naive per-entry lookup mislabels prior-year comparatives with the
    current filing's fy (e.g. NVDA's fiscal-2026 10-K tags its FY2024,
    FY2025, *and* FY2026 comparative figures all with fy=2026).

    The fix: for each accession, only the entry whose period `end` is the
    LATEST within that filing is genuinely "the current period" - and only
    for that one entry is its `fy` value trustworthy. Doing this across a
    filer's whole filing history reconstructs a full end-date -> label map
    with no month-based guessing, and - verified against live data below -
    correctly reproduces BOTH conventions actually in use: NVDA/WMT/ORCL
    label by the year the period ENDS in (fy=2026 for the period ending
    2026-01-25), while Target labels by the year the period mostly OVERLAPS
    (fy=2023 for the period ending 2024-02-03) - a distinction no fixed
    month cutoff can get right for both.
    """
    accn_periods: dict[str, list[tuple[str, int, str]]] = {}
    for taxonomy_facts in facts_by_taxonomy:
        for tag_data in taxonomy_facts.values():
            for unit_entries in tag_data.get("units", {}).values():
                for entry in unit_entries:
                    if entry.get("form") not in _ANNUAL_FORMS:
                        continue
                    end_raw = entry.get("end")
                    accn = entry.get("accn")
                    fy = entry.get("fy")
                    if not end_raw or not accn or fy is None:
                        continue
                    accn_periods.setdefault(accn, []).append((end_raw, fy, entry.get("filed", "")))

    labels: dict[str, tuple[str, int]] = {}  # end date -> (filed, fy)
    for periods in accn_periods.values():
        end_raw, fy, filed = max(periods, key=lambda p: p[0])  # this filing's own current period
        try:
            fy_int = int(fy)
        except (TypeError, ValueError):
            continue
        existing = labels.get(end_raw)
        if existing is None or filed >= existing[0]:
            labels[end_raw] = (filed, fy_int)
    return {end: fy for end, (_filed, fy) in labels.items()}


def _annual_points(entries: list[dict], cik: int, fy_labels: dict[str, int]) -> list[FinancialPoint]:
    """Keep only full-year (~365-day) values reported on an annual form.

    XBRL facts include quarterly, YTD, and restated figures all mixed
    together; a duration close to a year on a 10-K/20-F/40-F is the
    reliable signal that a given entry is "the" annual figure, not a Q3
    year-to-date subtotal that happens to also be tagged with this concept.
    """
    by_year: dict[int, FinancialPoint] = {}
    for entry in entries:
        if entry.get("form") not in _ANNUAL_FORMS:
            continue
        start_raw, end_raw = entry.get("start"), entry.get("end")
        if not start_raw or not end_raw:
            continue
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            continue
        if not 300 <= (end - start).days <= 400:
            continue
        if end.month > 5:
            # Unambiguous: no filer names a period ending Jun-Dec by
            # anything other than the calendar year it ends in. Skipping
            # the fy_labels lookup here also sidesteps real SEC data
            # quirks where a filing's own `fy` metadata is occasionally
            # wrong (observed on SHOP) - safe to do since this branch never
            # needed disambiguating in the first place.
            fiscal_year = end.year
        else:
            # Genuinely ambiguous (this is where the H1 bug lived): a
            # Jan-May period end is labeled by different filers using
            # opposite conventions (NVDA/WMT/ORCL name it by the ending
            # year; Target-style retailers name it by the year it mostly
            # overlaps) - only the filer's own data can disambiguate.
            fiscal_year = fy_labels.get(end_raw)
            if fiscal_year is None:
                # No trusted label available for this exact period end
                # (e.g. pre-XBRL-mandate history) - fall back to the
                # historical (imperfect) heuristic rather than guessing.
                fiscal_year = end.year - 1
        filed = entry.get("filed", "")
        point = FinancialPoint(
            fiscal_year=fiscal_year,
            value=float(entry["val"]),
            citation=Citation(
                form=entry["form"],
                item="",
                filed_date=filed,
                accession=entry.get("accn", ""),
                url=_filing_index_url(cik, entry.get("accn", "")),
            ),
        )
        # A later-filed value for the same fiscal year is a restatement -
        # prefer it over the originally reported figure. Tie-break on
        # accession (not array order) so two same-day filings resolve
        # deterministically rather than by whichever happened to be listed
        # first in the source JSON.
        existing = by_year.get(fiscal_year)
        if existing is None or (filed, entry.get("accn", "")) > (
            existing.citation.filed_date,
            existing.citation.accession,
        ):
            by_year[fiscal_year] = point
    return [by_year[year] for year in sorted(by_year)]


def _candidate_units(tag_units: dict, is_per_share: bool) -> list[str]:
    """Every unit key plausibly holding this concept's values.

    Never hardcodes a currency: a monetary concept accepts any bare
    currency-code unit ("USD", "TWD", "EUR", ...) and a per-share concept
    accepts any "<currency>/shares" unit. Hardcoding "USD" (the old
    behavior) is exactly what silently zeroed out every IFRS foreign
    private issuer (TSM, ASML, SAP, Shell, Toyota, ...) that reports in its
    own functional currency.
    """
    return [key for key in tag_units if ("/" in key) == is_per_share]


def _best_series(
    concept: str,
    facts_by_taxonomy: list[dict],
    tags_by_taxonomy: list[tuple[str, ...]],
    fy_labels: dict[str, int],
    cik: int,
) -> tuple[str, list[FinancialPoint]] | None:
    """Across every candidate tag, taxonomy, and unit for ``concept``, pick
    the one whose data is most RECENT (not the first one that has any data
    at all - that first-match ordering is exactly the C1/C2 bug family:
    a filer that renamed or stopped using a tag still has old data sitting
    under it, and a fixed preference order finds that stale data first and
    never looks further).
    """
    is_per_share = concept == "EPS"
    best_key: tuple[int, str, str] | None = None
    best_points: list[FinancialPoint] | None = None
    best_unit: str | None = None
    for taxonomy_facts, tags in zip(facts_by_taxonomy, tags_by_taxonomy):
        for tag in tags:
            tag_units = taxonomy_facts.get(tag, {}).get("units", {})
            for unit_key in _candidate_units(tag_units, is_per_share):
                points = _annual_points(tag_units[unit_key], cik, fy_labels)
                if not points:
                    continue
                last = points[-1]
                key = (last.fiscal_year, last.citation.filed_date, last.citation.accession)
                if best_key is None or key > best_key:
                    best_key = key
                    best_points = points
                    best_unit = unit_key
    if best_points is None or best_unit is None:
        return None
    return best_unit, best_points


def _mark_stale_series(result: dict[str, FinancialSeries]) -> dict[str, FinancialSeries]:
    """Hard recency gate (C2): flag any concept whose newest fiscal year
    lags more than one year behind the newest fiscal year seen across the
    filer's other concepts.

    This is a distinct defect from C1 and is NOT fixed by the tag-selection
    fix above: it also occurs with a single-tag concept (ORCL's GrossProfit
    - one tag, last reported in fiscal 2017 - rendered beside a fiscal 2025
    Revenue series with no second tag to fall back to). Tag ordering is
    irrelevant here; the only defense is a recency floor applied after each
    series is built, so a filer that simply stopped tagging a concept years
    ago never gets rendered as if it were contemporaneous with the rest of
    the brief.

    The series is kept in the result (with ``stale=True``) rather than
    dropped: an advisor-facing UI needs to be able to say "GrossProfit not
    reported in recent filings" explicitly, which a missing dict key can't
    distinguish from "this filer never reports GrossProfit at all" or from
    a fetch failure.
    """
    latest_by_concept = {
        concept: series.points[-1].fiscal_year
        for concept, series in result.items()
        if series.points
    }
    if not latest_by_concept:
        return result
    newest_fy = max(latest_by_concept.values())
    for concept, series in result.items():
        latest = latest_by_concept.get(concept)
        series.stale = latest is not None and newest_fy - latest > 1
    return result


def latest_annual_report_fiscal_year(submissions: dict) -> int | None:
    """Best-effort fiscal year of the most recently *filed* annual report
    (10-K/20-F/40-F, amendments included) according to submissions.json.

    This is deliberately independent of ``get_financials()``/companyfacts -
    the whole point is to have a second, unrelated source of "how current
    is this filer's annual data" so a gap between the two is detectable
    (see ``mark_absolute_staleness``). submissions.json and companyfacts.json
    are two different SEC systems that can (and, confirmed live on TSM, do)
    disagree about how current a filer's data is.

    The fiscal year is taken from the filing's own reportDate (the period a
    filing covers, e.g. "2025-12-31") rather than any of the careful
    per-point fy-label reconstruction in ``_fiscal_year_labels`` - this is a
    coarse absolute sanity check, not a value that ends up cited to an
    advisor, so being off by one for an unusual (e.g. Jan/Feb) fiscal year
    end is an acceptable tradeoff for not needing a second XBRL fetch just
    to answer "roughly how new is the newest annual filing".

    Args:
        submissions: Parsed submissions JSON from edgar.get_submissions().

    Returns:
        The fiscal year (as an int) of the newest annual filing on record,
        or None if submissions.json has no filings.recent data or no
        10-K/20-F/40-F filing at all.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    n = len(forms)
    report_dates = recent.get("reportDate", [""] * n)
    filing_dates = recent.get("filingDate", [""] * n)

    best: tuple[str, int] | None = None  # (filing_date, fiscal_year)
    for i in range(n):
        if forms[i] not in _ANNUAL_FORMS:
            continue
        source_date = (report_dates[i] if i < len(report_dates) else "") or (
            filing_dates[i] if i < len(filing_dates) else ""
        )
        if not source_date:
            continue
        try:
            fiscal_year = int(source_date[:4])
        except ValueError:
            continue
        filed = filing_dates[i] if i < len(filing_dates) else ""
        if best is None or filed > best[0]:
            best = (filed, fiscal_year)
    return best[1] if best else None


def mark_absolute_staleness(
    result: dict[str, FinancialSeries], submissions: dict
) -> dict[str, FinancialSeries]:
    """Absolute recency gate: flag every concept as stale if submissions.json
    shows a newer annual filing than any fiscal year present in ``result``.

    ``_mark_stale_series`` (the existing check) is purely RELATIVE - it can
    only catch a concept lagging behind the filer's *other* concepts, so a
    filer whose entire companyfacts is uniformly behind (every concept
    agrees with every other) sails through it with nothing flagged. This is
    a confirmed live case, not a hypothetical: TSM's companyfacts currently
    tops out at FY2024 (filed 2025-04-17) even though TSM filed a FY2025
    20-F on 2026-04-16 - SEC simply hasn't back-filled XBRL facts for it
    yet. Without this check, an advisor would see year-old TSM numbers
    presented with the same unqualified confidence as a filer that's fully
    current.

    This is a SEC-side data lag, not a code defect - fetching harder or
    retrying cannot produce facts SEC hasn't ingested yet. The only
    obligation this function discharges is making the lag visible rather
    than silent, by (re)using the same ``stale`` flag the relative check
    already exposes to the UI.

    Args:
        result: The dict returned by ``get_financials()`` (mutated in
            place - every series has ``.stale`` set to True if triggered).
        submissions: Parsed submissions JSON from edgar.get_submissions()
            for the same filer.

    Returns:
        ``result``, for convenience chaining - the same object, mutated.
    """
    if not result:
        return result
    latest_annual_fy = latest_annual_report_fiscal_year(submissions)
    if latest_annual_fy is None:
        return result
    newest_fy_in_data = max(
        (series.points[-1].fiscal_year for series in result.values() if series.points),
        default=None,
    )
    if newest_fy_in_data is None:
        return result
    if latest_annual_fy > newest_fy_in_data:
        for series in result.values():
            series.stale = True
    return result


def get_financials(cik: int, years: int = 3) -> dict[str, FinancialSeries]:
    """Last ``years`` fiscal years of Revenue, NetIncomeLoss, GrossProfit, EPS.

    Args:
        cik: The company's SEC CIK number.
        years: How many trailing fiscal years to keep (default 3).

    Returns:
        A dict keyed by concept name ("Revenue", "NetIncomeLoss",
        "GrossProfit", "EPS") to a FinancialSeries. A concept absent from
        the filer's XBRL facts entirely (e.g. many filers never break out
        GrossProfit) is simply missing from the dict - never a raised
        error. A concept that IS present but whose newest data is stale
        relative to the filer's other concepts (see _mark_stale_series) is
        still returned, with ``stale=True``, so the caller can render it as
        explicitly out of date rather than making that indistinguishable
        from a fetch failure or genuine no-data filer.

    Raises:
        Whatever the underlying companyfacts fetch raises (network error,
        bad JSON, etc.) - this is intentional (H4): a genuine fetch failure
        must be distinguishable from "this filer has no XBRL data", so
        pipeline.wave2() can record it in errors["financials"] instead of
        it silently looking identical to a no-data filer. wave2() already
        isolates this failure so it doesn't blank the rest of the brief.
    """
    raw = get_companyfacts(cik)
    all_facts = raw.get("facts", {})
    us_gaap = all_facts.get("us-gaap", {})
    ifrs = all_facts.get("ifrs-full", {})
    fy_labels = _fiscal_year_labels([us_gaap, ifrs])

    result: dict[str, FinancialSeries] = {}
    for concept, us_gaap_tags in _CONCEPT_TAGS.items():
        ifrs_tags = _CONCEPT_TAGS_IFRS.get(concept, ())
        best = _best_series(
            concept,
            [us_gaap, ifrs],
            [us_gaap_tags, ifrs_tags],
            fy_labels,
            cik,
        )
        if best is not None:
            unit, points = best
            result[concept] = FinancialSeries(concept=concept, unit=unit, points=points[-years:])

    return _mark_stale_series(result)
