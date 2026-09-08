"""Streamlit rendering for the backend's typed results.

Single-column brief, read top to bottom like a research note, with per-block
provenance: the blocks differ in freshness by months, so one page-level
timestamp would imply the risk factors are as current as the price.
"""

from datetime import datetime

import pandas as pd
import streamlit as st

from globalinsight.models import Brief, BriefSection, Citation, FilingRef, Quote

CONCEPT_LABELS = {
    "Revenue": "Revenue",
    "GrossProfit": "Gross profit",
    "NetIncomeLoss": "Net income",
    "EPS": "Diluted EPS",
}
CONCEPT_ORDER = ["Revenue", "GrossProfit", "NetIncomeLoss", "EPS"]

ITEM_LABELS = {
    "1.01": "Material agreement entered",
    "1.02": "Material agreement terminated",
    "2.01": "Completion of acquisition or disposition",
    "2.02": "Results of operations (earnings)",
    "2.03": "Direct financial obligation created",
    "2.05": "Costs of exit or disposal",
    "3.01": "Listing / delisting notice",
    "4.01": "Change of accountant",
    "5.02": "Director or officer departure / appointment",
    "5.07": "Shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other material events",
    "9.01": "Financial statements and exhibits",
}

BADGE = {
    "verified": ("✓", "Quote located verbatim in the filing"),
    "unverified": ("•", "Quote not located verbatim — check the source before use"),
    "metadata": ("◦", "Filing indexed by item code; its body was not fetched"),
    "unknown": ("", ""),
}


def _money(value: float | None, unit: str = "USD") -> str:
    """Large aggregates get a T/B/M suffix; per-share figures keep cents."""
    if value is None:
        return "—"
    if unit != "USD/shares":
        for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if abs(value) >= cutoff:
                return f"${value / cutoff:,.2f}{suffix}"
    return f"${value:,.2f}"


# --- wave 2 ------------------------------------------------------------------


def quote_header(quote: Quote | None, financials: dict | None = None) -> None:
    if quote is None or not quote.available:
        reason = (quote.error if quote else None) or "no quote returned"
        st.warning(
            f"Quote block unavailable — {reason}. The rest of the brief is "
            "unaffected.",
            icon="⚠️",
        )
        return

    columns = st.columns(5)
    change = None
    if quote.change is not None and quote.change_percent is not None:
        change = f"{quote.change:+.2f} ({quote.change_percent:+.2f}%)"
    columns[0].metric(
        "Price", f"${quote.price:,.2f}" if quote.price is not None else "—", change
    )
    columns[1].metric("Market cap", _money(quote.market_cap))
    # Yahoo's quoteSummary endpoint 401s often, so P/E frequently comes back
    # empty. Trailing diluted EPS is already retrieved from XBRL, so derive it
    # rather than showing a dash - and say in the caption that we did.
    ratio, derived = quote.pe_ratio, False
    if ratio is None and quote.price:
        eps_series = (financials or {}).get("EPS")
        if eps_series and eps_series.points and eps_series.points[-1].value:
            ratio, derived = quote.price / eps_series.points[-1].value, True
    columns[2].metric("P/E", f"{ratio:,.1f}" if ratio is not None else "—")
    columns[3].metric("52-week low", _money(quote.week52_low))
    columns[4].metric("52-week high", _money(quote.week52_high))

    stamp = quote.as_of or "—"
    try:
        stamp = datetime.fromisoformat(quote.as_of).strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError):
        pass
    notes = [f"Quote as of {stamp}"]
    if derived:
        eps = (financials or {})["EPS"].points[-1]
        notes.append(
            f"P/E derived from price and FY{eps.fiscal_year} diluted EPS "
            f"({eps.citation.form} filed {eps.citation.filed_date})"
        )
    if quote.market_cap is None:
        notes.append("market cap not returned by the quote endpoint")
    st.caption(" · ".join(notes))


def financial_trend(financials: dict, annual: FilingRef | None, error: str = "") -> None:
    st.markdown("#### Financial trend")
    if error:
        st.warning(error, icon="⚠️")
        return
    if not financials:
        st.info("No XBRL annual financial data reported for this filer.")
        return

    years = sorted({p.fiscal_year for s in financials.values() for p in s.points})[-3:]
    table, accessions = {}, set()
    ordered = [c for c in CONCEPT_ORDER if c in financials]
    ordered += [c for c in financials if c not in CONCEPT_ORDER]
    for concept in ordered:
        series = financials[concept]
        by_year = {point.fiscal_year: point for point in series.points}
        accessions.update(p.citation.accession for p in series.points)
        table[CONCEPT_LABELS.get(concept, concept)] = [
            _money(by_year[year].value, series.unit) if year in by_year else "—"
            for year in years
        ]
    frame = pd.DataFrame(table, index=[f"FY{year}" for year in years]).T
    st.dataframe(frame, width="stretch")

    source = ""
    if annual:
        source = f"[{annual.form} filed {annual.filing_date}]({annual.url}) · "
    st.caption(
        "Retrieved from SEC XBRL structured data, never model-generated · "
        f"{source}accession {', '.join(sorted(accessions))}"
    )


def filings_index(annual: FilingRef | None, eight_ks: list[FilingRef]) -> None:
    if not eight_ks:
        return
    with st.expander(
        f"{len(eight_ks)} 8-K filings since the {annual.form if annual else 'annual report'}"
    ):
        for filing in eight_ks:
            described = ", ".join(
                f"**{code}** {ITEM_LABELS.get(code, 'Other')}" for code in filing.items
            )
            st.markdown(
                f"[{filing.form} · {filing.filing_date}]({filing.url})"
                + (f" — {described}" if described else "")
            )


# --- wave 3 ------------------------------------------------------------------


def citation_popover(citation: Citation, quote: str = "", status: str = "unknown") -> None:
    mark, tooltip = BADGE.get(status, ("", ""))
    item = f", Item {citation.item}" if citation.item else ""
    label = f"↳ {citation.form}{item}, filed {citation.filed_date} {mark}"
    with st.popover(label, width="content"):
        if quote:
            st.markdown(f"> {quote}")
        st.markdown(f"[Open filing on EDGAR]({citation.url})")
        st.caption(f"Accession {citation.accession}" + (f" · {tooltip}" if tooltip else ""))


def brief_section(section: BriefSection, bullet: bool = True) -> None:
    st.markdown(f"#### {section.heading}")
    if not section.content:
        st.caption("Nothing reported for this section.")
        return
    for index, text in enumerate(section.content):
        st.markdown(f"- {text}" if bullet else text)
        if index < len(section.citations):
            citation_popover(section.citations[index])


def narrative(brief: Brief) -> None:
    # business_line is a single sentence, so it renders unbulleted as a lead-in.
    brief_section(brief.business_line, bullet=False)
    for section in (brief.take, brief.risks, brief.whats_changed):
        brief_section(section)


def provenance(annual: FilingRef | None, eight_ks: list[FilingRef], seconds: float) -> None:
    scope = f"{annual.form} filed {annual.filing_date}" if annual else "no annual report"
    if eight_ks:
        scope += f" + {len(eight_ks)} 8-Ks through {eight_ks[0].filing_date}"
    st.caption(f"Narrative grounded in {scope} · synthesised in {seconds:.0f}s")


def answer(ans) -> None:
    # A refused question never reached the model, so there is nothing to cite
    # and no usage to report. Showing the "0s · $0.00 · 0/0 verified" footer
    # under it would dress a policy refusal up as a checked answer.
    if getattr(ans, "refusal_reason", ""):
        st.warning(ans.text, icon="🚫")
        return

    st.markdown(ans.text)
    if ans.unsupported:
        st.info(ans.unsupported, icon="ℹ️")
    for claim in ans.claims:
        citation_popover(claim.citation, claim.quote, claim.status)

    parts = [f"{ans.seconds:.0f}s", f"${ans.cost:.2f}"]
    if ans.cache_read:
        parts.append(f"{ans.cache_read:,} tokens read from cache")
    elif ans.cache_write:
        parts.append(f"{ans.cache_write:,} tokens written to cache")
    # Only claims citing a filing whose body was fetched can have their quote
    # checked; metadata-only citations are excluded from the ratio.
    checkable = [c for c in ans.claims if c.status in ("verified", "unverified")]
    if checkable:
        verified = sum(1 for c in checkable if c.status == "verified")
        parts.append(f"{verified}/{len(checkable)} quotes verified")
    metadata_only = sum(1 for c in ans.claims if c.status == "metadata")
    if metadata_only:
        parts.append(f"{metadata_only} cited by item code only")
    st.caption(" · ".join(parts))
