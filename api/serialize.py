"""Backend dataclasses -> JSON-safe dicts.

Deliberately tolerant. The backend package is owned by another team and its
dataclasses have already changed shape once mid-build, so every accessor here
uses getattr with a default rather than attribute access. A field that appears
is passed through; a field that disappears serialises as null instead of
raising. The front end treats every value as nullable for the same reason.
"""

from dataclasses import fields, is_dataclass
from typing import Any

from globalinsight import filings as filings_mod

# Display labels for the XBRL concept keys the backend emits. Order matters:
# it is the row order of the financial trend table.
CONCEPT_ORDER = ["Revenue", "GrossProfit", "NetIncomeLoss", "EPS"]
CONCEPT_LABELS = {
    "Revenue": "Revenue",
    "GrossProfit": "Gross profit",
    "NetIncomeLoss": "Net income",
    "EPS": "Diluted EPS",
}


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def plain(obj: Any) -> Any:
    """Recursively convert dataclasses/containers to JSON-safe values."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if is_dataclass(obj):
        return {f.name: plain(getattr(obj, f.name, None)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [plain(v) for v in obj]
    return str(obj)


def citation(obj: Any) -> dict | None:
    if obj is None:
        return None
    item = _get(obj, "item") or ""
    return {
        "form": _get(obj, "form") or "",
        # An empty item is correct for a 10-K (supplied whole-document, so no
        # Item can honestly be attributed). Null, not "", so the client cannot
        # accidentally render an empty slot. See UI-SPEC section 6.3.
        "item": item or None,
        "filed_date": _get(obj, "filed_date") or "",
        "accession": _get(obj, "accession") or "",
        "url": _get(obj, "url") or "",
    }


def quote(obj: Any) -> dict | None:
    if obj is None:
        return None
    return {
        "ticker": _get(obj, "ticker"),
        "available": bool(_get(obj, "available", False)),
        "error": _get(obj, "error"),
        "price": _get(obj, "price"),
        "change": _get(obj, "change"),
        "change_percent": _get(obj, "change_percent"),
        "market_cap": _get(obj, "market_cap"),
        "pe_ratio": _get(obj, "pe_ratio"),
        "week52_low": _get(obj, "week52_low"),
        "week52_high": _get(obj, "week52_high"),
        "currency": _get(obj, "currency") or "USD",
        "as_of": _get(obj, "as_of"),
    }


def financials(series_map: dict) -> dict:
    """Concept -> {label, unit, points[]}, ordered for display."""
    out = {}
    keys = [k for k in CONCEPT_ORDER if k in series_map]
    keys += [k for k in series_map if k not in CONCEPT_ORDER]
    for key in keys:
        series = series_map[key]
        out[key] = {
            "concept": key,
            "label": CONCEPT_LABELS.get(key, key),
            "unit": _get(series, "unit") or "USD",
            "points": [
                {
                    "fiscal_year": _get(p, "fiscal_year"),
                    "value": _get(p, "value"),
                    "citation": citation(_get(p, "citation")),
                }
                for p in (_get(series, "points") or [])
            ],
        }
    return out


def filing(obj: Any) -> dict | None:
    """One FilingRef. `tier` predicts whether the body will be retrieved.

    Tier comes from the backend's own determine_tier() rather than a copy of
    the policy, so it cannot drift from what fetch_tiered_8ks actually does.
    """
    if obj is None:
        return None
    items = list(_get(obj, "items") or [])
    try:
        tier = filings_mod.determine_tier(items)
    except Exception:
        tier = None
    return {
        "form": _get(obj, "form") or "",
        "accession": _get(obj, "accession") or "",
        "filing_date": _get(obj, "filing_date") or "",
        "report_date": _get(obj, "report_date") or "",
        "url": _get(obj, "url") or "",
        "items": items,
        "tier": tier,
        # Confirmed later, in the wave3_start event. Null means "not yet known".
        "body_retrieved": None,
    }


def classify(wave2: Any) -> str:
    """filer | etf | pre_annual - the server decides, the client renders.

    `etf`        no SEC CIK at all, or a CIK that never files an annual report
    `pre_annual` a filer with something on file (typically an S-1) but no 10-K
    """
    if _get(wave2, "company") is None:
        return "etf"
    if _get(wave2, "annual_filing") is not None:
        return "filer"
    return "pre_annual" if (_get(wave2, "other_filings") or []) else "etf"


def wave2(result: Any) -> dict:
    company = _get(result, "company")
    return {
        "ticker": _get(result, "ticker"),
        "is_sec_filer": bool(company is not None),
        "classification": classify(result),
        "company": None
        if company is None
        else {
            "ticker": _get(company, "ticker"),
            "cik": _get(company, "cik"),
            "name": _get(company, "name"),
        },
        "quote": quote(_get(result, "quote")),
        "financials": financials(_get(result, "financials") or {}),
        "annual_filing": filing(_get(result, "annual_filing")),
        "eight_ks": [filing(f) for f in (_get(result, "eight_ks") or [])],
        "other_filings": [filing(f) for f in (_get(result, "other_filings") or [])],
        "errors": dict(_get(result, "errors") or {}),
    }


# --- brief -------------------------------------------------------------------

# Headings the backend leaves to the presentation layer. business_line comes
# back with heading="" by design.
SECTION_HEADINGS = {
    "business_line": "How they make money",
    "take": "The 30-Second Take",
    "risks": "Key Risks",
    "whats_changed": "What's Changed",
}
SECTION_ORDER = ["business_line", "take", "risks", "whats_changed"]


def section(obj: Any, key: str) -> dict:
    """Zip the backend's parallel content/citations lists into claims.

    The client never indexes two arrays in step; a content entry with no
    matching citation is dropped here, because a claim without a citation must
    not render at all.
    """
    content = list(_get(obj, "content") or [])
    citations = list(_get(obj, "citations") or [])
    claims = [
        {
            "text": text,
            "citation": citation(cit),
            # Brief citations carry no verbatim excerpt (the backend's
            # _CITATION_SCHEMA has no `quote` field), so nothing was checked
            # against the source. "cited" must never render as a tick.
            "status": "cited",
            "quote": None,
        }
        for text, cit in zip(content, citations)
        if cit is not None
    ]
    return {
        "key": key,
        "heading": _get(obj, "heading") or SECTION_HEADINGS.get(key, key),
        "claims": claims,
    }


def brief(obj: Any) -> dict:
    out = {
        "ticker": _get(obj, "ticker"),
        "dropped_citations": list(_get(obj, "dropped_citations") or []),
        "sections": [],
    }
    present = [k for k in SECTION_ORDER if _get(obj, k) is not None]
    # Any section the backend adds later is appended rather than dropped.
    known = set(SECTION_ORDER)
    for f in (fields(obj) if is_dataclass(obj) else []):
        value = getattr(obj, f.name, None)
        if f.name not in known and hasattr(value, "content"):
            present.append(f.name)
    out["sections"] = [section(_get(obj, key), key) for key in present]
    return out


def answer(obj: Any) -> dict:
    """chatui/api qa.Answer -> JSON, with the verification counts the footer needs."""
    claims = list(_get(obj, "claims") or [])
    checkable = [c for c in claims if _get(c, "status") in ("verified", "unverified")]
    return {
        "text": _get(obj, "text") or "",
        "unsupported": _get(obj, "unsupported") or "",
        # Non-null means the question was declined on policy grounds and no
        # model call was made: the client renders `text` as the whole answer
        # and must not show the verification footer, which would imply a
        # generated answer was checked.
        "refusal": _get(obj, "refusal_reason") or None,
        "claims": [
            {
                "text": _get(c, "text") or "",
                "quote": _get(c, "quote") or "",
                "status": _get(c, "status") or "unknown",
                "citation": citation(_get(c, "citation")),
            }
            for c in claims
        ],
        "usage": {
            "seconds": round(float(_get(obj, "seconds", 0.0) or 0.0), 1),
            "cost": round(float(_get(obj, "cost", 0.0) or 0.0), 4),
            "cache_read": _get(obj, "cache_read", 0) or 0,
            "cache_write": _get(obj, "cache_write", 0) or 0,
            "verified": sum(1 for c in checkable if _get(c, "status") == "verified"),
            "checkable": len(checkable),
            "indexed_only": sum(
                1 for c in claims if _get(c, "status") == "metadata"
            ),
        },
    }
