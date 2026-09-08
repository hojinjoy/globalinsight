# GlobalInsight — SEC Filing Advisor Brief

An advisor types a ticker and gets a one-page, fully-cited briefing built from
live quote data and the company's own SEC filings — then keeps asking questions
about those filings in the same chat.

Implements the [design doc](https://tjh-digital.atlassian.net/wiki/spaces/~71202001e138cec2d34135befa362f279c4292/pages/69468162/GlobalInsight+SEC+Filing+Advisor+Brief+Design).

## Run

```bash
cp .env.example .env      # set SEC_USER_AGENT; ANTHROPIC_API_KEY if not already set
uv sync
uv run streamlit run app.py
```

Type `NVDA`, `$AAPL`, or *what do you think about Nvidia?* — then follow up with
anything about the filings.

The backend also has a CLI:

```bash
uv run python -m globalinsight NVDA --synthesize
```

## The trust architecture

| Output | Source | Why it matters |
| --- | --- | --- |
| Price, 52-week range | Yahoo quote API | Timestamped, carries an explicit "as of" |
| Revenue, gross profit, net income, EPS | SEC XBRL structured data | Retrieved, never generated — hallucination is structurally impossible |
| Risks, business, what changed | Claude Opus 5 over filing text | Every claim cites form, item, date and accession |

The two paths never cross. The numbers reach the page without passing through
the model.

## Layout

Two packages, deliberately separate:

```
globalinsight/          backend — retrieval, XBRL, document fetch, synthesis
  config.py             SEC User-Agent, rate limit, cache TTLs, 8-K tier caps
  http.py               shared token bucket (SEC's 10 req/s is global, not per-connection)
  cache.py  clean.py    disk cache; HTML -> text
  edgar.py              ticker -> CIK -> submissions -> filing refs
  financials.py         companyfacts -> 3-year series, each point carrying its citation
  filings.py            10-K text + tiered 8-K fetch
  quote.py              Yahoo quote, fails closed
  models.py             Company, Quote, Citation, FinancialSeries, FilingRef, Brief
  pipeline.py           wave2 / wave3 / build_brief
  synthesize.py         the one structured-output call
  __main__.py           CLI

app.py                  Streamlit chat: intent -> waves -> replay from state
chatui/                 UI layer
  intent.py             "what do you think about Nvidia?" -> NVDA
  render.py             quote header, XBRL table, filing index, cited brief
  qa.py                 follow-up Q&A (see note below)
scripts/prewarm.py      pre-cache demo tickers
```

`chatui` reads the backend's public API and adds nothing to it. The exception is
`chatui/qa.py`: the backend exposes `generate_brief` and no question-answering
entry point, so follow-ups are implemented there for now. It has no Streamlit
dependency and should move into `globalinsight/synthesize.py`.

## Waves

| Wave | Arrives | Content |
| --- | --- | --- |
| 2 | ~0.9s cold | Quote header, XBRL trend, 8-K index with item codes |
| 3 | 20–60s | Narrative — the take, business, risks, what changed, client questions |

Each block fails independently: a Yahoo 429 costs the header card and nothing
else. Measured on AAPL: wave2 0.88s cold, 10-K + six 8-K bodies ~101k tokens.

Follow-up questions reuse the filing through a 1-hour prompt cache — measured
$0.71 on the first question (cache write), **$0.11 and 23s** on each one after.

## Before a demo

Nothing uncached should be called on stage. Yahoo rate-limits aggressively:

```bash
uv run python -m scripts.prewarm NVDA AAPL MSFT JPM
uv run python -m scripts.prewarm --no-narrative SPY    # free half only
```

## Edge cases handled

- **ADRs file 20-F** (BABA), Canadian issuers 40-F — picked up alongside 10-K
- **ETFs have no SEC filings** (SPY, QQQ) — quote renders, filing half empty by design
- **Recent IPOs** may have an S-1 but no annual report — said plainly, not left blank
- **Whole filings, not extracted items** — item segmentation failed on 3 of 4 test
  filings once HTML formatting was stripped, so the full cleaned document goes in

## Design docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — the design document: trust architecture,
  SEC API flow, 8-K tiering policy, measured economics, and the question
  guardrails (section 17). Mirrors the Confluence page; keep both in sync.
- [`docs/UI-SPEC.md`](docs/UI-SPEC.md) — front-end specification.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Azure Container Apps deployment
  plan: what has to change before the first container build, why it runs at a
  single replica today, and the three phases out.
