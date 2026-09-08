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

Two more failure modes are guarded against here, both about the payload
getting too large for one call to survive:

- Per-8-K caps (config.TIER1_CAP_TOKENS / TIER2_CAP_TOKENS) bound each
  individual 8-K, but nothing previously bounded their *sum* together with
  the 10-K. ``_apply_token_budget`` enforces a global ceiling
  (``TOTAL_TOKEN_BUDGET``) on the assembled payload, dropping the oldest
  8-Ks first (down to a metadata-only mention, never deleted outright) when
  it's exceeded - see that function's docstring for why the 10-K and the
  most recent earnings 8-K are exempt.
- The 10-K is static and immutable, and used to be re-billed in full on
  every single call. ``_build_message_content`` puts it in its own leading
  content block with a ``cache_control`` breakpoint so repeat calls read it
  from cache instead - see that function's docstring for the ordering rule
  this depends on.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from . import clean
from .config import SYNTHESIS_EFFORT, SYNTHESIS_MAX_TOKENS, SYNTHESIS_MODEL, TIER1_ITEMS
from .models import Brief, BriefSection, Citation, Company, FinancialSeries, Quote

logger = logging.getLogger(__name__)

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


def _tenk_filing_block(ctx: SynthesisContext) -> str:
    """The 10-K's own ``<filing>`` block, split out from the rest of the
    filings text so it can be isolated in its own cache_control breakpoint
    - see ``_build_message_content``. Empty string if no 10-K was supplied.
    """
    if not (ctx.tenk_text and ctx.tenk_citation):
        return ""
    c = ctx.tenk_citation
    return (
        f'<filing form="{c.form}" filed="{c.filed_date}" accession="{c.accession}" '
        f'url="{c.url}">\n{ctx.tenk_text}\n</filing>'
    )


def _eight_k_filings_block(ctx: SynthesisContext) -> str:
    """Every 8-K's ``<filing>`` block (bodies fetched) plus a metadata-only
    listing for any 8-K that has a citation but no body in ``eight_k_texts``
    - either because it's tier 3 (body never fetched) or because
    ``_apply_token_budget`` dropped it to stay under the global budget.
    Either way it's mentioned, not fabricated: the model knows it exists
    but is told not to invent detail about content it was never given.
    """
    parts: list[str] = []
    for accession, text in ctx.eight_k_texts.items():
        c = ctx.eight_k_citations.get(accession)
        if c is None:
            continue
        parts.append(
            f'<filing form="{c.form}" item="{c.item}" filed="{c.filed_date}" '
            f'accession="{c.accession}" url="{c.url}">\n{text}\n</filing>'
        )
    metadata_only = [
        c for accn, c in ctx.eight_k_citations.items() if accn not in ctx.eight_k_texts
    ]
    if metadata_only:
        parts.append("METADATA-ONLY 8-Ks (no body text supplied, do not invent detail):")
        for c in metadata_only:
            parts.append(f"- {c.form} item {c.item}, filed {c.filed_date}, {c.url}")
    return "\n\n".join(parts)


def _filings_block(ctx: SynthesisContext) -> str:
    parts = [p for p in (_tenk_filing_block(ctx), _eight_k_filings_block(ctx)) if p]
    return "\n\n".join(parts)


def _quote_line(ctx: SynthesisContext) -> str:
    if ctx.quote and ctx.quote.available:
        if ctx.quote.price is not None and ctx.quote.change_percent is not None:
            return (
                f"QUOTE: {ctx.company.ticker} ${ctx.quote.price} "
                f"({ctx.quote.change_percent:+.2f}%)"
            )
        return f"QUOTE: {ctx.company.ticker} (partial data)"
    return "QUOTE: unavailable"


def build_user_message(ctx: SynthesisContext) -> str:
    """Build the single user-turn prompt for the synthesis call, as one
    plain string. Used for tests and anywhere the full prompt text is
    wanted as a unit; the real API call instead uses
    ``_build_message_content``, which splits this same material into
    cacheable content blocks.
    """
    return (
        f"COMPANY: {ctx.company.name} ({ctx.company.ticker}), CIK {ctx.company.cik}\n"
        f"{_quote_line(ctx)}\n\n"
        f"{_financials_block(ctx.financials)}\n\n"
        f"{_filings_block(ctx)}\n\n"
        "Produce the brief now."
    )


def _build_message_content(ctx: SynthesisContext) -> list[dict[str, Any]]:
    """The user-turn content for the real API call, split for prompt
    caching.

    The 10-K is static and immutable (SEC filings never change once
    accepted) yet was previously re-sent and re-billed in full on every
    call. Anthropic's caching is a prefix match, and the rule for where to
    put a breakpoint is: stable content first, volatile content after the
    last breakpoint. So this puts the company header + 10-K text in a
    leading block with an ``ephemeral`` cache_control breakpoint - stable
    across repeat calls for the same company - and everything that *can*
    change from one call to the next (the quote, financials, the 8-K set,
    the closing instruction) in a second, uncached block after it.
    """
    header = f"COMPANY: {ctx.company.name} ({ctx.company.ticker}), CIK {ctx.company.cik}"
    tenk_block = _tenk_filing_block(ctx)
    stable_text = f"{header}\n\n{tenk_block}" if tenk_block else header

    volatile_text = (
        f"{_quote_line(ctx)}\n\n"
        f"{_financials_block(ctx.financials)}\n\n"
        f"{_eight_k_filings_block(ctx)}\n\n"
        "Produce the brief now."
    )

    return [
        {"type": "text", "text": stable_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile_text},
    ]


# --- global token budget -------------------------------------------------------

# Well inside the 1M context window, but chosen to actually bind: JPM's 10-K
# alone measures ~352,601 tokens (approx, via clean.count_tokens_approx - the
# same 4-chars/token heuristic config.py's per-8-K tier caps already rely
# on), and with its tier-1/2 8-Ks the full assembled payload for one brief
# measures ~412,729 tokens - already over this threshold on its own. 400k
# still leaves >500k tokens of headroom in the window for the system prompt,
# the financials block, and the SYNTHESIS_MAX_TOKENS output allocation
# (thinking tokens at "high" effort count against that cap - see
# generate_brief), while capping the single biggest cost/failure driver in
# this call well before a filer with a large 10-K *and* several tier-1 8-Ks
# - a realistic combination, JPM already needs the trim - can push the sum
# over the window and return a hard 400 mid-demo.
TOTAL_TOKEN_BUDGET = 400_000


def _apply_token_budget(
    ctx: SynthesisContext, budget: int = TOTAL_TOKEN_BUDGET
) -> tuple[SynthesisContext, list[str]]:
    """Enforce a global token budget across the assembled filings payload
    (10-K + every fetched 8-K body).

    Per-8-K caps (config.TIER1_CAP_TOKENS / TIER2_CAP_TOKENS) bound each
    individual 8-K, but nothing previously bounded their *sum*. When the
    total exceeds ``budget``, the OLDEST 8-Ks (by filed_date) are dropped
    first - down to a metadata-only mention via ``_eight_k_filings_block``,
    never deleted outright, so the model still knows they exist and when
    they were filed. The 10-K and the most recent tier-1 earnings 8-K (item
    "2.02", possibly combined with other item codes - matched the same way
    ``filings.determine_tier`` classifies tiers) are the highest-value
    content in the payload and are never dropped, even if that means the
    total stays over budget (a 10-K alone bigger than the budget is a
    separate, out-of-scope problem - this function's job is bounding the
    *sum*, not truncating the 10-K itself).

    Returns a new ``SynthesisContext`` with ``eight_k_texts`` filtered
    (``eight_k_citations`` is untouched, which is what makes the
    metadata-only degrade-gracefully behavior work) and the list of dropped
    accessions, oldest-filed first, so the caller can log what happened
    instead of dropping it silently.
    """
    entries: list[tuple[str, str, Citation]] = []
    for accession, text in ctx.eight_k_texts.items():
        citation = ctx.eight_k_citations.get(accession)
        if citation is None:
            continue  # already excluded from the prompt - see _eight_k_filings_block
        entries.append((accession, text, citation))

    tenk_tokens = clean.count_tokens_approx(ctx.tenk_text) if ctx.tenk_text else 0
    total = tenk_tokens + sum(clean.count_tokens_approx(text) for _, text, _ in entries)
    if total <= budget:
        return ctx, []

    earnings_entries = [
        e for e in entries if set(e[2].item.split(",")) & TIER1_ITEMS
    ]
    protected_accession = (
        max(earnings_entries, key=lambda e: e[2].filed_date)[0] if earnings_entries else None
    )

    droppable = sorted(
        (e for e in entries if e[0] != protected_accession),
        key=lambda e: e[2].filed_date,
    )

    kept_texts = dict(ctx.eight_k_texts)
    dropped: list[str] = []
    for accession, text, _ in droppable:
        if total <= budget:
            break
        del kept_texts[accession]
        total -= clean.count_tokens_approx(text)
        dropped.append(accession)

    if not dropped:
        return ctx, []
    return replace(ctx, eight_k_texts=kept_texts), dropped


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
        If the assembled payload was over ``TOTAL_TOKEN_BUDGET``, the
        oldest 8-Ks were dropped to fit - see ``_apply_token_budget`` - and
        that drop is logged (a warning on this module's logger), not
        silent.

    Raises:
        RuntimeError: the model refused the request (``stop_reason ==
            "refusal"``), the response was truncated before completion
            (``stop_reason == "max_tokens"`` - the JSON is necessarily
            incomplete, so this is raised instead of letting ``json.loads``
            fail on truncated input), or the response contained no text
            block at all (e.g. a thinking-only turn).
        Whatever else the underlying client raises (e.g. anthropic.APIError)
        - this function does not swallow synthesis failures; pipeline.py
        decides how to degrade if this call fails.
    """
    active_client = client if client is not None else _client()

    trimmed_ctx, dropped_for_budget = _apply_token_budget(ctx)
    if dropped_for_budget:
        logger.warning(
            "Synthesis payload for %s exceeded the %d-token budget; dropped "
            "%d oldest 8-K(s) to a metadata-only mention: %s",
            ctx.company.ticker,
            TOTAL_TOKEN_BUDGET,
            len(dropped_for_budget),
            ", ".join(dropped_for_budget),
        )

    # Long input (up to the full 1M-token context) and a large max_tokens
    # (thinking at "high" effort counts against it) should stream rather
    # than block on a single non-streaming response, per current Anthropic
    # guidance - it avoids HTTP timeouts. get_final_message() collapses the
    # stream back into the same Message shape a non-streaming call returns.
    #
    # `betas=["server-side-fallback-2026-07-01"]` + `fallbacks="default"`
    # (beta namespace, since the beta header is required) is Anthropic's
    # recommended pairing for claude-opus-5: on a policy refusal, the same
    # request is re-run server-side on Anthropic's recommended fallback
    # model instead of this call raising below. Without it, a refusal here
    # is unrecoverable mid-demo.
    with active_client.beta.messages.stream(
        model=SYNTHESIS_MODEL,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": SYNTHESIS_EFFORT,
            "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": _build_message_content(trimmed_ctx)}],
    ) as stream:
        response = stream.get_final_message()

    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("Synthesis request was refused by the model.")
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Synthesis response hit the max_tokens limit "
            f"({SYNTHESIS_MAX_TOKENS}) before completing - the JSON is "
            "truncated and cannot be parsed. Thinking tokens count against "
            "this limit, so a large payload at high effort can exhaust it "
            "with no visible output. Retry, raise SYNTHESIS_MAX_TOKENS, or "
            "lower SYNTHESIS_EFFORT."
        )

    text_block = next((block for block in response.content if block.type == "text"), None)
    if text_block is None:
        raise RuntimeError(
            "Synthesis response contained no text block to parse (e.g. a "
            "thinking-only turn) - cannot produce a brief from this response."
        )
    payload = json.loads(text_block.text)
    return _parse_brief(ctx.company.ticker, payload, _citation_allow_set(trimmed_ctx))
