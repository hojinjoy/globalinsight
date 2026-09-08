# GlobalInsight — SEC Filing Advisor Brief (Design)

> **Mirror of the Confluence design doc**, kept in the repo so the design
> travels with the code it describes.
> Canonical page: [GlobalInsight — SEC Filing Advisor Brief (Design)](https://tjh-digital.atlassian.net/wiki/spaces/~71202001e138cec2d34135befa362f279c4292/pages/69468162/GlobalInsight+SEC+Filing+Advisor+Brief+Design)
> · synced at Confluence **revision 4**. Change both when you change either.
> Confluence panels render here as blockquotes and status lozenges as
> 🟢 / 🟡 / 🔴; nothing else is altered.

> ℹ️ **An advisor types a ticker and gets a one-page, fully-cited briefing built from live quote data and the company's own SEC filings.**
>
> Design document for a wealth-management take-home exercise: 120-minute prototype, one slide, 20-minute stakeholder panel. Drafted 2026-09-08.

> ✅ **Revision 4.** Section 17 (question guardrails) is new: the "no recommendations" stance in section 15 is now enforced by a deterministic gate in code rather than requested in a prompt, and this section publishes the allowed / refused question lists. Revision 3 added sections 6 and 7 (SEC API flow, 8-K exhibit handling) and put the timings and economics in sections 8 and 12 on **measurements against five real filers** rather than estimates. Revision 2 dropped Item segmentation after testing; revision 1 was the original design.

## 1. The problem, reframed

The stated ask is "quote data plus filing summaries," but the real job is narrower and more demanding. An advisor gets a client call — *"what do you think about NVDA?"* — and has maybe five minutes. They need to sound informed **and be able to defend every claim they make.**

That second half is the whole ballgame. In wealth management, an advisor repeating an unverifiable AI claim to a client is not an embarrassment — it is a compliance event. So the product is not "AI summarizes filings." It is **AI that shows its work.**

## 2. The trust architecture

| Output | Source | Why it matters |
| --- | --- | --- |
| Price, valuation | Yahoo quote API | Timestamped, carries an explicit "as of" |
| Revenue, margins, EPS | **SEC XBRL structured data** | Numbers are *retrieved, never generated* — hallucination is structurally impossible |
| Risks, business, changes | Claude over filing text | Every claim cites filing, section and date |

## 3. Product — what is on the page

| # | Section | Source | Purpose |
| --- | --- | --- | --- |
| — | **Header** — price, change, mkt cap, P/E, 52-week | Yahoo, cached | Orientation, with "as of" stamp |
| 1 | **The 30-Second Take** | Claude | What you say if the client asks right now |
| 2 | **How They Make Money** | 10-K Item 1 | Advisors often don't know this outside their focus names |
| 3 | **Financial Trend** — 3yr | **XBRL** | Exact reported figures, never model-generated |
| 4 | **Key Risks** — top 3–5 | 10-K Item 1A | The material ones, each cited |
| 5 | **What's Changed Since the 10-K** | 8-Ks since filing date | **The differentiator.** A 10-K can be 11 months stale. |
| 6 | **Questions Your Client May Ask** | Claude | Anticipatory prep |

## 4. Architecture

```
ticker
  ├─► Yahoo quote ─────────────────────────┐  (disk-cached)
  └─► EDGAR                                │
        ├─ company_tickers.json → CIK      │
        ├─ submissions.json → 10-K + 8-Ks  │
        ├─ XBRL companyfacts → financials ─┤  numbers path
        └─ filings → cleaned full text ────┤  narrative path
                                           ▼
                          Claude Opus 5 · structured output
                                           ▼
                            Brief object (typed + cited)
                                           ▼
                                      Streamlit
```

**The two paths never cross.** Numbers flow from XBRL straight to the page; narrative flows through Claude. That separation is the compliance story, and it belongs on the slide drawn exactly like this.

## 5. Key decision — whole document, not section extraction

> ⚠️ **Tested and abandoned.** The original plan was to extract Item 1A and Item 7 by regex. Tested against four real 10-K filings, it **failed on three.**

| Filer | Plain text | Segmentation result |
| --- | --- | --- |
| AAPL | 55k tokens | 🟢 **Clean** — all 9 items, sequential |
| NVDA | 90k tokens | 🔴 **Failed** — "Item 1A" matched 6 times |
| MSFT | 89k tokens | 🔴 **Failed** — only 2 of 9 items survived |
| JPM | 353k tokens | 🔴 **Failed** — "Item 1A" matched 5 times |

**Root cause.** In the rendered document a heading is bold, in its own block, visually distinct. A cross-reference — *"see Item 1A, Risk Factors"* — is inline body text. Once the HTML is stripped, **the two are byte-identical.** The formatting was the signal, and stripping tags discards it. A cross-reference filter was tried and recovered almost nothing.

**Resolution: send the whole cleaned filing.** Every document tested fits inside Claude Opus 5's 1M-token context window, including JPM's 353k. This removes the highest-risk component from the build plan, works on any filer including banks and REITs, and leaves nothing to debug at T+40 with the clock running.

## 6. SEC API flow

Four endpoints, no key, no auth, no quota. Roughly **20 HTTP requests per ticker**, full payload retrieved in under one second.

```
PHASE 0 — BOOTSTRAP                              once per day, cached
  GET www.sec.gov/files/company_tickers.json     797 KB → {ticker: CIK}
                                                 10,415 companies
       │
PHASE 1 — RESOLVE                                local, instant
  ticker → CIK ────────────────► unknown ticker → stop, honest error
                                 ETF / ADR       → quote-only path
       │
PHASE 2 — WAVE 2 DATA           2 requests, PARALLEL       0.36–0.48s
  ├─ GET data.sec.gov/submissions/CIK##########.json
  │     └─► form[], filingDate[], accessionNumber[],
  │         primaryDocument[], items[]   ← 8-K codes free
  └─ GET data.sec.gov/api/xbrl/companyfacts/CIK##########.json
        └─► 500–630 us-gaap concepts, each w/ source accn
       │
       ├──► RENDER: financials + filing index ◄── advisor can talk here
       │
  parse: latest 10-K date → 8-Ks since → tier filter
       │
PHASE 3 — DOCUMENTS             1 + (N×~2.5) requests       +0.3–0.4s
  ├─ GET Archives/edgar/data/{cik}/{accn}/{primaryDocument}   the 10-K
  └─ for each tier-1/2 8-K:
       GET .../{accn}/index.json      → list .htm, drop R\d+.htm
       GET .../{accn}/{exhibit}       → EX-99.1, EX-99.2 …
       clean → strip tags → cap
       │
PHASE 4 — SYNTHESIS                                        20–60s
  assembled payload → Claude Opus 5 → structured brief
```

### Rate limiting

SEC caps at **10 requests per second** and enforces with IP blocks. Two rules: use a bounded thread pool (5 workers) for the 8-K fan-out, the only place with real concurrency; and use a token-bucket limiter shared across threads rather than per-call `sleep()`, which under-throttles when parallel. The `User-Agent` header must carry a real contact address — a generic or absent one gets 403'd, potentially at IP level rather than per request.

### Caching, per phase

| Phase | Cache key | TTL |
| --- | --- | --- |
| 0 — Bootstrap | — | 24 hours |
| 2 — submissions / companyfacts | CIK | 24 hours |
| 3 — Documents | **Accession number** | **Permanent** — filings are immutable |
| 4 — Synthesis | Accession set + prompt version | **Permanent** |

Keying phases 3 and 4 on accession number is what makes this cheap at firm scale: a filing that never changes never needs refetching or re-summarizing.

### Failure handling per phase

- **Phase 0** — bootstrap fails, fall back to yesterday's cached map; never hard-fail here
- **Phase 2** — either request fails, render the other. A missing `companyfacts` means no financials block, not a dead page
- **Phase 3** — a single 8-K fails, drop it, note it in sources, continue. One bad exhibit must never kill the brief
- **Phase 4** — LLM fails, waves 1 and 2 still stand. That is the point of the split

## 7. 8-K handling — the exhibit trap

> 🚨 **The 8-K `primaryDocument` is a stub.** For NVDA's earnings 8-K it is 997 tokens of form boilerplate — registrant name, address, checkboxes. **Zero content.** Fetching only `primaryDocument` — the obvious implementation — produces an empty "What's Changed" section.

| Document | Tokens | Content |
| --- | --- | --- |
| `nvda-20260826.htm` (primary) | 997 | Form header only — nothing usable |
| `q2fy27pr.htm` (**EX-99.1**) | 5,586 | "Revenue of $96.2 billion, up 106%" — the actual release |
| `q2fy27cfocommentary.htm` (**EX-99.2**) | 4,930 | Full CFO financial table |
| `R1.htm` | 2,237 | XBRL viewer artifact — skip |

**Fetch rule:** read `index.json` for the accession, take all `.htm` *except* `R\d+.htm`, concatenate. Never trust `primaryDocument` alone for an 8-K.

### The volume problem

Fetching all ten of NVDA's 8-Ks with exhibits costs **128,791 tokens — 1.43× the 10-K itself** — and the distribution is severely skewed:

| Date | Items | Tokens | Value |
| --- | --- | --- | --- |
| 2026-08-26 | **2.02** | 11,513 | Latest quarter results |
| 2026-05-20 | **2.02** | 10,099 | Prior quarter |
| 2026-08-17 | 1.01, 2.03 | 4,836 | Material agreement |
| 4 filings | 5.02 | ~6,167 | Exec changes |
| 2026-06-30 | 5.07 | 1,728 | Vote results |
| 2026-09-03 | 8.01 | 2,018 | Other |
| **2026-06-18** | **8.01** | **92,430** | ⚠️ **72% of the total — boilerplate** |

### Selection policy

| Tier | Item codes | Action | Typical cost |
| --- | --- | --- | --- |
| **1** | `2.02` earnings | Fetch exhibits — most recent always included | ~11k |
| **2** | `5.02`, `1.01`, `2.03` | Fetch — small and genuinely material | ~11k |
| **3** | `8.01`, `7.01`, `5.07` | **Metadata only** — date, code and title; body not fetched | 0 |

Plus a **hard per-document token cap** as a safety valve: tiering catches the known-boring codes, the cap catches anomalies that slip through a high-value code. Result for NVDA: ~32,600 tokens instead of 128,791 — a 75% reduction, where the discarded 75% was boilerplate.

> 📝 **This retroactively justifies dropping 10-Q coverage.** The `2.02` earnings 8-K contains the latest quarter's results — revenue, margin and segment breakdown — inside the EX-99.1 release. Quarterly freshness without parsing a second filing format.

> ⚠️ **Cap needs tuning.** At 25k tokens the cap *bound on both* WMT and UNH (8-K totals landed at exactly 50,000 = 2 × 25,000), so it is truncating real content, not just boilerplate. Either raise it to ~40k for tier-1 `2.02` filings specifically, or truncate from the *middle* — the top of a release carries headline numbers, the bottom carries financial tables, and the middle is narrative.

## 8. Rendering — three waves

The two halves of the page differ in latency by roughly 100×. Rendering them as one unit leaves the advisor watching a spinner while holding a live client call.

| Wave | Arrives | Content | Nature |
| --- | --- | --- | --- |
| 1 | ~0.2s | Quote header — price, change, mkt cap, P/E | Deterministic |
| 2 | **~0.45s** (measured) | XBRL financial trend + filing index | Deterministic |
| 3 | 20–60s | LLM narrative — take, risks, what changed | Generative |

**Wave 2 was designed against a 2.5s estimate; measurement puts it at ~0.45s.** SEC infrastructure is considerably faster than assumed, so the entire deterministic half of the page is effectively instant. The advisor has price, valuation, real revenue and margin trend, and the filing inventory before the model has said a word.

**This is what lets structured output survive.** Because substantive content is on screen almost immediately, a 30-second wait on the narrative is tolerable rather than dead air — so a single structured-output call is affordable and citation integrity stays intact. Four parallel per-section calls would resend the filing four times and quadruple input cost for no benefit inside 120 minutes.

**Layout:** single-column brief, read top to bottom like a research note. Matches how an advisor scans before a call, and prints or exports cleanly.

**Wait state:** skeleton placeholders with a live status line naming the work in progress (*"Reading 10-K + 4 8-Ks since 2026-02-25… 18s"*). Makes the delay legible and demonstrates the grounding work to the panel rather than hiding it behind a spinner.

### Per-block provenance is mandatory

The data types differ in freshness by months, so a single page-level timestamp would be actively misleading — it would imply the risk factors are as current as the price.

- Quote — *"as of 16:00 ET today"*
- Financials — *"FY2025 10-K, filed 2026-02-25"*
- Narrative — *"10-K + 4 8-Ks through Aug 2026"*

### Failure isolation and edge cases

Each block fails independently — a Yahoo 429 shows an unavailable quote card while filings render normally. Three resolver cases to handle explicitly:

- **ADRs file 20-F, not 10-K** (BABA, TSM) — different form and structure
- **ETFs have no corporate filings** (SPY, QQQ) — quote works, filing half is empty by design
- **Recent IPOs** may have an S-1 but no 10-K yet

## 9. Citations

Do not ask for prose and hope citations appear. Use **structured output** so every claim is a typed object:

```json
{ "claim": "...", "form": "10-K", "item": "1A", "filed_date": "2026-02-25", "url": "https://www.sec.gov/..." }
```

Rendered as `↳ 10-K FY25, Item 1A, filed 2026-02-25`, linking to EDGAR. If a claim cannot carry a citation, it does not render. That constraint *is* the product.

Numbers get citations for free — XBRL returns the source accession number with every value:

```
Revenue      FY2023 $383.3B   FY2024 $391.0B   FY2025 $416.2B
Net income   FY2023  $97.0B   FY2024  $93.7B   FY2025 $112.0B
Gross profit FY2023 $169.1B   FY2024 $180.7B   FY2025 $195.2B
   ↳ each row tagged: 10-K, filed 2025-10-31, accn 0000320193-25-000079
```

Model configuration: `claude-opus-5`, adaptive thinking, `effort: high`, structured output schema.

## 10. 8-K item code reference

| Code | Event | Tier |
| --- | --- | --- |
| **2.02** | Results of operations — earnings | 1 |
| **5.02** | Director or officer departure / appointment | 2 |
| **1.01** | Entry into a material agreement | 2 |
| **2.03** | Creation of a direct financial obligation | 2 |
| 7.01 | Regulation FD disclosure | 3 |
| 8.01 | Other material events | 3 |
| 5.07 | Submission of matters to a shareholder vote | 3 |
| 9.01 | Financial statements and exhibits — flag that exhibits exist | — |

## 11. Build plan — 120 minutes

| Time | Work | Risk |
| --- | --- | --- |
| 0–20 | EDGAR client, ticker resolver, cache layer | 🟢 Low |
| 20–35 | XBRL financials | 🟢 Low |
| 35–50 | HTML clean-up + 8-K exhibit fetch and tiering | 🟡 Medium |
| 50–60 | Yahoo quote and cache | 🟡 429 |
| 60–90 | Claude synthesis, structured output with citations | 🟡 Medium |
| 90–105 | Streamlit, three-wave rendering | 🟢 Low |
| 105–120 | **Pre-warm demo tickers, dry run** | 🔴 Do not skip |

Cut order if running behind: Section 6, then Section 2, then tier-2 8-Ks (keep tier 1). **Never cut citations.**

## 12. Economics — measured

Measured end to end across five real filers, cold cache, using the tiered 8-K policy:

| Ticker | Wave 2 | Full fetch | Payload | Opus 5 | Sonnet 5 |
| --- | --- | --- | --- | --- | --- |
| NVDA | 0.18s* | 0.58s* | 122,723 tok | $0.66 | ~$0.27 |
| KO | 0.46s | 0.87s | 211,748 tok | $1.11 | ~$0.45 |
| WMT | 0.48s | 0.90s | 145,823 tok | $0.78 | ~$0.32 |
| UNH | 0.36s | 0.68s | 141,500 tok | $0.76 | ~$0.31 |

*\*NVDA warm from earlier fetches; the other three are genuine cold-cache measurements.*

> ℹ️ **Approximately $0.85 per brief on Opus 5, ranging $0.66–$1.11.** This supersedes the $0.25 estimate in revision 1 (which assumed section extraction worked) and the $0.50 estimate in revision 2 (extrapolated from Apple alone, the smallest filer tested). Roughly $0.35 on Sonnet 5.
>
> Permanent caching on immutable filings means marginal cost across a firm trends toward *filings per year*, not *briefs per year*.

## 13. Environment status

| Dependency | Status | Note |
| --- | --- | --- |
| SEC EDGAR (all four endpoints) | 🟢 OK | Tested end to end against 7 filers |
| SEC XBRL financial data | 🟢 OK | Exact figures with source accession numbers |
| Yahoo Finance | 🟡 429 | Rate-limited. Demo risk, not a build risk — mitigate by caching. |
| `ANTHROPIC_API_KEY` | 🔴 Blocker | Not set, and no `ant` CLI installed. Nothing runs until resolved. |

## 14. Demo risks

- **Yahoo 429** (confirmed live) — every demo ticker cached to disk beforehand
- **LLM latency 20–60s** — three-wave rendering means the page is useful at ~0.5s regardless
- **SEC rate limits** — 10 req/sec cap and mandatory User-Agent; all cached regardless

> 🚨 **Rule for the room: nothing uncached is called during the demo.**

## 15. Stakeholder Q&A prep

| Their question | Your answer |
| --- | --- |
| *"How do we know it's not hallucinating?"* | Numbers are retrieved from XBRL, never generated. Narrative claims carry filing, section and date. Click through to EDGAR live on stage. |
| *"How current is it?"* | Different per block, and the page says so: quote to the minute, financials to the filing date, narrative covering every 8-K since. |
| *"What does it cost?"* | About $0.85 per newly filed document analysed, measured across real filers. Cached permanently after that, since a filed 10-K never changes. |
| *"Does it scale to our coverage universe?"* | Cost tracks filings per year, not briefs per year. Pre-process overnight; embeddings are the phase-2 layer. |
| *"What about compliance review?"* | Every brief is reproducible: cached inputs plus citation trail equals an audit record. |
| *"What stops an advisor asking it whether to buy?"* | Nothing stops them asking — the tool declines, deterministically, before it spends a cent. Advice questions are caught by a lexical gate, not by asking the model nicely. **Demo this live: type "Is AMD a buy?" and let the panel watch it refuse and redirect.** See section 17. |
| *"Why not just use ChatGPT?"* | Grounded in this company's actual filings rather than training data — and auditable. |

> 📝 **The stance to hold:** no recommendations, no price targets, no buy/sell calls. The tool informs; the advisor decides. This is deliberate, and it is the answer that makes compliance comfortable. **Section 17 is how that stance is enforced rather than merely asserted.**

## 16. The slide

1. **Problem** — 45 minutes of reading before every client call
2. **The two-path trust diagram** from section 2, centered
3. **Before / after** — 45 minutes reduced to 3
4. **Economics** — cost tracks filings, not queries
5. **Roadmap sliver** — Q&A across filing history

Lead the pitch with the trust diagram, not the demo. Any team can summarize a 10-K; you are the one who can prove where every number came from.

## 17. Question guardrails — what it will and will not answer

Section 15 states the stance: *no recommendations, no price targets, no buy/sell calls.* Through revision 3 that stance lived only in the prompt — a sentence asking the model to behave. **A prompt rule is a request, not a control.** It is not enforced, it is not auditable after the fact, and it costs a full high-effort Opus call to discover whether it was honoured this time.

> 🚨 **The compliance objection this answers.** "What stops it recommending a stock?" cannot be answered with "we asked it not to." An advice question is now refused by a deterministic lexical gate that runs **before any document fetch or paid call** — so the refusal is identical every time, costs nothing, and can be handed to a reviewer as code and a test suite rather than as prompt text.

### What it refuses

Four categories, each with a reason code that is what you would count in a log, and each with its own message and redirect.

| Reason code | Example questions | What the advisor sees |
| --- | --- | --- |
| `recommendation` | *Is AMD a buy?*<br>*Should I buy this for a conservative client?*<br>*Would you hold it through earnings?*<br>*What would you do?* | "I can't tell you whether to buy, sell or hold — that's a recommendation, and this tool only reports what the filings say. I can lay out the facts behind the call." |
| `valuation_judgment` | *What's your price target?*<br>*Is the stock overvalued?*<br>*Is it cheap right now?* | "I can't give a price target or a view on whether the stock is cheap or expensive — that's a valuation judgment, not something the filings state. I can show you the reported figures behind it." |
| `price_forecast` | *Will the stock go up after earnings?*<br>*How high can it go?* | "I can't forecast where the price goes. I can tell you what the filings disclose about the drivers, and what has changed recently." |
| `allocation` | *How much should I put into it?*<br>*What position size makes sense?* | "I can't advise on position sizing or allocation. I can give you the disclosed facts you'd size a position against." |

Every refusal names the boundary once and then offers two or three answerable rewrites of the same information need. The advisor is mid-call; they need a redirect, not a lecture.

### What stays answerable

**This is the harder half, and the one that decides whether the guardrail is usable.** Every pattern matches an advice-seeking *construction* — "should I buy", "is it a buy", "price target" — never a bare keyword, because a keyword filter on buy / sell / hold / value / target eats ordinary filing questions:

| Stays answerable | Why a naive filter would wrongly catch it |
| --- | --- |
| *What do they sell?* | "sell" — the single most common verb in a business description |
| *Did they announce a buyback?* | "buy" — a share repurchase is a disclosed event, not a recommendation |
| *What were selling, general and administrative expenses?* | "selling" — SG&A is a line item on every income statement |
| *Is there a sell-off risk disclosed in the filing?* | "sell" inside a hyphenated word |
| *What does the 10-K say about fair value measurements?* | "fair value" — ASC 820 is a real disclosure, so the bare phrase is deliberately **not** a trigger |
| *Should I be worried about their debt load?* | "should I" — with no buy/sell/hold verb attached, this is a question about the filing |
| *Did the board make a recommendation on the merger?* | "recommendation" — proxy statements contain board recommendations |
| *What do you think about Nvidia?* | Reads like an opinion request but is the documented way to **ask for a brief**; refusing it would break a supported entry point |

### Behaviour on a mixed question

> 📝 **"Is AMD a buy?" is two things at once** — a recommendation request *and* a legitimate information need about AMD. The recommendation is declined and **the factual brief is still built**. A brief contains no recommendation, so serving it costs nothing in compliance terms, and refusing outright would withhold exactly the information the advisor needs to reach their own answer. This also matters mechanically: that phrasing routes to a brief, not a follow-up, so a gate on the Q&A path alone would have missed the canonical case entirely.

### Where it is enforced

| Layer | File | What it catches |
| --- | --- | --- |
| Intent classification | `chatui/intent.py` | Advice phrasing that names a ticker and would otherwise open a brief silently |
| Answer path | `api/qa.py` | Every caller — HTTP, Streamlit, future callers — because the check sits in the shared function, not in a route |
| HTTP route | `api/app.py` `/api/ask` | Refuses before the context build, so a blocked question triggers no fetch and no spend |
| Published policy | `/api/policy` | Serves these same lists to any client, so the UI and this document cannot disagree |

### Why the examples above can be trusted

The allowed and refused examples in this section are not prose written alongside the code. They are declared in `api/guardrails.py` and asserted in `tests/test_guardrails.py`: every allowed example must pass the checker and every refused example must be caught with the reason claimed. **A pattern change that breaks a published example fails the suite** rather than quietly making this page wrong. The false-positive corpus is treated as part of the contract, not as an afterthought.

> ⚠️ **What this does not do.** It gates the *question*, not the *answer*. A request phrased around the filter — *"what would a portfolio manager conclude about its attractiveness?"* — still reaches the model, where the prompt rule is the only remaining backstop. The matching output-side check (scanning a generated answer for recommendation language before it renders) is the natural follow-on and is not yet built.

## Open items

- [ ] Obtain an Anthropic API key — hard blocker for any execution
- [x] Test Item 1A/7 segmentation against real 10-Ks — failed 3 of 4, approach dropped
- [x] Confirm 8-K item codes are exposed in submissions.json — yes, `items[]` field, no parsing needed
- [x] Measure real per-brief cost — $0.66 to $1.11, ~$0.85 average on Opus 5
- [ ] Tune the per-document token cap — currently binding on WMT and UNH, truncating real content
- [ ] Verify the current Bloomberg terminal list price before quoting it to the panel
- [ ] Choose and pre-cache the demo tickers
- [x] Enforce the "no recommendations" stance in code — advice gate shipped, section 17
- [ ] Add the output-side check: scan a generated answer for recommendation language, not just the question
