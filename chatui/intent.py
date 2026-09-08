"""Turn a line of advisor chat into an intent.

"NVDA" -> brief. "what do you think about Nvidia?" -> brief.
"what are the risks?" -> follow-up about whatever is already on screen.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from api import guardrails
from globalinsight import edgar

# Uppercase words that are also real tickers. Matching these would turn ordinary
# sentences into ticker lookups, so a bare occurrence never counts.
AMBIGUOUS = {
    "A", "ALL", "AN", "AND", "ANY", "ARE", "AS", "AT", "BE", "BY", "CAN", "DD",
    "DO", "E", "EPS", "ETF", "FOR", "GO", "HAS", "HE", "IF", "IN", "IPO", "IT",
    "K", "M", "ME", "MY", "NEW", "NO", "NOT", "NOW", "OF", "ON", "ONE", "OR",
    "OUT", "PE", "R", "SEC", "SO", "T", "THE", "TO", "TV", "UP", "US", "USA",
    "V", "WE", "Y", "YOU",
}

# Plenty of real companies are named after ordinary words ("Hello Group" trades
# as MOMO). A one-word company name only counts when it is capitalised in the
# message and is not one of these.
COMMON_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "anything", "are",
    "as", "ask", "at", "back", "bad", "be", "been", "before", "best", "better",
    "big", "both", "but", "buy", "by", "call", "can", "check", "client", "come",
    "compare", "could", "current", "do", "does", "doing", "down", "each",
    "explain", "few", "find", "first", "for", "from", "get", "give", "go",
    "good", "great", "happening", "has", "have", "hello", "help", "hey", "hi",
    "high", "his", "how", "if", "in", "info", "into", "is", "it", "its", "just",
    "key", "know", "last", "latest", "like", "look", "low", "main", "make",
    "many", "may", "me", "might", "money", "more", "morning", "most", "much",
    "my", "need", "new", "next", "no", "not", "now", "of", "off", "ok", "okay",
    "on", "one", "only", "open", "or", "other", "our", "out", "over", "please",
    "point", "quick", "read", "really", "right", "risk", "risks", "same", "say",
    "see", "sell", "share", "show", "since", "so", "some", "sure", "take",
    "tell", "than", "thanks", "that", "the", "their", "them", "then", "there",
    "these", "they", "think", "this", "those", "to", "today", "top", "two",
    "up", "us", "use", "very", "want", "was", "watch", "we", "well", "were",
    "what", "when", "where", "which", "while", "who", "why", "will", "with",
    "would", "year", "years", "yes", "you", "your",
}

_TOKEN = re.compile(r"\$?\b[A-Z]{1,5}(?:[.-][A-Z])?\b")
_WORD = re.compile(r"[a-z0-9]+")
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|"
    r"group|the|sa|nv|ag|lp|llc|trust|adr)\b\.?",
    re.I,
)


@dataclass
class Intent:
    kind: str  # "brief" | "followup" | "help"
    ticker: str = ""
    question: str = ""
    refusal: guardrails.Refusal | None = None
    """Set when the message asks for investment advice.

    It rides alongside ``kind`` rather than replacing it, because the two
    answer different questions. "Is AMD a buy?" is a recommendation request
    *and* a legitimate information need about AMD: the honest response is to
    decline the recommendation and serve the factual brief underneath it, not
    to pretend we don't know which company was meant. A follow-up
    ("should I hold it?") carries the same flag and is declined outright by
    qa.answer_question, which never reaches the model.
    """


@lru_cache(maxsize=1)
def _name_index() -> tuple[tuple[str, str], ...]:
    """[(normalised company name, ticker)], longest first.

    Uses edgar._raw_ticker_rows() because the backend's public
    load_ticker_map() drops the company titles. Worth promoting to a public
    helper there.
    """
    entries = []
    for row in edgar._raw_ticker_rows().values():
        name = _SUFFIXES.sub(" ", row["title"].lower())
        name = re.sub(r"[^a-z0-9 ]+", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) >= 4:
            entries.append((name, row["ticker"].upper()))
    entries.sort(key=lambda pair: -len(pair[0]))
    return tuple(entries)


def find_ticker(message: str) -> str | None:
    known = edgar.load_ticker_map()

    # 1. Explicit $TICKER always wins.
    for match in re.finditer(r"\$([A-Za-z.\-]{1,6})\b", message):
        symbol = match.group(1).upper().replace(".", "-")
        if symbol in known:
            return symbol

    # 2. A message that is just a symbol, in any case.
    bare = message.strip().strip("?.!,").upper().replace(".", "-")
    if bare in known:
        return bare

    # 3. An uppercase token inside a sentence, ignoring English words.
    for match in _TOKEN.finditer(message):
        symbol = match.group(0).lstrip("$").upper().replace(".", "-")
        if symbol not in AMBIGUOUS and symbol in known:
            return symbol

    # 4. A company name. Longest name first, so "Bank of America" beats "Bank".
    words = _WORD.findall(message.lower())
    if len(words) <= 12:
        # Space-padded containment gives word-boundary semantics without
        # compiling 10,000 regexes per message.
        haystack = f" {' '.join(words)} "
        capitalised = {w.lower() for w in re.findall(r"\b[A-Z][a-z]+\b", message)}
        for name, ticker in _name_index():
            if f" {name} " not in haystack:
                continue
            if " " not in name and (name in COMMON_WORDS or name not in capitalised):
                continue
            return ticker
    return None


def classify(message: str, active: str | None) -> Intent:
    text = message.strip()
    if not text:
        return Intent("help")
    try:
        ticker = find_ticker(text)
    except Exception:
        ticker = None
    refusal = guardrails.check_question(text)

    # A ticker already on screen means "tell me more", not "start over" -
    # otherwise every follow-up naming the company regenerates the brief.
    if ticker and ticker != active:
        return Intent("brief", ticker=ticker, refusal=refusal)
    if active:
        return Intent("followup", ticker=active, question=text, refusal=refusal)
    if ticker:
        return Intent("brief", ticker=ticker, refusal=refusal)
    return Intent("help", refusal=refusal)
