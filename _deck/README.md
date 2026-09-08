# _deck — the panel materials

The take-home asks for **one slide, ten minutes, treating the interview panel as client
stakeholders.** Two files, both written for a non-technical audience.

| File | What it is |
| --- | --- |
| [`slide.html`](slide.html) | The single slide. 16:9, scales to any projector, prints landscape. |
| [`pitch.html`](pitch.html) | The ten-minute talk track: six timed beats, the eight questions the panel asks, a demo run sheet, and the limits to name before they do. |

Published copies (private artifacts):

- Slide — <https://claude.ai/code/artifact/8399ce03-3078-4e8d-bfd4-c180fae0054a>
- Pitch — <https://claude.ai/code/artifact/6814fda2-c942-44fa-a4d7-94015c0e041f>

## The slide

Structure follows [`docs/DESIGN.md`](../docs/DESIGN.md) §16 — lead with the trust diagram,
not the demo — organised as the three questions a stakeholder actually asks:

1. **The problem** — an advisor on a live call has five minutes and must back up every word.
2. **What we're building** — the two-path diagram. Numbers are copied from the company's
   filed accounts and never pass through the model; narrative claims are checked back to a
   document, and unresolved ones are dropped.
3. **What it's worth** — 0 untraceable claims · facts on screen in ~1s · 85¢ per company ·
   11¢ per follow-up.

The footer carries the guardrail from §17: an advice question is refused before any fetch or
paid call. For this audience that is a stronger close than the prompt-level stance it
replaced, because it is an enforced control rather than a policy.

**Every number on the slide is measured** (§8, §12, and the UI spec's timings) with one
exception, called out in `pitch.html`: how long a pre-call read takes today is the firm's
own number, not ours. It is deliberately not on the slide.

Vocabulary is deliberately plain — no XBRL, accession numbers, model names or token counts.
The mapping from the internal terms is in the pitch script.

## Presenting

Read the run sheet in `pitch.html` first. Two things that decide whether the demo lands:

```bash
uv run python -m scripts.prewarm NVDA AAPL SPY   # nothing uncached is called on stage
GI_FIXTURES=1 ./run.sh                           # replay mode — the account has no credits
```

Replay reproduces the recorded timing, and the environment strip shows a **Replay** badge.
Say so if asked; never let the panel discover it.

## A note on the HTML

Both files are authored for the Artifact host, which supplies the
`<!doctype html><head>…<body>` wrapper at publish time — so they open with `<title>` and a
stylesheet link rather than a document skeleton. Browsers imply the missing tags, so opening
either file directly from disk works fine. Fonts load from Google Fonts and need a network
connection; without one they fall back to the system stack and the layout still holds.
