"""Document fetching for 10-Ks and 8-Ks, with the 8-K tier policy.

Two facts drive everything here:

- An 8-K's primaryDocument is a STUB. It is the cover-page form (item
  checkboxes, signature block) and carries essentially no content - measured
  at ~997 tokens on a real filing. The substance (a press release, prepared
  remarks) lives in the *exhibits* (EX-99.1, EX-99.2, ...), which are
  separate files only discoverable via the filing's index.json. Fetching
  only primaryDocument is the single easiest bug to introduce in this
  module: it "works" (no error, returns text) and silently returns nothing
  useful. fetch_8k_text() therefore always reads index.json and fetches
  every .htm document in the filing except the XBRL-viewer artifacts.
- Not every 8-K deserves the same budget. Item 2.02 (earnings) is always
  worth reading in full; 5.02/1.01/2.03 are worth reading but capped; and
  the catch-all items (8.01/7.01/5.07) are frequently pure boilerplate (one
  real NVDA 8.01 filing measured at 92,430 tokens - 72% of that quarter's
  entire 8-K content - and contained no new information). Those are
  recorded as metadata (form, item codes, date) but their bodies are never
  fetched.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import clean, edgar, http
from .config import (
    MAX_8K_WORKERS,
    TIER1_CAP_TOKENS,
    TIER1_ITEMS,
    TIER2_CAP_TOKENS,
    TIER2_ITEMS,
    TIER3_ITEMS,
    TTL_PERMANENT,
)
from .models import Company, FilingRef

# XBRL-viewer-generated renderings of the financial statements (R1.htm,
# R2.htm, ...) - auto-generated from the XBRL data, not exhibit content, and
# would otherwise duplicate/pollute the real narrative text.
_XBRL_VIEWER_RE = re.compile(r"^R\d+\.htm$", re.IGNORECASE)


def fetch_10k_text(company: Company) -> tuple[str, FilingRef] | None:
    """Cleaned full text of the latest 10-K (or 20-F/40-F for non-US filers).

    Deliberately sends the WHOLE cleaned document rather than trying to
    isolate "Item 1A" / "Item 7" by regex: tested against real filings
    (NVDA, MSFT, JPM, and one more), it failed on 3 of 4, because a real
    section heading and a cross-reference like "see Item 1A, Risk Factors"
    are byte-identical once HTML formatting is stripped. The full document
    fits comfortably in a 1M-token context (largest measured: JPM at ~353k
    tokens), so there's no compression to buy here worth the false-negative
    risk.

    Args:
        company: The resolved Company to fetch a 10-K for.

    Returns:
        A (cleaned_text, filing) tuple, or None if the filer has no annual
        report on file in any of 10-K/20-F/40-F (e.g. a fresh IPO that has
        only filed an S-1 so far).
    """
    subs = edgar.get_submissions(company.cik)
    filing = (
        edgar.latest_filing(subs, "10-K")
        or edgar.latest_filing(subs, "20-F")
        or edgar.latest_filing(subs, "40-F")
    )
    if filing is None or not filing.primary_document:
        return None
    raw_html = http.get_text(filing.url, ttl=TTL_PERMANENT)
    return clean.html_to_text(raw_html), filing


def _list_exhibit_documents(cik: int, accession: str) -> list[str]:
    """Every .htm document in a filing's index.json, minus XBRL viewer artifacts."""
    index = http.get_json(edgar.index_json_url(cik, accession), ttl=TTL_PERMANENT)
    names = [
        item["name"]
        for item in index.get("directory", {}).get("item", [])
        if item.get("name", "").lower().endswith(".htm")
    ]
    return [name for name in names if not _XBRL_VIEWER_RE.match(name)]


def determine_tier(item_codes: list[str]) -> int:
    """Classify an 8-K by its item codes into tier 1 (always fetch), 2
    (fetch, capped) or 3 (metadata only).

    An 8-K with no item code in any configured tier is treated as tier 3
    (metadata only) rather than fetched in full - a conservative default,
    since the untiered items are rarely where advisor-relevant narrative
    lives and the two explicit tiers already cover earnings and the other
    disclosures that matter for "what's changed" framing.
    """
    codes = set(item_codes)
    if codes & TIER1_ITEMS:
        return 1
    if codes & TIER2_ITEMS:
        return 2
    return 3


def truncate_middle(text: str, max_tokens: int) -> str:
    """Truncate ``text`` to ``max_tokens`` (approx), dropping the MIDDLE.

    Not the tail: the top of a press release carries the headline numbers
    and framing, the bottom carries financial tables/exhibits, and the
    narrative padding a cap needs to shed sits in the middle. Truncating
    from the tail (the obvious, "simpler" choice) throws away the tables
    instead. A truncation marker names how much was cut so the LLM (and a
    human reader) knows content is missing rather than assuming the
    document just ended there.
    """
    total_tokens = clean.count_tokens_approx(text)
    if total_tokens <= max_tokens:
        return text
    keep_chars = max_tokens * 4
    head_chars = keep_chars // 2
    tail_chars = keep_chars - head_chars
    dropped_tokens = total_tokens - max_tokens
    marker = f"\n\n[... truncated {dropped_tokens} tokens ...]\n\n"
    tail = text[-tail_chars:] if tail_chars else ""
    return text[:head_chars] + marker + tail


def _fetch_8k_body(company: Company, filing: FilingRef) -> tuple[str, list[str]] | None:
    """Shared implementation behind fetch_8k_text() / fetch_8k_text_checked().

    Returns (cleaned_tiered_text, exhibit_document_names), or None for a
    tier-3 filing. Exposing ``exhibit_document_names`` alongside the text is
    what lets detect_stub_regression() tell "no separate exhibit existed"
    apart from "the text is just short".
    """
    tier = determine_tier(filing.items)
    if tier == 3:
        return None
    cap = TIER1_CAP_TOKENS if tier == 1 else TIER2_CAP_TOKENS

    names = _list_exhibit_documents(company.cik, filing.accession)
    chunks: list[str] = []
    for name in names:
        try:
            raw_html = http.get_text(
                edgar.document_url(company.cik, filing.accession, name),
                ttl=TTL_PERMANENT,
            )
        except Exception:
            continue  # one bad exhibit must not sink the whole 8-K
        cleaned = clean.html_to_text(raw_html)
        if cleaned:
            chunks.append(cleaned)

    full_text = "\n\n".join(chunks)
    return truncate_middle(full_text, cap), names


def fetch_8k_text(company: Company, filing: FilingRef) -> str | None:
    """Cleaned, tier-capped text of one 8-K's exhibits.

    Reads index.json for the accession and fetches every .htm document
    except XBRL-viewer artifacts (see module docstring) - never just
    primaryDocument, which is a content-free stub.

    Args:
        company: The filer.
        filing: The 8-K's FilingRef, with `.items` populated.

    Returns:
        The concatenated, cleaned, tier-capped exhibit text, or None if the
        filing's item codes classify it as tier 3 (metadata only - body
        intentionally not fetched).
    """
    result = _fetch_8k_body(company, filing)
    return result[0] if result is not None else None


STUB_REGRESSION_TOKEN_FLOOR = 1500
"""Below this cleaned-token count, a tier-1/2 8-K whose only fetched
document was the cover-page stub (filing.primary_document) is flagged by
detect_stub_regression() as a likely silent exhibit-fetch miss rather than
a genuinely low-content filing. Not zero: a legitimate case exists (RIVN,
2026-07-06, item 2.02, 2,177 tokens - content genuinely inline on the
primary document, no separate exhibit filed at all), so this is a soft
floor for a visible warning, never a hard failure."""


def detect_stub_regression(filing: FilingRef, exhibit_names: list[str], text: str) -> str | None:
    """Tripwire for the silent 8-K-stub regression (see module docstring).

    An 8-K's primaryDocument is a content-free cover-page stub (~997 tokens
    measured on a real filing) - the exhibit-fetching mechanism in this
    module is what's supposed to find the real content (EX-99.*). Nothing
    previously *detected* a miss: fetching only the stub "works" (no
    exception, returns text) and silently ships an empty "What's Changed".

    Args:
        filing: The 8-K's FilingRef (tier 1/2 - callers should not call this
            for tier-3 filings, which never fetch a body at all).
        exhibit_names: The .htm document names index.json listed for this
            accession (as returned by _fetch_8k_body / _list_exhibit_documents).
        text: The cleaned, tier-capped text fetch_8k_text() produced.

    Returns:
        A human-readable warning if the only document fetched was the
        primary-document stub AND the resulting text is under
        STUB_REGRESSION_TOKEN_FLOOR tokens. None otherwise - including the
        legitimate case where the only document is the primary one but it
        genuinely carries real, higher-token-count content (e.g. RIVN's
        2026-07-06 8-K, item 2.02, 2,177 tokens, content inline).
    """
    if exhibit_names != [filing.primary_document]:
        return None
    tokens = clean.count_tokens_approx(text)
    if tokens >= STUB_REGRESSION_TOKEN_FLOOR:
        return None
    return (
        f"{filing.accession} ({filing.form}, items {','.join(filing.items) or 'none'}): "
        f"only primary_document ({filing.primary_document!r}) was fetched and the "
        f"cleaned text is {tokens} tokens (< {STUB_REGRESSION_TOKEN_FLOOR}) - possible "
        "silent exhibit-fetch miss, not confirmed real content"
    )


def fetch_8k_text_checked(company: Company, filing: FilingRef) -> tuple[str | None, str | None]:
    """Like fetch_8k_text(), but also runs the stub-regression tripwire.

    Returns:
        (text, warning) - ``text`` is exactly what fetch_8k_text() would
        return; ``warning`` is detect_stub_regression()'s verdict (or None).
        Both are None for a tier-3 filing.
    """
    result = _fetch_8k_body(company, filing)
    if result is None:
        return None, None
    text, names = result
    return text, detect_stub_regression(filing, names, text)


def fetch_tiered_8ks(
    company: Company, eight_ks: list[FilingRef]
) -> dict[str, str | None]:
    """Fetch tier 1/2 8-K bodies concurrently; tier 3 stays metadata-only.

    Fans out with a bounded pool (max 5 workers, config.MAX_8K_WORKERS) -
    this is safe with respect to SEC's 10 req/sec limit because every
    worker's requests still funnel through http.py's single shared token
    bucket, not a per-worker one.

    Args:
        company: The filer.
        eight_ks: The 8-Ks to fetch (typically edgar.eight_ks_since()'s
            output).

    Returns:
        A dict from accession number to fetched text (or None for a tier-3
        filing, or a filing whose fetch failed - a single failed 8-K must
        not take down the others).
    """
    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=MAX_8K_WORKERS) as pool:
        future_to_accession = {
            pool.submit(fetch_8k_text, company, filing): filing.accession
            for filing in eight_ks
        }
        for future in as_completed(future_to_accession):
            accession = future_to_accession[future]
            try:
                results[accession] = future.result()
            except Exception:
                results[accession] = None
    return results


def fetch_tiered_8ks_with_warnings(
    company: Company, eight_ks: list[FilingRef]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Like fetch_tiered_8ks(), but also runs the M5 stub-regression tripwire
    (detect_stub_regression) on every fetch.

    Returns:
        (texts, warnings) - ``texts`` is exactly fetch_tiered_8ks()'s return
        value; ``warnings`` maps accession -> detect_stub_regression()'s
        message, present only for filings that tripped it (a visible
        warning, not a hard failure - the filing's text is still returned
        in ``texts`` either way).

    Callers assembling a brief should surface ``warnings`` somewhere visible
    (e.g. pipeline.Wave3Result.errors, keyed like
    ``f"8-K stub:{accession}"``) so a silent exhibit-fetch miss on a
    tier-1/2 filing shows up instead of quietly shipping an empty "What's
    Changed". Not wired into pipeline.py from here - Wave3Result lives
    outside this module; this function is the integration point for
    whoever owns that wiring.
    """
    texts: dict[str, str | None] = {}
    warnings: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_8K_WORKERS) as pool:
        future_to_filing = {
            pool.submit(fetch_8k_text_checked, company, filing): filing
            for filing in eight_ks
        }
        for future in as_completed(future_to_filing):
            filing = future_to_filing[future]
            try:
                text, warning = future.result()
            except Exception:
                text, warning = None, None
            texts[filing.accession] = text
            if warning:
                warnings[filing.accession] = warning
    return texts, warnings
