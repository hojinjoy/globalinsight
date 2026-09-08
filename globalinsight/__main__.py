"""CLI entry point: ``uv run python -m globalinsight TICKER``.

Prints a JSON dump of everything the pipeline can produce without an LLM
call (quote, financials with citations, filing index, tiered 8-K fetch
results, 10-K text). The LLM synthesis step needs ANTHROPIC_API_KEY, which
this environment does not have, so it's opt-in via --synthesize and clearly
reported as skipped/failed rather than silently omitted.

Document bodies (10-K text, 8-K exhibit text) are summarized by character
and approximate-token count rather than dumped in full, to keep the CLI
output readable - pass --full-text to include the raw text instead.
"""

import argparse
import dataclasses
import json
import sys
import time
from typing import Any

from . import clean
from .pipeline import build_brief


def _dataclass_to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dataclass_to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {key: _dataclass_to_dict(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dataclass_to_dict(item) for item in obj]
    return obj


def _summarize_text(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    return {
        "chars": len(text),
        "tokens_approx": clean.count_tokens_approx(text),
        "preview": text[:280],
    }


def _build_report(ticker: str, synthesize: bool, full_text: bool) -> dict[str, Any]:
    started = time.monotonic()
    result = build_brief(ticker, synthesize=synthesize)
    elapsed = time.monotonic() - started

    wave2_dict = _dataclass_to_dict(result.wave2)
    wave3_dict = _dataclass_to_dict(result.wave3)

    if not full_text:
        wave3_dict["tenk_text"] = _summarize_text(result.wave3.tenk_text)
        wave3_dict["eight_k_texts"] = {
            accession: _summarize_text(text)
            for accession, text in result.wave3.eight_k_texts.items()
        }

    report: dict[str, Any] = {
        "ticker": ticker.upper(),
        "elapsed_seconds": round(elapsed, 3),
        "wave2": wave2_dict,
        "wave3": wave3_dict,
    }
    if synthesize:
        report["brief"] = _dataclass_to_dict(result.brief) if result.brief else None
        report["synthesis_error"] = result.synthesis_error
    else:
        report["synthesis"] = "skipped (pass --synthesize; requires ANTHROPIC_API_KEY)"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m globalinsight")
    parser.add_argument("ticker", help="Stock ticker, e.g. NVDA")
    parser.add_argument(
        "--synthesize",
        action="store_true",
        help="Also run the Claude synthesis step (requires ANTHROPIC_API_KEY).",
    )
    parser.add_argument(
        "--full-text",
        action="store_true",
        help="Include full 10-K/8-K text instead of a length summary.",
    )
    args = parser.parse_args(argv)

    report = _build_report(args.ticker, args.synthesize, args.full_text)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
