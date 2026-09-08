"""The one LLM call in this package: cited-brief synthesis.

There is currently no ANTHROPIC_API_KEY in the environment this was built
in, so this module cannot be exercised end-to-end here. To keep it testable
anyway:

- The Anthropic client is never constructed at import time or as a module
  global - only lazily, inside ``_client()``, the first time a real call is
  made. Importing this module (or any other module in the package) must
  never require credentials.
- ``generate_brief`` takes an optional ``client`` parameter. Tests inject a
  fake/mock client and never touch ``_client()`` at all, so the whole
  prompt-building / response-parsing path is unit-testable without a key.
- Structured output (``output_config.format``) is used instead of asking
  for prose and hoping citations show up - the response schema *requires*
  a citation object on every claim-bearing item, so an uncited claim is not
  a shape the model is even allowed to emit.

The output is deliberately small: an advisor scans this mid-call, they
don't read it. See ``BRIEF_SCHEMA`` - the bullet arrays are capped at 3
items each (structurally, via ``maxItems``/``minItems``, not just by asking
politely in the prose) so the brief fits one screen with no scrolling.

Citation integrity is enforced twice, independently:

1. Shape - the JSON schema forces every claim-bearing item to carry a
   citation object with the right fields (this module never trusted prose).
2. Content - ``_parse_brief`` cross-checks every citation the model emits
   against an allow-set built from the filings actually supplied in
   ``SynthesisContext`` (the 10-K + each 8-K), and overwrites the model's
   copy of the citation with the canonical record on a match. A schema-valid
   but fabricated accession does NOT pass (2) even though it passes (1) -
   see ``_resolve_citation``. This is the fix for the historical gap where
   schema compliance was mistaken for citation integrity.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from .config import SYNTHESIS_EFFORT, SYNTHESIS_MAX_TOKENS, SYNTHESIS_MODEL
from .models import Brief, BriefSection, Citation, Company, FinancialSeries, Quote

SYSTEM_PROMPT = """You are a research assistant preparing a pre-call briefing \
for a financial advisor about a public company. The advisor is scanning this \
DURING a live client call, not reading it beforehand - if it doesn't fit on \
one screen with no scrolling, it has already failed at its job.

Formatting rules (hard requirements, not suggestions):
- Every bullet (business_line, take, risks, whats_changed) is ONE LINE: \
roughly 120 characters or fewer. Write it the way you'd say it out loud in \
five seconds, not the way you'd write it in a report.
- "take": exactly 3 bullets. "risks": exactly 3 bullets, ranked most-material \
first. "whats_changed": up to 3 bullets, empty list if no 8-Ks were supplied.
- Prefer a concrete number over a qualitative adjective in every bullet. Not \
"revenue grew strongly" - "revenue grew 94% YoY to $130.5B". Not "margins \
compressed" - "gross margin fell 6.2 points to 61%".

Rules:
- Every factual claim you make about the business, its risks, or recent \
developments MUST be attached to a citation drawn from the supplied filings. \
Never state a fact you cannot cite.
- Never invent or restate specific financial figures (revenue, net income, \
EPS, etc.) beyond what is given to you in the FINANCIALS block - that data \
is retrieved from structured filings, not something you should recompute or \
round further. Quote it at the precision it was given to you.
- A citation's "item" field is the 8-K item code (e.g. "2.02") - copy it \
from the "item" attribute on the <filing> tag the passage came from. The \
10-K is supplied as a single undivided document with no per-Item \
attribution available to you, so leave "item" null for any 10-K citation. \
Never guess or invent an Item number.
- "whats_changed" must be grounded only in the supplied 8-K filings; if none \
were supplied, return an empty list. Never cite an 8-K to back a claim that \
belongs in "business" or "risks", or vice versa.
- Do not give investment advice, price targets, or buy/sell recommendations. \
This is informational briefing material only.
"""


@dataclass
class SynthesisContext:
    """Everything the synthesis prompt is built from - the output of
    pipeline.wave2() + pipeline.wave3(), reshaped for the LLM call.
    """

    company: Company
    quote: Quote | None
    financials: dict[str, FinancialSeries] = field(default_factory=dict)
    tenk_text: str | None = None
    tenk_citation: Citation | None = None
    eight_k_texts: dict[str, str] = field(default_factory=dict)
    """accession -> cleaned exhibit text, tier 1/2 only (tier 3 has no body)."""
    eight_k_citations: dict[str, Citation] = field(default_factory=dict)
    """accession -> Citation, for every 8-K including tier 3 (metadata-only)."""


# --- JSON schema for structured output ---------------------------------------

_CITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "form": {"type": "string"},
        "item": {
            "type": ["string", "null"],
            "description": (
                "8-K item code (e.g. '2.02'), copied from the filing's item "
                "attribute. Null for a 10-K citation - the 10-K is supplied "
                "whole-document, so no specific Item can be attributed. "
                "Never fabricate a value here."
            ),
        },
        "filed_date": {"type": "string"},
        "accession": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["form", "item", "filed_date", "accession", "url"],
    "additionalProperties": False,
}

_CITED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "citation": _CITATION_SCHEMA,
    },
    "required": ["text", "citation"],
    "additionalProperties": False,
}


def _cited_list_schema(description: str, *, min_items: int, max_items: int) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": _CITED_ITEM_SCHEMA,
        "minItems": min_items,
        "maxItems": max_items,
    }


BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "business_line": {
            "type": "object",
            "description": (
                "Exactly ONE sentence: how the company makes money. Cited "
                "to the 10-K. This is the lead-in an advisor reads first."
            ),
            "properties": {
                "text": {"type": "string"},
                "citation": _CITATION_SCHEMA,
            },
            "required": ["text", "citation"],
            "additionalProperties": False,
        },
        "take": _cited_list_schema(
            "The 30-Second Take: exactly 3 headline bullets an advisor can "
            "open a call with, one line each.",
            min_items=3,
            max_items=3,
        ),
        "risks": _cited_list_schema(
            "Key Risks: exactly 3 material risks, ranked most-material "
            "first, one line each, each cited.",
            min_items=3,
            max_items=3,
        ),
        "whats_changed": _cited_list_schema(
            "What's Changed: up to 3 material developments since the annual "
            "report, drawn only from the supplied 8-Ks, one line each, each "
            "cited. Empty list if no 8-Ks were supplied.",
            min_items=0,
            max_items=3,
        ),
    },
    "required": ["business_line", "take", "risks", "whats_changed"],
    "additionalProperties": False,
}


# --- prompt construction ------------------------------------------------------


def _format_financial_value(value: float, unit: str) -> str:
    """Render one financial value for the prompt, precision chosen by unit.

    Per-share units (e.g. "USD/shares", i.e. EPS) always get 2 decimal
    places, full stop - this is the fix for the C3 bug where ``{:,.0f}``
    rounded EPS 2.94 down to "3" before the model ever saw it, making a
    materially wrong figure unrecoverable downstream (nothing after this
    point has the true value to catch the error against). Readability
    formatting (e.g. "$130.5B") is a nice-to-have for large USD magnitudes
    and must never be applied to a per-share figure.
    """
    if unit.endswith("/shares"):
        return f"{value:,.2f} {unit}"
    if unit == "USD":
        abs_value = abs(value)
        if abs_value >= 1_000_000_000:
            return f"${value / 1_000_000_000:,.1f}B"
        if abs_value >= 1_000_000:
            return f"${value / 1_000_000:,.1f}M"
        return f"{value:,.0f} {unit}"
    return f"{value:,.0f} {unit}"


def _financials_block(financials: dict[str, FinancialSeries]) -> str:
    if not financials:
        return "FINANCIALS: none available."
    lines = ["FINANCIALS (retrieved from XBRL, cite by accession if referenced):"]
    for concept, series in financials.items():
        for point in series.points:
            lines.append(
                f"- {concept} FY{point.fiscal_year}: "
                f"{_format_financial_value(point.value, series.unit)} "
                f"(source: {point.citation.form} filed {point.citation.filed_date}, "
                f"accession {point.citation.accession})"
            )
    return "\n".join(lines)


def _filings_block(ctx: SynthesisContext) -> str:
    parts: list[str] = []
    if ctx.tenk_text and ctx.tenk_citation:
        c = ctx.tenk_citation
        parts.append(
            f'<filing form="{c.form}" filed="{c.filed_date}" accession="{c.accession}" '
            f'url="{c.url}">\n{ctx.tenk_text}\n</filing>'
        )
    for accession, text in ctx.eight_k_texts.items():
        c = ctx.eight_k_citations.get(accession)
        if c is None:
            continue
        parts.append(
            f'<filing form="{c.form}" item="{c.item}" filed="{c.filed_date}" '
            f'accession="{c.accession}" url="{c.url}">\n{text}\n</filing>'
        )
    # 8-Ks with no fetched body (tier 3) still get listed as metadata, so the
    # model knows they exist and can mention them in "whats_changed" without
    # fabricating detail about content it was never given.
    metadata_only = [
        c for accn, c in ctx.eight_k_citations.items() if accn not in ctx.eight_k_texts
    ]
    if metadata_only:
        parts.append("METADATA-ONLY 8-Ks (no body text supplied, do not invent detail):")
        for c in metadata_only:
            parts.append(f"- {c.form} item {c.item}, filed {c.filed_date}, {c.url}")
    return "\n\n".join(parts)


def build_user_message(ctx: SynthesisContext) -> str:
    """Build the single user-turn prompt for the synthesis call."""
    quote_line = "QUOTE: unavailable"
    if ctx.quote and ctx.quote.available:
        quote_line = (
            f"QUOTE: {ctx.company.ticker} ${ctx.quote.price} "
            f"({ctx.quote.change_percent:+.2f}%)"
            if ctx.quote.price is not None and ctx.quote.change_percent is not None
            else f"QUOTE: {ctx.company.ticker} (partial data)"
        )
    return (
        f"COMPANY: {ctx.company.name} ({ctx.company.ticker}), CIK {ctx.company.cik}\n"
        f"{quote_line}\n\n"
        f"{_financials_block(ctx.financials)}\n\n"
        f"{_filings_block(ctx)}\n\n"
        "Produce the brief now."
    )


# --- citation validation -------------------------------------------------------


def _citation_allow_set(ctx: SynthesisContext) -> dict[str, Citation]:
    """The {accession: Citation} allow-set a model-emitted citation must
    resolve against - built from the filings actually supplied in ``ctx``
    (the 10-K citation plus every 8-K citation, tier 3/metadata-only
    included), never from anything the model says.
    """
    allow_set: dict[str, Citation] = {}
    if ctx.tenk_citation is not None:
        allow_set[ctx.tenk_citation.accession] = ctx.tenk_citation
    for accession, citation in ctx.eight_k_citations.items():
        allow_set[accession] = citation
    return allow_set


def _resolve_citation(
    raw_citation: dict[str, Any], allow_set: dict[str, Citation]
) -> Citation | None:
    """Look up a model-emitted citation by accession in ``allow_set``.

    On a match, the canonical ``Citation`` from the synthesis context is
    returned - not the model's copy. The model's ``form``/``item``/
    ``filed_date``/``url`` are never trusted, even on a match: only the
    accession is used, as a lookup key. On no match (a hallucinated-but-
    plausible accession, or a missing/empty one), returns None - the caller
    is responsible for dropping the bullet rather than rendering it as if
    it were sourced.
    """
    accession = raw_citation.get("accession", "") if raw_citation else ""
    return allow_set.get(accession)


# --- client + call ------------------------------------------------------------


def _client():
    """Lazily construct the Anthropic client. Never called at import time."""
    import anthropic

    return anthropic.Anthropic()


def _parse_brief(ticker: str, payload: dict[str, Any], allow_set: dict[str, Citation]) -> Brief:
    dropped: list[str] = []

    def resolve(raw_citation: dict[str, Any]) -> Citation | None:
        citation = _resolve_citation(raw_citation, allow_set)
        if citation is None:
            dropped.append((raw_citation or {}).get("accession") or "<missing accession>")
        return citation

    def cited_section(key: str, heading: str) -> BriefSection:
        content: list[str] = []
        citations: list[Citation] = []
        for item in payload.get(key, []):
            citation = resolve(item["citation"])
            if citation is None:
                # No valid citation -> drop the bullet entirely. It must
                # never render as though it were sourced (nor, for that
                # matter, render un-sourced prose dressed up as a brief
                # bullet - dropping it outright is the only option that
                # can't be mistaken for a citation that just happens to be
                # missing).
                continue
            content.append(item["text"])
            citations.append(citation)
        return BriefSection(heading=heading, content=content, citations=citations)

    business_line_payload = payload.get("business_line") or {}
    business_line_citation = None
    if business_line_payload:
        business_line_citation = resolve(business_line_payload.get("citation", {}))
    business_line = BriefSection(
        heading="",
        content=(
            [business_line_payload["text"]]
            if business_line_citation is not None and business_line_payload.get("text")
            else []
        ),
        citations=[business_line_citation] if business_line_citation is not None else [],
    )

    return Brief(
        ticker=ticker,
        business_line=business_line,
        take=cited_section("take", "The 30-Second Take"),
        risks=cited_section("risks", "Key Risks"),
        whats_changed=cited_section("whats_changed", "What's Changed"),
        dropped_citations=dropped,
    )


def generate_brief(ctx: SynthesisContext, client: Any | None = None) -> Brief:
    """Call Claude to synthesize a cited Brief from ``ctx``.

    Args:
        ctx: The assembled filings/financials/quote context (see
            SynthesisContext).
        client: An ``anthropic.Anthropic``-compatible client. If omitted,
            one is constructed lazily via ``_client()`` - pass a fake/mock
            here in tests so no network call or API key is ever needed to
            exercise this function.

    Returns:
        A populated Brief. Every citation in it has been cross-checked
        against ``ctx`` and rewritten to the canonical record - see
        ``_resolve_citation``. Any bullet whose citation didn't validate is
        dropped, and its accession recorded in ``Brief.dropped_citations``.

    Raises:
        Whatever the underlying client raises (e.g. anthropic.APIError) -
        this function does not swallow synthesis failures; pipeline.py
        decides how to degrade if this call fails.
    """
    active_client = client if client is not None else _client()

    response = active_client.messages.create(
        model=SYNTHESIS_MODEL,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": SYNTHESIS_EFFORT,
            "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(ctx)}],
    )

    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("Synthesis request was refused by the model.")

    text_block = next(block.text for block in response.content if block.type == "text")
    payload = json.loads(text_block)
    return _parse_brief(ctx.company.ticker, payload, _citation_allow_set(ctx))
