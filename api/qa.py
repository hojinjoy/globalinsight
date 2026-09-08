"""Follow-up question answering over an already-built SynthesisContext.

Canonical home for this logic. It belongs in
``globalinsight.synthesize.answer_question()`` - it builds a prompt, calls
Claude and verifies citations, none of which is a UI concern - but that package
is owned by another team, so it lives here as a documented temporary home
(UI-SPEC section 3, backend request 3). It must stay a pure function with no
HTTP or rendering concerns so that upstreaming it is a file move.

``chatui/qa.py`` re-exports this module so the Streamlit fallback keeps
working.

Two things it does that the brief call does not need:

- The filing text sits behind a 1-hour cache breakpoint, so the second and
  subsequent questions in a conversation re-read the 10-K at cache-read rates
  (~$0.10/turn instead of a full re-read).
- Answer citations carry a verbatim ``quote``, which is checked back against
  the filing text. A citation naming a filing that was not supplied is dropped
  outright.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from api import guardrails
from globalinsight.config import SYNTHESIS_EFFORT, SYNTHESIS_MODEL
from globalinsight.models import Citation
from globalinsight.synthesize import SynthesisContext

MAX_TOKENS = 8_000

# Opus 5 list price, $/1M tokens.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 5.0, 25.0, 0.50, 6.25

INSTRUCTIONS = """You answer follow-up questions from a wealth-management \
advisor who is on a live client call, using only the SEC filings supplied below.

Rules, in order of importance:

1. Answer only from the filings below. If they do not answer the question, say
   so in `unsupported` rather than reaching for general knowledge.
2. Every substantive factual assertion goes in `claims` with a citation whose
   `url` and `accession` are copied verbatim from the SOURCE MANIFEST. Never
   construct or guess a URL.
3. The `quote` field must be an exact, contiguous excerpt from the filing text
   as it appears below - not a paraphrase. It is checked against the source.
4. Do not state financial figures in narrative text; those are shown from XBRL
   elsewhere on the page. Describing a trend in words is fine.
5. No recommendations, no price targets, no buy/sell/hold calls. Questions
   asking for one are refused before they reach you (see api/guardrails.py);
   if one arrives anyway, phrased so the filter missed it, decline it here in
   `unsupported` and answer only the factual part.
6. Be brief. The advisor is reading this aloud while a client waits."""

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Direct answer, 1-3 short paragraphs.",
        },
        "claims": {
            "type": "array",
            "description": "Each factual assertion in the answer, with its source.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "form": {"type": "string"},
                    "item": {"type": "string"},
                    "filed_date": {"type": "string"},
                    "accession": {"type": "string"},
                    "url": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": "Verbatim excerpt, up to 240 characters.",
                    },
                },
                "required": [
                    "text", "form", "item", "filed_date", "accession", "url", "quote",
                ],
                "additionalProperties": False,
            },
        },
        "unsupported": {
            "type": "string",
            "description": "Empty unless the filings do not answer the question.",
        },
    },
    "required": ["answer", "claims", "unsupported"],
    "additionalProperties": False,
}


@dataclass
class Claim:
    text: str
    citation: Citation
    quote: str
    status: str = "unknown"  # verified | unverified | rejected


@dataclass
class Answer:
    text: str
    claims: list[Claim] = field(default_factory=list)
    unsupported: str = ""
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    refusal_reason: str = ""
    """Set when the question was declined on policy grounds - see
    api/guardrails.py. Empty on every answered question. A refused Answer
    carries the refusal prose in ``text``, no claims, and zero usage,
    because no model call was made."""

    @property
    def cost(self) -> float:
        return (
            self.input_tokens * PRICE_IN
            + self.output_tokens * PRICE_OUT
            + self.cache_read * PRICE_CACHE_READ
            + self.cache_write * PRICE_CACHE_WRITE
        ) / 1_000_000


class AnswerTextStream:
    """Pulls the growing ``answer`` string out of a structured-output stream.

    The model is streaming JSON, not prose, because the call uses
    ``output_config.format``. Token deltas are therefore JSON fragments and
    cannot be shown to a user directly. This scans the accumulated buffer for
    the ``"answer"`` value and yields only newly-decoded characters of it, so
    the UI can render real prose as it forms rather than faking a progress bar.
    """

    _KEY = re.compile(r'"answer"\s*:\s*"')

    def __init__(self) -> None:
        self._buffer = ""
        self._value_at: int | None = None
        self._emitted = 0

    def feed(self, chunk: str) -> str:
        """Append a raw stream chunk; return newly decoded answer text."""
        self._buffer += chunk
        if self._value_at is None:
            match = self._KEY.search(self._buffer)
            if match is None:
                return ""
            self._value_at = match.end()

        characters: list[str] = []
        escaped = False
        for character in self._buffer[self._value_at:]:
            if escaped:
                characters.append(character)
                escaped = False
            elif character == "\\":
                characters.append(character)
                escaped = True
            elif character == '"':
                break
            else:
                characters.append(character)
        if escaped:  # trailing backslash: wait for the character it escapes
            characters.pop()
        try:
            decoded = json.loads('"' + "".join(characters) + '"')
        except ValueError:
            return ""
        new_text = decoded[self._emitted:]
        self._emitted = len(decoded)
        return new_text


_NORM = re.compile(r"[^a-z0-9]+")


def _normalise(text: str) -> str:
    return _NORM.sub("", text.lower())


def _documents(ctx: SynthesisContext) -> tuple[str, dict[str, str]]:
    """The filing text block, plus url -> normalised text for quote checking.

    Only filings whose body was fetched appear in the returned map. Tier-3 8-Ks
    are indexed in the manifest but never fetched, so a claim citing one is
    legitimate - it just cannot have its quote checked. Keep the two ideas
    separate: see ``_allowed_urls``.
    """
    chunks, by_url = [], {}
    if ctx.tenk_text and ctx.tenk_citation:
        citation = ctx.tenk_citation
        chunks.append(
            f'<filing form="{citation.form}" filed="{citation.filed_date}" '
            f'url="{citation.url}">\n{ctx.tenk_text}\n</filing>'
        )
        by_url[citation.url] = _normalise(ctx.tenk_text)
    for accession, text in ctx.eight_k_texts.items():
        citation = ctx.eight_k_citations.get(accession)
        if citation is None:
            continue
        chunks.append(
            f'<filing form="8-K" items="{citation.item}" '
            f'filed="{citation.filed_date}" url="{citation.url}">\n{text}\n</filing>'
        )
        by_url[citation.url] = _normalise(text)
    return "\n\n".join(chunks), by_url


def _allowed_urls(ctx: SynthesisContext) -> set[str]:
    """Every filing the model was told about - bodies fetched or not."""
    urls = {citation.url for citation in ctx.eight_k_citations.values()}
    if ctx.tenk_citation:
        urls.add(ctx.tenk_citation.url)
    return urls


def _manifest(ctx: SynthesisContext) -> str:
    lines = ["SOURCE MANIFEST - cite only these, copied exactly:"]
    citations = list(ctx.eight_k_citations.values())
    if ctx.tenk_citation:
        citations.insert(0, ctx.tenk_citation)
    for citation in citations:
        item = f" items {citation.item}" if citation.item else ""
        lines.append(
            f"- {citation.form}{item} | filed {citation.filed_date} | "
            f"accession {citation.accession} | {citation.url}"
        )
    # Tier-3 8-Ks are indexed but their bodies were never fetched, so the model
    # must not claim to have read them.
    unread = [
        citation.accession
        for accession, citation in ctx.eight_k_citations.items()
        if accession not in ctx.eight_k_texts
    ]
    if unread:
        lines.append(
            "Metadata only (body not supplied, do not quote): " + ", ".join(unread)
        )
    return "\n".join(lines)


def _system(ctx: SynthesisContext) -> tuple[list[dict], dict[str, str]]:
    documents, by_url = _documents(ctx)
    header = (
        f"{INSTRUCTIONS}\n\nCOMPANY: {ctx.company.name} ({ctx.company.ticker}), "
        f"CIK {ctx.company.cik}\n\n{_manifest(ctx)}"
    )
    return (
        [
            {"type": "text", "text": header},
            {
                "type": "text",
                "text": f"FILING TEXT\n\n{documents}",
                # Breakpoint after the filings: follow-ups re-read at cache rates.
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        by_url,
    )


def refusal_answer(refusal: guardrails.Refusal) -> Answer:
    """A completed Answer that declines ``refusal``'s question.

    Same type as a real answer so every caller - SSE route, Streamlit, tests -
    renders it through the existing path with no special-casing. Usage is zero
    across the board because nothing was sent to the model.
    """
    return Answer(
        text=guardrails.refusal_text(refusal),
        claims=[],
        unsupported="",
        seconds=0.0,
        refusal_reason=refusal.reason,
    )


def answer_question(
    ctx: SynthesisContext,
    history: list[dict],
    question: str,
    client: Any | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> Answer:
    """Answer ``question`` from ``ctx``'s filings. ``history`` is prior turns.

    A question asking for investment advice is refused here, before any call
    is made - see api/guardrails.py. The check lives in this function rather
    than in the HTTP route so that every entry point inherits it: the SSE
    route, the Streamlit fallback via ``chatui.qa``, and anything that
    upstreams this into the backend package later. A route-level check would
    protect one caller and silently miss the rest.

    ``on_delta`` receives newly decoded answer prose as it streams; see
    AnswerTextStream for why raw token deltas cannot be used directly.
    """
    refusal = guardrails.check_question(question)
    if refusal is not None:
        answer = refusal_answer(refusal)
        # The refusal is prose the caller is about to render; push it through
        # the same delta channel so a streaming UI shows it the way it shows
        # anything else, rather than sitting on an empty stream.
        if on_delta:
            on_delta(answer.text)
        return answer

    system, by_url = _system(ctx)
    active = client if client is not None else anthropic.Anthropic()

    started = time.monotonic()
    with active.messages.stream(
        model=SYNTHESIS_MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": SYNTHESIS_EFFORT,
            "format": {"type": "json_schema", "schema": _ANSWER_SCHEMA},
        },
        system=system,
        messages=history + [{"role": "user", "content": question}],
    ) as stream:
        prose = AnswerTextStream()
        for chunk in stream.text_stream:
            if on_delta:
                new_text = prose.feed(chunk)
                if new_text:
                    on_delta(new_text)
            if on_progress:
                on_progress(time.monotonic() - started)
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    payload = json.loads(
        next(block.text for block in message.content if block.type == "text")
    )

    answer = Answer(
        text=payload["answer"],
        unsupported=payload.get("unsupported", ""),
        seconds=time.monotonic() - started,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read=message.usage.cache_read_input_tokens or 0,
        cache_write=message.usage.cache_creation_input_tokens or 0,
    )
    allowed = _allowed_urls(ctx)
    for raw in payload.get("claims", []):
        if raw["url"] not in allowed:
            continue  # cites a filing that was never supplied - drop it
        if raw["url"] not in by_url:
            # Indexed in the manifest but body never fetched (tier-3 8-K), so
            # the claim stands on the filing's existence and item code alone.
            status = "metadata"
        else:
            needle = _normalise(raw["quote"])
            status = (
                "verified"
                if len(needle) >= 12 and needle in by_url[raw["url"]]
                else "unverified"
            )
        answer.claims.append(
            Claim(
                text=raw["text"],
                citation=Citation(
                    form=raw["form"],
                    item=raw["item"],
                    filed_date=raw["filed_date"],
                    accession=raw["accession"],
                    url=raw["url"],
                ),
                quote=raw["quote"],
                status=status,
            )
        )
    return answer
