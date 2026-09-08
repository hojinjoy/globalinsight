"""Record replay fixtures so the UI demos with zero API credits.

Two modes:

  (default)        Record real model output. Costs money; needs credits.
  --from-filings   Build a fixture with no API call at all. Bullets are
                   verbatim excerpts from the filing text, each carrying the
                   real citation for the document it came from.

The second mode exists because the account ran out of credits before any brief
could be recorded. It fabricates nothing - every sentence is quoted from the
filing and every accession and URL is real - but it is NOT model output, and
the fixture says so in `provenance_note`, which the UI renders as a banner
alongside the REPLAY badge. Re-record with the default mode once credits are
back and the placeholder is overwritten.

    uv run python -m scripts.record_fixtures --from-filings NVDA AAPL SPY BABA
    uv run python -m scripts.record_fixtures NVDA          # real, costs money
"""

import re
import sys
from datetime import datetime, timezone

from api import fixtures, serialize
from globalinsight import pipeline, synthesize

# Sentence-selection cues per section. Deliberately dull heuristics: the point
# is to surface real filing language, not to imitate the model's judgement.
CUES = {
    "business_line": [
        "designs, manufactures", "designs and", "we are a", "the company is a",
        "products and services", "operates", "our mission",
    ],
    "take": [
        "compared to", "increased", "decreased", "growth in", "driven primarily by",
    ],
    "risks": [
        "could adversely affect", "may adversely affect", "could harm",
        "could result in", "we may be unable", "could have a material adverse",
    ],
}
MIN_LENGTH, MAX_LENGTH = 70, 260


def sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned)]


def pick(text: str, cues: list[str], limit: int, used: set[str]) -> list[str]:
    chosen = []
    for sentence in sentences(text):
        if not (MIN_LENGTH <= len(sentence) <= MAX_LENGTH):
            continue
        lowered = sentence.lower()
        if not any(cue in lowered for cue in cues):
            continue
        if sentence in used:
            continue
        used.add(sentence)
        chosen.append(sentence)
        if len(chosen) == limit:
            break
    return chosen


def citation_of(filing, item: str = "") -> dict:
    return {
        "form": getattr(filing, "form", "") or "",
        "item": item or None,
        "filed_date": getattr(filing, "filing_date", "") or "",
        "accession": getattr(filing, "accession", "") or "",
        "url": getattr(filing, "url", "") or "",
    }


def claim(text: str, citation: dict) -> dict:
    # status "cited", never "verified": these are excerpts, not checked model
    # output, and the UI must not show a tick it has not earned.
    return {"text": text, "citation": citation, "status": "cited", "quote": text}


def from_filings(ticker: str) -> dict | None:
    wave2 = pipeline.wave2(ticker)
    wave3 = pipeline.wave3(wave2)
    payload = serialize.wave2(wave2)
    record = {
        "ticker": ticker,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "filing-excerpt",
        "provenance_note": (
            "Replay fixture. These bullets are verbatim excerpts from the filings "
            "named in their citations - not model output. Recorded without an API "
            "call because the account had no credits."
        ),
        "wave2": payload,
        "elapsed_seconds": 12.0,
        "answers": {},
    }
    if payload.get("classification") != "filer" or not wave3.tenk_text:
        record["brief"] = None
        return record

    tenk = wave3.tenk_filing or wave2.annual_filing
    tenk_citation = citation_of(tenk)
    used: set[str] = set()

    sections = []
    business = pick(wave3.tenk_text, CUES["business_line"], 1, used)
    sections.append({
        "key": "business_line",
        "heading": serialize.SECTION_HEADINGS["business_line"],
        "claims": [claim(t, tenk_citation) for t in business],
    })
    sections.append({
        "key": "take",
        "heading": serialize.SECTION_HEADINGS["take"],
        "claims": [claim(t, tenk_citation) for t in pick(wave3.tenk_text, CUES["take"], 3, used)],
    })
    sections.append({
        "key": "risks",
        "heading": serialize.SECTION_HEADINGS["risks"],
        "claims": [claim(t, tenk_citation) for t in pick(wave3.tenk_text, CUES["risks"], 3, used)],
    })

    changed = []
    for accession, text in (wave3.eight_k_texts or {}).items():
        filing = next(
            (f for f in wave2.eight_ks if f.accession == accession), None
        )
        if filing is None:
            continue
        picked = pick(text, [""], 1, used) or pick(text, CUES["take"], 1, used)
        for sentence in picked:
            changed.append(claim(sentence, citation_of(filing, ",".join(filing.items))))
        if len(changed) >= 3:
            break
    sections.append({
        "key": "whats_changed",
        "heading": serialize.SECTION_HEADINGS["whats_changed"],
        "claims": changed[:3],
    })

    record["brief"] = {"ticker": ticker, "dropped_citations": [], "sections": sections}

    # A generic recorded answer so follow-ups demo end to end.
    answer_claims = [
        {"text": c["text"], "quote": c["text"], "status": "cited", "citation": c["citation"]}
        for section in sections for c in section["claims"][:1]
    ][:3]
    record["answers"]["_default"] = {
        "text": (
            "Replay mode. The passages below are verbatim excerpts from this "
            "company's filings, shown to exercise the citation path without an "
            "API call. A live answer would be written by the model and its "
            "quotes checked against the filing text."
        ),
        "unsupported": "",
        "claims": answer_claims,
        "usage": {
            "seconds": 6.0, "cost": 0.0, "cache_read": 0, "cache_write": 0,
            "verified": 0, "checkable": 0, "indexed_only": 0,
        },
    }
    return record


def live(ticker: str) -> dict | None:
    wave2 = pipeline.wave2(ticker)
    wave3 = pipeline.wave3(wave2)
    payload = serialize.wave2(wave2)
    context = pipeline.build_synthesis_context(wave2, wave3)
    record = {
        "ticker": ticker,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "model",
        "provenance_note": None,
        "wave2": payload,
        "answers": {},
    }
    if context is None:
        record["brief"] = None
        return record
    import time

    started = time.monotonic()
    brief = synthesize.generate_brief(context)
    record["elapsed_seconds"] = round(time.monotonic() - started, 1)
    record["brief"] = serialize.brief(brief)
    return record


def main(argv: list[str]) -> int:
    from_filings_mode = "--from-filings" in argv
    tickers = [a.upper() for a in argv if not a.startswith("-")] or [
        "NVDA", "AAPL", "SPY", "BABA"
    ]
    for ticker in tickers:
        try:
            record = from_filings(ticker) if from_filings_mode else live(ticker)
        except Exception as exc:
            print(f"{ticker}: failed - {type(exc).__name__}: {exc}")
            continue
        if record is None:
            print(f"{ticker}: nothing recorded")
            continue
        path = fixtures.save(ticker, record)
        brief = record.get("brief") or {}
        claims = sum(len(s["claims"]) for s in brief.get("sections", []))
        print(f"{ticker}: {path.name} · {claims} claims · source={record['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
