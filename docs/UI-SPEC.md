# GlobalInsight — Front-End Product Specification

**Status:** approved for build · **Owner:** Product · **Implementer:** senior JS developer
**Supersedes:** the Streamlit prototype (`app.py`, `chatui/`)
**Stack:** vanilla JavaScript (ES modules), no framework, no build step · Starlette API

---

## 1. The product, in one paragraph

An advisor is on a client call. The client asks *"what do you think about NVDA?"* and
the advisor has about five minutes. They need to sound informed **and be able to defend
every claim they make.** In wealth management, repeating an unverifiable AI claim to a
client is not an embarrassment — it is a compliance event.

So the product is not "AI summarises filings." It is **AI that shows its work.** Every
number is retrieved from SEC XBRL and never generated. Every narrative claim names the
filing, item and date it came from and links to it on EDGAR. The front end exists to make
that provenance impossible to miss — and to put usable content on screen before the model
has said a word.

**The panel-facing claim we are building toward:** *any team can summarise a 10-K; we can
prove where every number came from.*

---

## 2. Why we are replacing the Streamlit prototype

The prototype works and validated the pipeline. It is not demo-ready. Seven specific
problems, each of which the new UI must fix:

| # | Problem | Consequence |
|---|---|---|
| 1 | Streamlit re-runs the whole script on every message and re-renders all prior turns | Turn 6 costs the same as turn 1; state bugs are easy and history flickers |
| 2 | `st.spinner` / `st.status` block the render thread | The three-wave contract collapsed — wave 2 arrived as one lump, and the design doc's "advisor talks at 2.5s" property was lost |
| 3 | Citations live in `st.popover` | Cannot be printed, poor keyboard support, invisible to a screen reader until opened |
| 4 | No print or export path at all | An advisor prints this before a call. The prototype has no answer |
| 5 | Framework chrome — hamburger, "Deploy" button, "Made with Streamlit" | Reads as a prototype to a stakeholder panel |
| 6 | No URL routing | Cannot link a panel member to a specific brief |
| 7 | Blocking synchronous calls in the render path | One slow filing fetch freezes the entire page, defeating failure isolation |

The prototype also *hid a real defect*: brief citations carry no verification state at
all, and nothing in the UI said so. See §6.

---

## 3. Architecture and ownership

```
globalinsight/          BACKEND — owned by another team. DO NOT EDIT.
                        Python package, no HTTP surface.
                        pipeline.wave2 / wave3 / build_synthesis_context
                        synthesize.generate_brief
api/                    NEW, OURS. Thin HTTP layer. Starlette + uvicorn.
  app.py                routes, SSE plumbing
  serialize.py          backend dataclasses -> JSON
  qa.py                 follow-up Q&A (moved from chatui/qa.py)
  fixtures/             recorded responses for replay mode
web/                    NEW, OURS. Static assets, served by the same Starlette app.
  index.html
  css/
  js/
docs/UI-SPEC.md         this document
scripts/                OURS. prewarm, fixture recorder.
```

**Rules of engagement.** `api/` contains **no business logic** — it translates HTTP to
existing backend calls and serialises the results. Any behaviour that feels like domain
logic belongs in `globalinsight/`; if it is missing there, raise it as a backend request
rather than implementing it in `api/`.

**Where follow-up Q&A belongs.** `chatui/qa.py` is backend logic — it builds a prompt,
calls Claude, and verifies citations. It has no UI dependency. **It belongs in
`globalinsight/synthesize.py` as `answer_question()`.** We cannot put it there because we
do not own that package, so it moves to `api/qa.py` as a documented temporary home. File a
backend request to upstream it. Do not let it grow HTTP or rendering concerns in the
meantime — it must stay a pure function so the move is a file move.

---

## 4. The three-wave rendering contract

The two halves of the page differ in latency by roughly 100×. Rendering them as one unit
leaves the advisor watching a spinner while holding a live client call.

| Wave | Arrives | Content | Nature |
|---|---|---|---|
| 1 | < 200ms | App shell, ticker echo, skeletons with reserved height | Static |
| 2 | ~0.9s cold, < 300ms warm | Quote header, XBRL financial trend, filing index | Deterministic |
| 3 | 45–60s typical, ~90s for JPM | Narrative — how they make money, the take, risks, what's changed | Generative |

Measured: `wave2` 0.88s cold for AAPL, 0.23s for SPY. NVDA's full brief 57.5s; BABA 46.9s.

**The advisor can start talking at ~1s.** Price, valuation, three years of real revenue and
margin, and confirmation of exactly which filings exist all land before the model starts.
The narrative arrives while they are still on pleasantries.

**Rules the implementation must honour:**

- Wave 2 renders the moment its event arrives. It must never wait on wave 3.
- Skeletons reserve the height wave 3 will occupy. **Zero cumulative layout shift** when
  the narrative lands — the advisor may be reading the risk table as it does.
- The wait state is a **live status line naming the work in progress**, not a spinner:
  > *Reading 10-K filed 2026-02-25 + 7 8-K bodies (3 more indexed by item code) —
  > ~173,000 tokens · 18s*

  This makes the delay legible and demonstrates the grounding work to the panel rather
  than hiding it.
- The elapsed counter updates at ~1s. It is driven by server heartbeat events, not a
  client-side timer, so it stays truthful if the connection stalls.

**Known constraint:** the backend's `generate_brief` uses a non-streaming call, so the API
can only emit elapsed-time heartbeats for wave 3 — there is no partial brief text to show.
Follow-up Q&A *does* stream and must render tokens as they arrive. Do not fake progress for
the brief; an honest elapsed clock against a named task is better than a fake progress bar.
*Backend request: make `generate_brief` streaming so we can render the take as it forms.*

---

## 5. Per-block provenance

The blocks differ in freshness by **months**. A single page-level timestamp would be
actively misleading — it would imply the risk factors are as current as the price. Every
block carries its own provenance line, always visible, never behind a hover:

| Block | Provenance line |
|---|---|
| Quote | `Quote as of 2026-09-08 20:00 UTC` |
| Market cap / P/E | `P/E derived from price and FY2025 diluted EPS (10-K filed 2025-10-31)` |
| Financial trend | `Retrieved from SEC XBRL structured data, never model-generated · 10-K filed 2026-02-25 · accession 0001045810-26-000023` |
| Filing index | `10 8-K filings since the 10-K · 7 bodies retrieved, 3 indexed by item code` |
| Narrative | `Grounded in 10-K filed 2026-02-25 + 10 8-Ks through 2026-09-03 · synthesised in 57s` |

The XBRL line is the one that lands with a finance panel. Give it visual weight — it is the
answer to *"how do we know it isn't making the numbers up?"*

---

## 6. Citation UX — the load-bearing component

### 6.1 Four states on screen, plus a disclosure

`globalinsight/synthesize.py`'s `_CITATION_SCHEMA` contains only `form`, `item`,
`filed_date`, `accession`, `url` — **there is no `quote` field.** Brief claims therefore
carry no verifiable excerpt and **cannot be checked against the source text.** Only
follow-up answers (via `api/qa.py`, which adds a `quote` to its own schema) can be.

This asymmetry is real and must not be papered over.

| State | Applies to | Badge | Label shown to the user |
|---|---|---|---|
| **Verified** | Q&A claims whose quote was located verbatim in the filing text | `✓` solid | *Quote located in source* |
| **Unverified** | Q&A claims whose quote was not located | `!` amber | *Quote not located — verify before use* |
| **Indexed only** | Claims citing a tier-3 8-K whose body was never retrieved | `◦` outline | *Filing indexed by item code; body not retrieved* |
| **Cited** | All brief claims | `→` neutral | *Cited to filing; excerpt not captured* |

**Never render a check mark we have not earned.** A "Cited" chip is neutral grey, carries no
tick, and its label states plainly that no excerpt was captured. A compliance reviewer must
be able to tell at a glance which claims were machine-checked and which were not.

*Backend request, high priority: add a `quote` field to the brief citation schema. It
promotes every brief claim from "Cited" to "Verified" and is the single highest-value
change available to this product.* Measured evidence it works: 13/13 NVDA follow-up quotes
and 15/16 AAPL follow-up quotes verified verbatim.

### 6.2 Dropped citations — show the catch, don't hide it

The backend resolves every citation against the filings it actually supplied. A claim
citing an accession that was never supplied — a plausible-looking fabrication — is
**dropped before it reaches us**, and its accession is recorded in
`Brief.dropped_citations`.

Surface this. It is not an embarrassment; it is the strongest live evidence of the
compliance story, and hiding it would waste the backend's best feature. Render a
disclosure line beneath the narrative whenever the list is non-empty:

> **1 claim withheld.** It cited accession `0000000000-00-000000`, which was not among the
> filings supplied to the model. Withheld rather than shown as sourced.

Neutral styling — an informational note, not an error. In the normal case the list is empty
and nothing renders. When it fires during a demo, it is worth pausing on.

### 6.3 A 10-K citation has no item number, and that is correct

`item` is nullable by design. The 10-K is supplied whole-document — no item segmentation —
so no specific Item can honestly be attributed to a 10-K claim; the schema instructs the
model never to fabricate one. 8-K citations do carry item codes.

Render `10-K, filed 2026-02-25` with no item segment. **Do not show "Item —", "Item n/a",
or an empty slot** — a missing item is not missing data, and the UI must not imply the
citation is incomplete.

### 6.2 Behaviour

- A citation renders as an inline chip immediately after the claim it supports:
  `→ 10-K, Item 1A, filed 2026-02-25`
- Chip is a `<button>` that opens a **popover** (not a modal — the advisor must keep reading
  the claim). Popover contains: the verbatim excerpt when we have one, the verification
  state with its plain-language label, the accession number, and an **Open on EDGAR** link.
- EDGAR link opens in a new tab, `rel="noopener"`. It goes to the exact document URL the
  backend supplied — never a constructed URL.
- Keyboard: chip is focusable, `Enter`/`Space` opens, `Escape` closes and returns focus,
  arrow keys move between chips within a section.
- **If a claim has no citation, it does not render.** That constraint is the product.

### 6.3 On paper

Popovers are useless in print. The print stylesheet converts every citation chip into a
numbered footnote, and appends a **Sources** section listing form, item, filing date,
accession, verification state and full URL. See §12.

---

## 7. Screen inventory

One screen. Deep-linkable via `#/NVDA`.

```
┌────────────┬───────────────────────────────────────────────┐
│            │  GlobalInsight              [Print] [Copy]    │
│  Session   ├───────────────────────────────────────────────┤
│  rail      │                                               │
│            │   NVDA — NVIDIA CORP            [20-F badge]  │
│  · NVDA    │   ┌─────────────────────────────────────────┐ │
│  · AAPL    │   │ Price  Mkt cap  P/E  52w low  52w high  │ │
│  · SPY     │   │ Quote as of …                           │ │
│            │   └─────────────────────────────────────────┘ │
│            │   Financial trend        (XBRL table)         │
│  ─────────  │   Filings since the 10-K (expandable)        │
│  Environment│   ─────────────────────────────────────────  │
│  ✓ SEC UA  │   [ wave-3 status line, then narrative ]      │
│  ! Credits │   How they make money  (one line)            │
│  Replay    │   The 30-Second Take   (3 bullets)           │
│            │   Key Risks (3, ranked) · What's Changed     │
│            ├───────────────────────────────────────────────┤
│            │  [ Ticker, company, or a question…      ] [→] │
└────────────┴───────────────────────────────────────────────┘
```

**Left rail** — 240px, collapsible, hidden below 900px and in print. Holds the session's
tickers (click to re-render from client cache, no refetch) and the environment strip.

**Main column** — max-width 760px. Single-column brief, read top to bottom like a research
note. This matches how an advisor scans before a call and prints cleanly.

**Composer** — pinned to the bottom, always focused on load. Accepts a ticker (`NVDA`),
a `$`-prefixed symbol, a company name (*Nvidia*), or a plain question about the active
ticker. Typeahead suggests tickers after 2 characters. Demo-ticker chips (NVDA, AAPL, MSFT,
JPM) sit above it on an empty session and disappear once a brief is loaded.

### Component list

| Component | Responsibility |
|---|---|
| `AppShell` | Layout, routing, session state, keyboard shortcuts |
| `Composer` | Input, typeahead, demo chips, submit |
| `EnvironmentStrip` | Health: SEC UA, credentials, cache size, replay badge |
| `BriefHeader` | Ticker, company name, form badge (10-K / 20-F / 40-F) |
| `QuoteCard` | Five metrics + provenance caption + degraded states |
| `FinancialTrend` | 3-year XBRL table + accession footnote |
| `FilingIndex` | 8-K list, item codes with plain-English labels, tier indicator |
| `WaveStatus` | Live "reading…" line with elapsed, `aria-live="polite"` |
| `BriefSection` ×4 | Heading, one-line claims, citation chips |
| `DroppedCitations` | Withheld-claim disclosure (§6.2), renders only when non-empty |
| `CitationChip` | Chip + popover + EDGAR link + verification state |
| `AnswerCard` | Streamed answer text, claims, usage/verification line |
| `Notice` | Edge cases and per-block errors (ETF, IPO, no-credits, fetch failure) |

### States every data component must implement

`empty` · `loading` (skeleton, reserved height) · `ready` · `partial` (rendered with a
caveat, e.g. quote present but market cap missing) · `error` (scoped to the block, with an
actionable message) · `unavailable` (structurally absent, e.g. an ETF has no filings — this
is **not** an error and must not look like one).

---

## 8. Failure isolation

**One dead API must never blank the page.** Every block renders independently and degrades
in place. These are the failure modes we actually hit in testing, not hypotheticals:

| Failure | Observed | UI behaviour |
|---|---|---|
| Yahoo `429` / `401` on quote | Frequent; confirmed live | Quote card shows "Quote unavailable" with the reason and the age of the last cached value. Everything else renders normally. |
| `quoteSummary` 401 → no market cap or P/E | Every request in testing | Market cap shows `—` with caption *"not returned by the quote endpoint"*. **P/E is derived** from price ÷ XBRL diluted EPS and captioned as derived (AAPL 42.4, NVDA 46.1). Never show a bare dash where we can compute an honest value. |
| SEC `403` from a bad User-Agent | Reproduced: any UA containing `example.com`, or a bare `Mozilla/5.0` | Environment strip turns red **before** the user types. Message names the exact fix: set `SEC_USER_AGENT` to a real contact address. Brief is blocked with that message, not a stack trace. |
| Filing document fetch fails | `wave3.errors` populated | Brief renders wave 2 in full plus a notice naming which document failed. Narrative proceeds on whatever text was retrieved. |
| Synthesis `400 — credit balance too low` | **Current state of the account** | A dedicated no-credits state, visually distinct from a crash: explains that waves 1–2 are complete and unaffected, and offers the replay-mode hint. Never surfaces the raw API error string to a panel. |
| SSE connection drops mid-wave-3 | — | Status line switches to *"connection lost"*, offers Retry. Already-rendered blocks stay. One automatic reconnect, then manual. |
| Unknown ticker | — | Composer inline error, no page mutation. Suggests the closest matches. |

**Implementation rule:** every block subscribes to its own slice of the event stream. A
thrown error inside one component's render must be caught at that component's boundary and
converted to its `error` state — it must not propagate to the shell.

---

## 9. Edge cases that must be visibly handled

These are real filers, each verified against the live pipeline. All four go in the demo
script.

| Case | Example | Required behaviour |
|---|---|---|
| **ETF / fund** | `SPY` — has a CIK (884394) but never files an annual report | Quote card renders fully. Filing half shows an `unavailable` notice: *"ETFs and funds file no annual report, so the filing half of this brief is empty by design."* Must **not** read as an error, and must **not** say "recent IPO". |
| **ADR filing 20-F** | `BABA` — 20-F filed 2026-05-20, ~319k tokens | Header carries a `20-F` badge. Everything else identical. Brief completed in 46.9s. |
| **Canadian issuer 40-F** | — | Same treatment as 20-F. |
| **Fresh IPO** | S-1 on file, no annual report | Distinct notice from the ETF case: *"No annual report on file yet — only S-1."* Discriminated on `other_filings` being non-empty. |
| **Very large filer** | `JPM` — 10-K is 1,192,309 chars ≈ 298k tokens | Wait state must show the token estimate so a ~90s wait is legible. No timeout below 180s on the wave-3 stream. |
| **Tier-3 8-Ks** | NVDA — 3 of 10 8-Ks are items 8.01 / 5.07, indexed but body never fetched | Filing index marks them *indexed only*. Claims citing them get the "Indexed only" chip, **never dropped.** (The prototype silently deleted them, which emptied the *"what's changed"* answer — the exact question those filings answer.) |

---

## 10. API contract

Base path `/api`. All responses `application/json` except streams, which are
`text/event-stream`.

### 10.1 Streaming strategy

**Use `fetch()` + `ReadableStream` with a single shared SSE parser module for both
streams.** Do **not** use `EventSource`: it is GET-only (so it cannot carry the Q&A request
body) and cannot set headers, which would force two different stream-reading code paths.
One parser, one reconnect policy, one error path.

### 10.2 Endpoints

#### `GET /api/health`

Powers the environment strip. Must never fail.

```json
{
  "mode": "live",
  "credentials": false,
  "sec_user_agent_configured": true,
  "cache_bytes": 89128960,
  "model": "claude-opus-5",
  "effort": "high",
  "fixtures_available": ["NVDA", "AAPL", "SPY", "BABA"]
}
```

#### `GET /api/tickers?q=nvi&limit=8`

Typeahead. Server-side because the ticker table is 10,415 rows and already cached in Python.

```json
[{ "ticker": "NVDA", "name": "NVIDIA CORP" }]
```

#### `GET /api/resolve?q=<text>&active=<ticker>`

Chat intent. Server-side for the same reason. Returns what the composer should do.

```json
{ "kind": "brief", "ticker": "NVDA", "question": "" }
```

`kind` ∈ `brief` | `followup` | `help`. A ticker already active means *follow-up*, not
*regenerate* — otherwise every question naming the company rebuilds the brief.

#### `GET /api/quote/{ticker}`

Standalone quote for the 60s refresh. Returns the `Quote` shape; `available: false` with an
`error` string on failure — **HTTP 200 either way.** A failed quote is a data state, not a
transport error.

#### `GET /api/brief/{ticker}` — SSE

The core endpoint. One connection carries the whole three-wave render.

```
event: meta
data: {"ticker":"NVDA","request_id":"...","mode":"live"}

event: wave2
data: {"is_sec_filer":true,"classification":"filer",
       "company":{"ticker":"NVDA","cik":1045810,"name":"NVIDIA CORP"},
       "quote":{"available":true,"price":225.73,"change":…,"change_percent":…,
                "market_cap":null,"pe_ratio":null,"week52_low":164.27,
                "week52_high":236.54,"currency":"USD","as_of":"2026-09-08T20:00:01+00:00"},
       "financials":{"Revenue":{"concept":"Revenue","unit":"USD",
                     "points":[{"fiscal_year":2024,"value":…,
                                "citation":{…}}]}},
       "annual_filing":{"form":"10-K","accession":"…","filing_date":"2026-02-25",
                        "report_date":"…","url":"https://www.sec.gov/…"},
       "eight_ks":[{"form":"8-K","filing_date":"2026-09-03","items":["8.01"],
                    "accession":"…","url":"…","body_retrieved":false}],
       "other_filings":[],
       "errors":{}}

event: wave3_start
data: {"annual":{"form":"10-K","filed":"2026-02-25"},
       "eight_k_bodies":7,"eight_k_indexed_only":3,"tokens_approx":173073}

event: progress
data: {"elapsed_seconds":18.2,"phase":"synthesis"}

event: brief
data: {"business_line":{"heading":"How they make money",
                        "claims":[{"text":"…","citation":{…},"status":"cited"}]},
       "take":{"heading":"The 30-Second Take","claims":[…]},
       "risks":{"heading":"Key Risks","claims":[…]},
       "whats_changed":{"heading":"What's Changed","claims":[…]},
       "dropped_citations":["0000000000-00-000000"]}

event: block_error
data: {"block":"synthesis","kind":"no_credits",
       "message":"Synthesis unavailable — the API account has no credits.",
       "actionable":"Waves 1–2 are complete. Set GI_FIXTURES=1 to demo from recorded data."}

event: done
data: {"elapsed_seconds":57.5}
```

**Section shapes are fixed:** `business_line` holds 0 or 1 claim, `take` exactly 3,
`risks` exactly 3 ranked most-material first, `whats_changed` up to 3 (empty when no 8-Ks
were supplied). Every bullet is **one line** — the brief is sized to fit one screen for an
advisor scanning it mid-call. Do not design for paragraphs.

The backend returns `content: []` and `citations: []` as parallel lists on each section;
**`api/serialize.py` zips them into `claims: [{text, citation}]`** so the client never has
to index two arrays in step. `status` is `"cited"` for every brief claim (see §6.1).

`dropped_citations` carries accessions the model cited that did not resolve to a supplied
filing. Render per §6.2 when non-empty.

`classification` ∈ `filer` | `etf` | `pre_annual` — the server decides the edge case, the
client only renders it. Do not re-derive this in JavaScript.

`body_retrieved` on each 8-K drives the *indexed only* marker. The API must add it; the
backend's `FilingRef` does not carry it (derive from `wave3.eight_k_texts` membership).

**`block_error` is not fatal.** The stream continues and still emits `done`. Everything
already rendered stays.

#### `POST /api/ask` — SSE

```jsonc
// request
{ "ticker": "NVDA", "question": "What has changed since the 10-K?",
  "history": [{ "role": "user", "content": "…" }, { "role": "assistant", "content": "…" }] }
```

```
event: meta   data: {"ticker":"NVDA"}
event: delta  data: {"text":"Three things"}          // real token streaming
event: answer
data: {"text":"…","unsupported":"",
       "claims":[{"text":"…","quote":"…","status":"verified","citation":{…}}],
       "usage":{"seconds":47.6,"cost":1.19,"cache_read":0,"cache_write":173073,
                "verified":13,"checkable":13,"indexed_only":0}}
event: done   data: {"elapsed_seconds":47.6}
```

The server holds the `SynthesisContext` per ticker so follow-ups do not re-fetch documents.
The 1-hour prompt cache makes the second and later questions cheap: measured **$1.19 to
write the cache, then $0.11 and 23s per question after** (AAPL: 101,341 tokens; NVDA:
173,073).

Show `usage` in the answer footer. Cost transparency is part of the pitch — *"about $0.11
a follow-up"* is a better answer to the panel than silence.

### 10.3 Error envelope

Non-stream endpoints on failure:

```json
{ "error": { "kind": "sec_forbidden", "message": "…", "actionable": "Set SEC_USER_AGENT…" } }
```

`kind` ∈ `unknown_ticker` · `sec_forbidden` · `quote_unavailable` · `filing_fetch_failed` ·
`no_credits` · `synthesis_failed` · `rate_limited` · `internal`.

Every error carries an `actionable` string. The UI shows `message` and, when present,
`actionable`. Raw exception text never reaches the browser.

---

## 11. Replay mode — a first-class requirement

**The Anthropic account currently has no credits.** Every live synthesis returns
`400 — credit balance too low`. The front end must be buildable, testable and demoable
without spending a cent, and this is not a stub — it is how the demo runs.

- `GI_FIXTURES=1` puts the API in replay mode. `/api/health` reports `"mode": "replay"` and
  the UI shows a discreet **Replay** badge in the environment strip. Never pretend replayed
  data is live.
- Fixtures live in `api/fixtures/{TICKER}.json`: recorded wave 2, wave 3 metadata, the
  brief, and canned answers keyed by a normalised question hash with a generic fallback.
- `scripts/record_fixtures.py` records them from live runs when credits exist. Ours, not
  the backend's.
- **Replay must reproduce the real timing.** Fixtures store the measured elapsed
  (NVDA 57.5s, BABA 46.9s, AAPL follow-up 23s) and the API replays heartbeats against it,
  scaled by `GI_FIXTURE_SPEED` (default `1.0`). Without this the wave-3 wait state never
  gets exercised and we ship an untested status line. Set `GI_FIXTURE_SPEED=10` for fast
  test runs.
- Waves 1–2 need no fixtures at all: **85 MB of EDGAR, XBRL and filing text is already on
  disk** and works offline today. SPY renders in 0.23s from cache with no network at all.

---

## 12. Accessibility, print and export

**Accessibility** is not optional — this ships to a regulated industry.

- Semantic landmarks: `<header>`, `<nav>`, `<main>`, `<form>`. One `<h1>`, sections use
  `<h2>`/`<h3>` in order.
- Wave status is `aria-live="polite"`, so a screen-reader user hears *"Reading 10-K… 18s"*
  and knows the page is working.
- When a brief completes, move focus to the brief heading so keyboard users land on new
  content rather than being stranded in the composer.
- Citation chips are real `<button>`s in the tab order. Popovers trap nothing, close on
  `Escape`, and return focus.
- Verification state is **never colour alone** — every state carries a glyph and a text
  label.
- Contrast ≥ 4.5:1. Respect `prefers-reduced-motion` (skeleton shimmer and the elapsed
  counter both stop).
- Full keyboard path: `/` focuses composer, `Escape` closes popovers, `Ctrl/Cmd+P` prints.

**Print** — the advisor prints this before the call.

- `@media print`: hide the rail, composer, chrome and all interactive affordances.
- **Every citation chip becomes a numbered footnote marker**, and the brief ends with a
  **Sources** list: form, item, filing date, accession, verification state, full URL.
  A popover is worthless on paper; this is the single most important print rule.
- Per-block provenance lines print verbatim. Page breaks avoid splitting a claim from its
  citation marker.
- Header on every page: ticker, company, generation timestamp.

**Export** — a **Copy brief** action puts clean Markdown on the clipboard, citations
inlined as `[form, item, filed date](url)`, provenance lines intact. This is how a brief
reaches a CRM note or an email, and it is a 20-line feature.

---

## 13. Performance budget

| Metric | Budget |
|---|---|
| First paint (static shell) | < 200ms |
| `wave2` event rendered | < 1.5s cold, < 400ms warm |
| Cumulative layout shift when wave 3 lands | 0 |
| Composer keystroke → typeahead | < 100ms |
| Re-rendering a prior ticker from the rail | < 50ms, no network |
| Total JS shipped | < 60KB uncompressed, no bundler |

Prior turns render from a client-side cache. Unlike the prototype, turn 6 must cost the
same as turn 1.

---

## 14. Acceptance criteria

Each is testable. **The build is not done until every one passes.**

**Core**
1. Typing `NVDA` renders the quote card and financial trend within 1.5s cold, before any
   narrative exists.
2. The wave-3 status line names the filings being read, shows a token estimate, and updates
   its elapsed counter at ~1s from server heartbeats.
3. When the narrative lands, cumulative layout shift is 0.
4. Every rendered claim has a visible citation chip. **A claim with no citation does not
   render.**
5. Each citation chip opens a popover with the excerpt (when captured), the verification
   state in plain language, the accession, and a working EDGAR link to the exact document.
6. Brief claims show as **Cited**, not Verified. No check mark appears on any claim whose
   quote we did not check.
6a. A 10-K citation renders as `10-K, filed <date>` with no item slot, placeholder or dash.
6b. When `dropped_citations` is non-empty, the withheld-claim disclosure renders with the
    accession(s) named, styled as information rather than error.
7. Follow-up answers stream token by token and show `verified/checkable` counts.
8. Asking a second question about the same ticker does not refetch documents and reports a
   cache read in its usage line.

**Zero-credit operation** *(must pass in the current account state)*
9. With no API credits, `NVDA` still renders quote, financial trend and filing index in
   full, and the narrative area shows the dedicated no-credits state — not a crash, not a
   raw API error string.
10. With `GI_FIXTURES=1`, a full brief and at least two follow-ups render end to end with no
    network call to Anthropic, and the environment strip shows the **Replay** badge.
11. Replay reproduces the recorded elapsed time, so the wave-3 wait state is exercised.

**Failure isolation**
12. With the quote endpoint forced to 429, the filing and financial blocks render normally
    and the quote card degrades in place.
13. With `SEC_USER_AGENT` set to a rejected value, the environment strip shows red with the
    exact remedy before the user types.
14. Killing the SSE connection mid-wave-3 leaves rendered blocks intact and offers Retry.

**Edge cases**
15. `SPY` renders the quote and an ETF notice that does not read as an error and does not
    mention IPOs.
16. `BABA` renders with a `20-F` badge.
17. `JPM` completes without a client timeout and shows its token estimate during the wait.
18. A claim citing a tier-3 8-K renders with the **Indexed only** chip and is never dropped.

**Accessibility and print**
19. Keyboard-only: reach the composer, submit, open a citation, follow the EDGAR link,
    return.
20. Printing produces numbered footnotes and a Sources list with full URLs; no interactive
    chrome appears.
21. Axe reports zero critical violations.

---

## 15. Non-goals

Out of scope. Do not build these.

- Any framework or build step — no React/Vue/Svelte, no bundler, no npm dependency tree.
- Authentication, accounts, multi-user, persistence beyond the browser session.
- Watchlists, portfolios, alerts, comparison views.
- Price charts or sparklines. The design doc has none; the quote card's five numbers are
  the brief's orientation, not a trading screen.
- Mobile-first design. Desktop advisor at a desk. Must not *break* below 900px — the rail
  collapses — but it is not the target.
- Internationalisation, theming, dark mode.
- Any edit to `globalinsight/` or `tests/`.
- **Recommendations, price targets, or buy/sell calls.** The tool informs; the advisor
  decides. This is deliberate and it is the answer that makes compliance comfortable — the
  UI must never imply otherwise.

---

## 16. Cut order

If time runs short, cut in this order:

1. **Copy-to-Markdown export** — print covers the need.
2. **Typeahead** — demo tickers and exact symbols cover the demo path.
3. **Session rail** — a single brief at a time still demos.
4. **`business_line` section** — the trend table already implies the shape of the business.
5. **8-K coverage** — the *what's changed* differentiator; painful, but the 10-K brief stands.

The backend has already trimmed the brief to four sections; there is little left to cut
above the line. Cut UI affordances before cutting content.

**Never cut:**
- Citations, or any part of the verification-state display.
- Per-block provenance.
- The wave 2 / wave 3 split. Collapsing them destroys the core product claim.
- Replay mode. Without it there is no demo.

---

## 17. Open backend requests

Not blockers for this build, but each is worth more than any UI change. Raise with the
backend owner:

1. **Add `quote` to the brief citation schema.** Promotes every brief claim from *Cited* to
   *Verified*. Highest-value change available to this product.
2. **Make `generate_brief` streaming.** Turns a 57s elapsed clock into progressively
   rendering content.
3. **Upstream `api/qa.py` into `globalinsight.synthesize.answer_question()`.** It is backend
   logic sitting in our layer only because no entry point exists.
4. **Cache the synthesis permanently, keyed by accession.** A filed 10-K is immutable, so
   its brief never needs regenerating. Today every brief pays a full ~173k-token read and
   re-asking NVDA pays again. `TTL_PERMANENT` already exists in `config.py` for exactly
   this. It is the design doc's scaling argument — cost tracks *filings per year*, not
   *briefs per year* — and it is currently not implemented.
5. **Expose a public company-name index.** The UI needs titles for name matching and
   currently reaches into `edgar._raw_ticker_rows()`.
6. **Add share count** (`dei:EntityCommonStockSharesOutstanding`) to the financials dict, so
   market cap can be derived from XBRL the way P/E already is, instead of showing `—`.
