"""Pre-cache demo tickers to disk. Nothing uncached should be called on stage.

    uv run python -m scripts.prewarm NVDA AAPL MSFT JPM
    uv run python -m scripts.prewarm --no-narrative SPY
"""

import sys
import time

from globalinsight import pipeline, synthesize


def prewarm(ticker: str, with_narrative: bool = True) -> None:
    print(f"\n=== {ticker} ===")
    started = time.monotonic()
    wave2 = pipeline.wave2(ticker)
    print(f"  wave2 {time.monotonic() - started:.2f}s")
    if not wave2.is_sec_filer:
        print(f"  not an SEC filer — quote only: {wave2.quote.price}")
        return
    print(f"  {wave2.company.name} (CIK {wave2.company.cik})")
    print(f"  quote: {wave2.quote.price if wave2.quote.available else wave2.quote.error}")
    print(
        f"  annual: {wave2.annual_filing.form if wave2.annual_filing else 'none'}"
        f" | 8-Ks: {len(wave2.eight_ks)} | XBRL: {list(wave2.financials)}"
    )
    if wave2.errors:
        print(f"  errors: {wave2.errors}")
    if wave2.annual_filing is None:
        return

    started = time.monotonic()
    wave3 = pipeline.wave3(wave2)
    size = len(wave3.tenk_text or "") + sum(len(t) for t in wave3.eight_k_texts.values())
    print(
        f"  wave3 {time.monotonic() - started:.2f}s | {size:,} chars "
        f"(~{size // 4:,} tokens) | 8-K bodies: {len(wave3.eight_k_texts)}"
    )
    if wave3.errors:
        print(f"  errors: {wave3.errors}")

    if not with_narrative:
        return
    context = pipeline.build_synthesis_context(wave2, wave3)
    if context is None:
        print("  nothing to synthesise from")
        return
    started = time.monotonic()
    try:
        brief = synthesize.generate_brief(context)
    except Exception as exc:
        print(f"  narrative failed: {type(exc).__name__}: {exc}")
        return
    print(
        f"  narrative: {time.monotonic() - started:.0f}s | "
        f"{len(brief.take.content)} take, {len(brief.risks.content)} risks, "
        f"{len(brief.whats_changed.content)} changes"
    )


if __name__ == "__main__":
    arguments = sys.argv[1:]
    with_narrative = "--no-narrative" not in arguments
    tickers = [a for a in arguments if not a.startswith("-")] or [
        "NVDA", "AAPL", "MSFT", "JPM"
    ]
    for symbol in tickers:
        prewarm(symbol.upper(), with_narrative)
