# GlobalInsight — Azure deployment plan

**Status:** proposed · **Target:** Azure Container Apps · **Drafted** 2026-09-08

One image, one always-on web container and two scheduled jobs. Written against what is in
this repository today — including three things that will stop the first build, and one
reason the service cannot be scaled horizontally yet.

| | |
| --- | --- |
| **Platform** | Azure Container Apps — managed, SSE-friendly, native cron jobs, and scale-to-zero we deliberately will not use |
| **Shape** | One web container pinned at a single replica, two jobs, one Azure Files share, one registry |
| **Reality check** | Phase 0 is about half a day and gives a demoable URL. Horizontal scale is Phase 2 and needs code changes. |

---

## 1. Three things that will stop the first build

All three are in the repo now. None is hard to fix, but each fails in a way that wastes an
afternoon if you meet it inside a container build rather than here.

### 1.1 The web server is an accident of the prototype

`api/app.py` imports Starlette and is served by uvicorn, but **neither is declared in
`pyproject.toml`**. Both are present in the venv only because `streamlit` pulls them in
transitively — `uv.lock` lists `starlette` and `uvicorn` inside streamlit's dependency block
and nowhere else.

This bites precisely when you do the obvious thing. Streamlit and pandas are used only by
the superseded prototype (`app.py`, `chatui/render.py`), so the natural lean production image
drops them — **and the web server disappears with them.**

> ✅ **Fix.** Add `starlette` and `uvicorn[standard]` to `[project.dependencies]`, and move
> `streamlit` and `pandas` into an optional `prototype` group. The production image then
> installs four runtime packages instead of a scientific stack.

### 1.2 There is no `.dockerignore`, and the build context is dangerous

A naive `COPY . .` ships **85 MB of `.cache/`** — filing text and XBRL data that is state,
not code — plus `.venv/`, `.git/`, `.coverage`, and, if it exists on the build machine,
**`.env` carrying a live `ANTHROPIC_API_KEY`** straight into a registry layer where deleting
it later does not remove it.

> ✅ **Fix.** Write `.dockerignore` *before* the Dockerfile: `.env`, `.cache/`, `.venv/`,
> `.git/`, `__pycache__/`, `.pytest_cache/`, `.coverage`, `_deck/`. Treat the cache as a
> mounted volume, never as image content.

### 1.3 The default SEC User-Agent is rejected by design

`config.py` defaults `SEC_USER_AGENT` to `your-email@example.com`. Per section 6 of the
design doc, EDGAR **403s any User-Agent containing `example.com`**, sometimes at the IP
level. A container that starts without this set looks healthy and fails on the first filing
fetch.

> ✅ **Fix.** Set it per environment and treat it as required config, not a default. The
> environment strip already surfaces a rejected UA before the user types, so the failure is
> legible once the value is wrong rather than missing.

---

## 2. Target architecture

```plaintext
GitHub Actions ──build on merge──► Container Registry
                                          │  one image, two entrypoints
                                          ▼
        ┌──────────────── CONTAINER APPS ENVIRONMENT ─────────────────┐
advisor │                                                             │
browser─┼──HTTPS + SSE──► globalinsight-web    ingress :8520, 1 replica│
        │                        │                                    │
        │                 refresh-tickers      job · daily 06:00       │
        │                        │                                    │
        │                 prewarm              job · after each deploy │
        │                        │                                    │
        │                        └──► /data   Azure Files share        │
        │                                     cache + tickers.db       │
        └────────────────────────────┬────────────────────────────────┘
                                     │ all outbound
                                     ▼
                       NAT gateway — static egress IP        (Phase 2)
                                     │
             ┌───────────────────────┼───────────────────────┐
             ▼                       ▼                       ▼
        SEC EDGAR              Yahoo quotes            Anthropic API

Key Vault ──secret refs──► ANTHROPIC_API_KEY, SEC_USER_AGENT
```

**One image, two entrypoints.** The same container runs the web app
(`uvicorn api.app:app`) and both jobs (`python -m scripts.refresh_tickers`, `python -m
scripts.prewarm`) — so the code that warms the cache is provably the code that reads it. All
three mount the same share at `/data`, which is where `GI_CACHE_DIR` points.

### Why not the alternatives

| Option | Reason against |
| --- | --- |
| App Service for Containers | Fine for the web app, but no first-class scheduled jobs — the daily ticker refresh becomes a second service or a WebJob, and the prewarm has nowhere natural to live |
| AKS | Correct at ten services. At one web app and two cron jobs it buys you a cluster to patch and nothing else |
| Azure Functions | Wave 3 runs 45–90s behind a held-open SSE connection. That is the opposite of the execution model |

---

## 3. This cannot run more than one replica yet

Not a capacity guess — three independent things in the code break under a second replica.
All three are fixable, none is fixed today, and pinning to one replica is the right Phase 0
answer rather than a compromise.

| What breaks | Why | What it costs you |
| --- | --- | --- |
| **Follow-up questions** | `api/app.py` holds `_CONTEXTS` and `_WAVES` as plain in-process dicts, commented in the code as deliberate single-process demo state | A follow-up routed to a different replica has no context, so it re-fetches every document. **The 11¢ / 23s follow-up becomes a full-price cold read** — the economics in the design doc stop being true |
| **SEC rate limiting** | `globalinsight/http.py` uses a module-level token bucket. SEC's 10 req/s limit is enforced **per IP, globally**, with IP blocks | N replicas behind one egress IP means N × 10 req/s. **The failure mode is an IP-level ban**, which takes the whole deployment down and is not something you can retry your way out of |
| **The ticker database** | `store.py` puts SQLite at `CACHE_DIR/tickers.db`, on the shared volume. Web writes to it too — a lookup miss triggers `maybe_refresh()` | Concurrent SQLite writers over an SMB file share produce locking errors, and they arrive as intermittent 500s rather than a clean failure |

> 🚨 **So: set `minReplicas: 1` and `maxReplicas: 1`, and turn scale-to-zero off.** Zero is
> tempting on a demo budget, but a cold start also means an empty context map — the first
> advisor of the morning pays a full document fetch, and the "advisor is talking in one
> second" property is the first thing to break.

One replica is genuinely enough for a pilot: the work is I/O-bound, the expensive step is a
single upstream API call, and permanent caching means a repeated company costs almost
nothing. **Name the ceiling out loud** rather than discovering it under load.

---

## 4. Configuration

| Setting | Value | Lives in | Note |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Key Vault → secret ref | Never an image layer, never an env var in source control |
| `SEC_USER_AGENT` | real contact address | App config | Required, not defaulted — see §1.3 |
| `GI_CACHE_DIR` | `/data/cache` | App config | Points at the mounted share. The one line that makes the container stateless |
| `GI_FIXTURES` | `1` in demo, unset in pilot | App config | Replay mode. The demo environment runs on this and spends nothing |
| `GI_FIXTURE_SPEED` | unset (`1.0`) | App config | Replay must reproduce real timing, or the wait state never gets exercised |
| `PORT` | `8520` | App config | `run.sh` already honours it; ingress targets the same |

**Two environments, one image.** `gi-demo` runs with `GI_FIXTURES=1` and no Anthropic key at
all — safe to hand to a stakeholder, costs nothing per click, cannot leak spend. `gi-pilot`
runs live with the key. Same image tag in both, which is what makes the demo evidence rather
than theatre.

---

## 5. Storage, jobs, health

| Concern | Decision |
| --- | --- |
| **Persistent state** | One Azure Files share mounted at `/data` on the web app and both jobs. Holds the document cache (85 MB today, grows with filings read) and `tickers.db`. Filings are immutable, so this is an append-mostly store with no invalidation problem — the permanent-TTL decision in `config.py` is what makes a plain file share sufficient |
| **Seeding the cache** | Do not copy the developer's `.cache/` up. Run the `prewarm` job after each deploy against the demo tickers — reproducible, and it exercises the same code path the web app uses |
| **Ticker refresh** | Container Apps Job on `0 6 * * *` running `scripts.refresh_tickers`. Idempotent, and it already refuses to run if SEC returns under ~90% of active tickers, so a truncated upstream response cannot empty the table |
| **Health probes** | `/api/health` as both readiness and liveness. The UI spec requires it never to fail, which is exactly the property a probe needs. It also reports `mode`, so a probe response tells you whether you are looking at replay or live |
| **Ingress and SSE** | The brief stream holds a connection 45–90s. The app already emits 1s heartbeats, which keeps it clear of idle timeouts. **Set no client or ingress timeout below 180s** — JPM is the worst measured case at ~90s and you want headroom |
| **What to alert on** | SEC 403 (User-Agent rejected or IP blocked) · Yahoo 429 rate · synthesis `no_credits` · file share approaching quota. **Not CPU** — this service is never CPU-bound |

---

## 6. Phases

### Phase 0 — a URL you can demo · ~half a day

- Clear the three blockers in §1 — declare the deps, write `.dockerignore`, make the UA required
- Multi-stage `Dockerfile`: `uv sync --frozen --no-dev` into a slim Python 3.12 base,
  non-root user, no build toolchain in the final layer
- Build and run locally first. `GI_FIXTURES=1`, hit `/api/health`, load a brief
- Push to ACR; one Container App, external ingress, 1 replica, replay mode

**Unlocks:** a link you can send a stakeholder, spending nothing.

### Phase 1 — something you would let a real advisor use · 1–2 days

- Azure Files share mounted, `GI_CACHE_DIR=/data/cache`
- Key Vault secret refs; the live `gi-pilot` environment alongside `gi-demo`
- Both jobs: `refresh-tickers` on cron, `prewarm` post-deploy
- GitHub Actions: test → build → push → revision update. The 333-test suite runs in a
  second, so gating the build on it is free
- Custom domain, TLS, health probes, Log Analytics, the four alerts above

**Unlocks:** live briefs, real spend, an audit trail.

### Phase 2 — more than one replica · only when needed

- **NAT gateway with a static egress IP** — do this first regardless. It gives SEC a stable
  identity to associate with your User-Agent, and gives you something to point at if you are
  ever blocked
- Move `_CONTEXTS`/`_WAVES` to Azure Cache for Redis
- Replace the in-process token bucket with a Redis-backed one, so 10 req/s is enforced
  across replicas rather than per replica
- Move the ticker store to PostgreSQL — or make the web path read-only and let only the job
  write, which is a much smaller change
- Then, and only then, raise `maxReplicas`

**Unlocks:** horizontal scale without an IP ban.

---

## 7. Cost

Two separate bills, and the interesting one is not Azure.

| Line | Driver |
| --- | --- |
| Container Apps | One always-on replica at roughly 0.5 vCPU / 1 GiB, billed on active vCPU-seconds and memory. Small, and flat — it does not move with usage |
| Container Registry | Basic tier is sufficient for one image |
| Azure Files | Sized in gigabytes, not hundreds. The cache is 85 MB today |
| Log Analytics | Ingestion-based; the usual surprise. Cap retention at 30 days for a pilot |
| **Anthropic API** | **~85¢ per company report read, once.** Filings are immutable and cached permanently, so this tracks **filings per year, not questions per year** — the design doc's scaling argument. Follow-ups are ~11¢ each |

> ⚠️ **No dollar figures against the Azure lines.** They depend on region, tier and
> commitment, and a made-up number in a deployment plan is worse than none — run the four
> SKUs above through the Azure pricing calculator for the target region before this goes to
> anyone who will hold you to it.

The shape is defensible without exact figures: **Azure cost is flat and small; model cost
tracks how many companies you cover, not how hard your advisors use it.**

---

## 8. Risks

| Risk | Severity | Mitigation |
| --- | --- | --- |
| SEC IP block | **High** | Single replica keeps the shared bucket honest. Static egress IP in Phase 2 so the block is diagnosable. Real contact address in the User-Agent, always |
| Secret leaked into an image layer | **High** | `.dockerignore` before the Dockerfile; Key Vault refs only; no `.env` in any build context |
| Yahoo 429 on quotes | Medium | Already fails closed to `available: false` and degrades one card. Quote TTL is 60s. Nothing to do at the infrastructure layer |
| SQLite locking on the share | Medium | One replica now; web-read-only plus job-writes in Phase 2 |
| Anthropic account has no credits | Medium | Already handled as a distinct UI state, and the demo environment runs on replay fixtures regardless |
| Cache share fills | Low | Alert at 70%. Growth is bounded by filings read, not by traffic |

---

Nothing here needs an architecture change to reach a demoable URL — Phase 0 is three small
repo fixes and a Dockerfile. Everything after that is about making the single-replica ceiling
a deliberate choice instead of an undiscovered one.
