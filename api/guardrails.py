"""Advice-seeking question detection - the compliance gate on the ask side.

GlobalInsight produces briefing material for a regulated wealth-management
advisor. It reports what a filing says; it never says what to do about it.
Both prompts already carry that rule in prose (``qa.INSTRUCTIONS`` rule 5,
"No recommendations, no price targets, no buy/sell/hold calls", and the
matching line in ``synthesize.SYSTEM_PROMPT``) - but a prompt rule is only
ever a request. It is not enforced, it is not auditable after the fact, and
it costs a full high-effort Opus call to discover whether the model honoured
it this time.

This module makes the same rule deterministic, free, and checkable:

- The check is lexical and closed-form, not a model call. An advice question
  has to be refused *identically every time* - a classifier that declines 98%
  of the time is not a compliance control, and one that bills $0.10 per
  question to run is not one either. It also means the refusal lands before
  any document fetch or paid call, so a blocked question costs nothing.
- It errs toward answering. Every pattern below matches an advice-seeking
  *construction* ("should I buy", "is it a buy", "price target"), never a bare
  keyword - because "what do they sell?", "did they announce a buyback?" and
  "what were selling, general and administrative expenses?" are ordinary
  filing questions that a keyword filter would swallow whole. The
  false-positive corpus in ``tests/test_guardrails.py`` is part of this
  module's contract, not an afterthought: adding a pattern that breaks one of
  those is a regression.
- A refusal names the boundary once and immediately offers the factual
  question underneath it. The advisor is mid-call; they need a redirect, not
  a lecture.

This is the ask-side half. The tell-side half - scanning a generated answer
for recommendation language that slipped through - is deliberately not here;
see the note in ``qa.answer_question``.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Refusal:
    """Why a question was declined, and what to ask instead.

    ``reason`` is the stable machine-readable code (it goes into the SSE
    payload and is what you would count in a log); ``message`` is advisor-
    facing prose; ``suggestions`` are answerable rewrites of the same
    underlying information need.
    """

    reason: str
    message: str
    suggestions: tuple[str, ...] = ()


# A trailing (?![-\w]) after a verb keeps "sell-off", "buy-side" and
# "buyback" out of the advice patterns - the hyphen is a word boundary, so
# \bsell\b alone matches inside "sell-off" and would refuse "is there a
# sell-off risk?", which is a legitimate question about the filing.
_ACTION = r"(?:buy|sell|hold|short)(?![-\w])"

# Price movement, as the object of a forecast. Note \d+ rather than \d in the
# level patterns: a trailing \b after a single \d falls *inside* "$200" (the
# boundary between "2" and "0" does not exist) and the pattern silently never
# fires.
_MOVE = (
    r"(?:go(?:ing)?\s+(?:up|down|higher|lower)|rise|fall|drop|crash|rally|"
    r"double|tank|recover|keep\s+(?:going|climbing|falling|rising)|"
    r"outperform|beat\s+the\s+market|hit\s+\$?\d+|reach\s+\$?\d+)"
)

# The subjects a price forecast is asked about. Anchoring on these is what
# keeps "will revenue fall next year?" - a question about the business, which
# the prompt's "the filings do not say" path already handles - out of the
# filter.
_PRICE_SUBJECT = r"(?:it|this|the\s+stock|the\s+shares?|shares?|the\s+(?:share\s+)?price)"

_A_RATING = (
    r"a\s+(?:strong\s+|good\s+|great\s+|bad\s+|safe\s+|smart\s+|solid\s+)?"
    r"(?:buy|sell|hold|short)(?![-\w])"
    r"|an?\s+(?:good|bad|safe|smart|solid|sound|poor|terrible)\s+"
    r"(?:investment|stock|entry|bet|play|name)\b"
)

# Order is the order they are tried; the first match wins and names the
# reason. The four groups are disjoint in practice, so the ordering is for
# readability rather than precedence.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "recommendation",
        (
            # "should I buy", "should my clients hold", "should we trim this"
            # The -ing forms matter: "should we be adding here?" is the same
            # question as "should we add?". `sell(?:ing)?` carries a lookahead
            # because "selling, general and administrative" is a real line
            # item someone may reasonably ask to be pointed at.
            r"\bshould\s+(?:i|we|you|he|she|they|my\s+clients?|our\s+clients?|"
            r"the\s+client|a\s+client|anyone|someone|he\s+or\s+she)\b"
            r"[^.?!]{0,60}?\b(?:buy(?:ing)?|sell(?:ing)?(?!\s*,?\s*general)|"
            r"hold(?:ing)?|own(?:ing)?|invest(?:ing)?|add(?:ing)?|trim(?:ming)?|"
            r"exit(?:ing)?|short(?:ing)?|avoid(?:ing)?|dump(?:ing)?|get\s+in|"
            r"get\s+out|take\s+profits?|average\s+down|stay\s+in|stay\s+long)"
            r"(?![-\w])",
            r"\bwould\s+you\s+(?:buy|sell|hold|own|short|invest|recommend|avoid|"
            r"get\s+in|get\s+out|add|trim)(?![-\w])",
            r"\bwhat\s+would\s+you\s+do\b",
            r"\bthoughts\s+on\s+(?:buying|selling|shorting|owning|adding|"
            r"trimming|the\s+entry)\b",
            r"\b(?:my\s+client|the\s+client|i|we)\s+(?:wants?|want|"
            r"is\s+thinking\s+about|am\s+thinking\s+about|are\s+thinking\s+about)"
            r"\s+(?:to\s+)?(?:buy|sell|short|add|exit|get\s+in)(?:ing)?(?![-\w])",
            r"\btoo\s+late\s+to\s+(?:buy|sell|get\s+in|get\s+out|add)(?![-\w])",
            r"\btake\s+profits?\b",
            r"\byour\s+call\s+on\b",
            r"\bworth\s+a\s+look\b",
            r"\ba\s+value\s+trap\b",
            # "is AMD a buy", "is it a good investment", "AMD a buy?"
            r"\b(?:is|are|was|were|would|isn'?t)\b[^.?!]{0,30}?\b(?:" + _A_RATING + r")",
            r"^[^.?!]{0,24}?\b(?:" + _A_RATING + r")\s*\??\s*$",
            r"\bbuy\s*,?\s*(?:sell\s*,?\s*)?or\s+(?:sell|hold)(?![-\w])",
            r"\b(?:do|would|can|could|should)\s+you\s+recommend\b",
            r"\bwhat\s+(?:do|would)\s+you\s+recommend\b",
            # Narrow to "your"/"any": a proxy statement really can contain
            # "the board's recommendation", and asking about it is fair game.
            r"\b(?:your|any)\s+recommendations?\b",
            r"\brecommend\s+(?:buying|selling|holding|shorting|it|this|"
            r"a\s+position)\b",
            r"\binvestment\s+advice\b",
            r"\b(?:are\s+you|you)\s+(?:bullish|bearish)\b",
            r"\brate\s+(?:it|this|the\s+stock)\b",
            r"\b(?:overweight|underweight)\s+(?:it|this|the\s+(?:stock|name|shares))\b",
            r"\b(?:is|as|call\s+it)\s+an?\s+(?:overweight|underweight)\b",
            r"\bworth\s+(?:buying|owning|shorting|investing\s+in|"
            r"a\s+position|the\s+(?:investment|risk))\b",
            r"\b(?:good|right|best|bad)\s+time\s+to\s+(?:buy|sell|add|trim|"
            r"get\s+in|get\s+out)(?![-\w])",
            r"\b(?:entry|exit)\s+point\b",
            r"\bbuy\s+the\s+dip\b",
            r"\bpick\s+between\b|\bwhich\s+(?:one\s+)?(?:should|would)\s+"
            r"(?:i|we|you)\s+(?:buy|own|pick|choose)(?![-\w])",
        ),
    ),
    (
        "valuation_judgment",
        (
            r"\b(?:price|pt)\s+target\b",
            r"\btarget\s+price\b",
            r"\byour\s+(?:pt|target)\b",
            r"\bvalue\s+(?:it|this|the\s+(?:stock|company|shares?))\s+for\s+"
            r"(?:me|us)\b",
            r"\b(?:over|under)valued\b",
            r"\bis\s+(?:it|this|the\s+stock|the\s+name|the\s+shares?)\s+"
            r"(?:cheap|expensive|a\s+bargain|rich|pricey)\b",
            r"\bwhat(?:'s|\s+is|\s+are)?\s+(?:it|the\s+stock|the\s+shares?|"
            r"the\s+company)\s+(?:really\s+)?worth\b",
            # Deliberately NOT bare "fair value": ASC 820 fair value
            # measurements are a real disclosure and a fair question about it.
            r"\bfair\s+value\s+(?:estimate|per\s+share|of\s+the\s+stock)\b",
            r"\bhow\s+much\s+(?:upside|downside)\b",
            r"\bis\s+the\s+valuation\s+(?:justified|reasonable|stretched|fair)\b",
            r"\bdcf\b|\bintrinsic\s+value\b",
        ),
    ),
    (
        "price_forecast",
        (
            # Verb first: "will the stock go up?"
            r"\b(?:will|can|could|is|are)\s+" + _PRICE_SUBJECT + r"\b[^.?!]{0,40}?\b"
            + _MOVE + r"\b",
            # Subject first: "do you think it will keep climbing?"
            r"\b" + _PRICE_SUBJECT + r"\s+(?:will|is\s+going\s+to|might|may|could|"
            r"would|should)\b[^.?!]{0,40}?\b" + _MOVE + r"\b",
            r"\bwhere\s+(?:will|do\s+you\s+see|is)\s+" + _PRICE_SUBJECT
            + r"\b[^.?!]{0,40}?\b(?:go|going|headed|head|end\s+up|next|"
            r"in\s+\d+|by\s+\d+|trade)\b",
            r"\bprice\s+(?:forecast|prediction|outlook|projection)\b",
            r"\bhow\s+(?:high|low|far)\s+(?:will|can|could|might)\s+"
            r"(?:it|this|the\s+stock|the\s+shares?|shares?|the\s+price)\b",
            r"\bpredict\s+(?:the\s+)?(?:price|stock)\b",
        ),
    ),
    (
        "allocation",
        (
            r"\bhow\s+much\s+(?:should|would|do|can)\s+(?:i|we|my\s+clients?|"
            r"a\s+client|the\s+client)\b",
            r"\bposition\s+siz(?:e|ing)\b",
            r"\bhow\s+much\s+(?:of\s+)?(?:my|our|the|a|their)\s+portfolio\b",
            r"\bwhat\s+(?:percentage|%|weight|weighting)\b[^.?!]{0,30}?"
            r"(?:portfolio|allocat|position)",
            r"\ballocate\s+(?:to|into|toward)\b",
            r"\bhow\s+many\s+shares\s+should\b",
            r"\b(?:size|scale)\s+(?:the|a|my|this)\s+position\b",
        ),
    ),
)

_COMPILED: tuple[tuple[str, tuple[re.Pattern, ...]], ...] = tuple(
    (reason, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for reason, patterns in _PATTERNS
)

_MESSAGES: dict[str, str] = {
    "recommendation": (
        "I can't tell you whether to buy, sell or hold - that's a "
        "recommendation, and this tool only reports what the filings say. "
        "I can lay out the facts behind the call."
    ),
    "valuation_judgment": (
        "I can't give a price target or a view on whether the stock is cheap "
        "or expensive - that's a valuation judgment, not something the "
        "filings state. I can show you the reported figures behind it."
    ),
    "price_forecast": (
        "I can't forecast where the price goes. I can tell you what the "
        "filings disclose about the drivers, and what has changed recently."
    ),
    "allocation": (
        "I can't advise on position sizing or allocation. I can give you the "
        "disclosed facts you'd size a position against."
    ),
}

_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "recommendation": (
        "What risks does management flag in the latest 10-K?",
        "What's changed since the annual report?",
        "How does the company actually make money?",
    ),
    "valuation_judgment": (
        "How have revenue and margins moved over the last three years?",
        "What does management say about pricing and competition?",
    ),
    "price_forecast": (
        "What forward-looking risks does the 10-K identify?",
        "What did the most recent earnings 8-K report?",
    ),
    "allocation": (
        "What customer or supplier concentration does the 10-K disclose?",
        "What are the three most material risks in the filing?",
    ),
}

_WHITESPACE = re.compile(r"\s+")


def check_question(question: str) -> Refusal | None:
    """Return a ``Refusal`` if ``question`` asks for advice, else None.

    Pure and side-effect free: no model call, no network, no state. Callers
    run it before spending anything - see ``qa.answer_question`` and the
    ``/api/ask`` route.
    """
    text = _WHITESPACE.sub(" ", (question or "").strip())
    if not text:
        return None
    for reason, patterns in _COMPILED:
        if any(pattern.search(text) for pattern in patterns):
            return Refusal(
                reason=reason,
                message=_MESSAGES[reason],
                suggestions=_SUGGESTIONS[reason],
            )
    return None


def refusal_text(refusal: Refusal) -> str:
    """The refusal rendered as the markdown an advisor reads."""
    if not refusal.suggestions:
        return refusal.message
    lines = [refusal.message, "", "Ask me instead:"]
    lines += [f"- {suggestion}" for suggestion in refusal.suggestions]
    return "\n".join(lines)


def serialize(refusal: Refusal | None) -> dict | None:
    """JSON-safe form for the HTTP layer."""
    if refusal is None:
        return None
    return {
        "reason": refusal.reason,
        "message": refusal.message,
        "suggestions": list(refusal.suggestions),
        "text": refusal_text(refusal),
    }
