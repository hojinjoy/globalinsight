"""HTML -> plain text, and a cheap token estimator.

Deliberately dumb: strip script/style, strip all tags, unescape entities,
normalize whitespace. No structure-aware parsing, no attempt to recover
headings or sections - see the module docstring in filings.py for why
regex/heading-based section extraction is explicitly out of scope here.
"""

import html
import re

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(raw_html: str) -> str:
    """Convert an HTML filing document to cleaned plain text.

    Order matters: strip script/style blocks *before* stripping tags (else
    their content leaks into the text as prose), unescape entities *after*
    stripping tags (an escaped "&lt;div&gt;" must survive as literal text,
    not be mistaken for a real tag), and only then collapse whitespace.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _INLINE_WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def count_tokens_approx(text: str) -> int:
    """Cheap token estimate: ~4 characters per token, no tokenizer dependency."""
    return len(text) // 4
