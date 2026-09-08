"""GlobalInsight — SEC Filing Advisor Brief, as a chat.

An advisor types a ticker and gets a one-page, fully-cited briefing built from
live quote data and the company's own SEC filings — then keeps asking questions
about those filings in the same thread.

This file is UI only. All retrieval and synthesis lives in the `globalinsight`
backend package; `chatui` holds the Streamlit-facing pieces.
"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import streamlit as st

from api import guardrails
from chatui import intent as intent_mod, qa, render
from globalinsight import config, pipeline, synthesize

DEMO_TICKERS = ["NVDA", "AAPL", "MSFT", "JPM"]

st.set_page_config(page_title="GlobalInsight", page_icon="📄", layout="centered")


def init_state() -> None:
    st.session_state.setdefault("turns", [])
    st.session_state.setdefault("pending", None)
    st.session_state.setdefault("active", None)
    st.session_state.setdefault("contexts", {})  # ticker -> SynthesisContext
    st.session_state.setdefault("threads", {})   # ticker -> prior Q&A messages


@st.cache_resource(show_spinner=False)
def credentials_available() -> bool:
    try:
        import anthropic

        anthropic.Anthropic()
        return True
    except Exception:
        return False


def submit(prompt: str) -> None:
    st.session_state.turns.append({"role": "user", "text": prompt})
    st.session_state.pending = prompt


def run_with_timer(function, label: str, status) -> object:
    """Run a blocking call while ticking an elapsed counter in the status line.

    The backend's generate_brief is not streaming, so there is nothing to read
    progress from - an elapsed clock is the honest substitute for a spinner.
    """
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(function)
        while True:
            try:
                return future.result(timeout=0.4)
            except FutureTimeout:
                status.update(label=f"{label} {time.monotonic() - started:.0f}s")


# --- sidebar -----------------------------------------------------------------

def sidebar() -> None:
    with st.sidebar:
        st.subheader("Environment")
        if credentials_available():
            st.success("Claude credentials found", icon="✅")
        else:
            st.error(
                "No Claude credentials. Quote, financials and the filing index "
                "still work; the narrative and follow-ups will not.",
                icon="🚫",
            )
        st.caption(f"SEC User-Agent: `{config.SEC_USER_AGENT}`")

        st.subheader("Demo tickers")
        st.caption("Pre-cache before a demo: `uv run python -m scripts.prewarm`")
        columns = st.columns(2)
        for index, ticker in enumerate(DEMO_TICKERS):
            if columns[index % 2].button(ticker, width="stretch"):
                submit(ticker)
                st.rerun()

        if st.button("Clear conversation", width="stretch"):
            for key in ("turns", "active", "threads"):
                st.session_state.pop(key, None)
            st.rerun()

        published = guardrails.policy()
        st.subheader("What I answer")
        with st.expander("Answerable", expanded=False):
            for question in published["allowed"]:
                st.markdown(f"- {question}")
        with st.expander("Declined", expanded=False):
            for entry in published["refused"]:
                st.markdown(f"**{entry['label']}**")
                for question in entry["examples"]:
                    st.markdown(f"- *{question}*")
        st.caption(published["stance"])


# --- replay ------------------------------------------------------------------

def replay(turn: dict) -> None:
    """Re-render a finished turn from state. No network, no model calls."""
    with st.chat_message(turn["role"]):
        kind = turn.get("kind")
        if turn["role"] == "user":
            st.markdown(turn["text"])
        elif kind == "brief":
            st.markdown(f"### {turn['title']}")
            wave2 = turn["wave2"]
            render.quote_header(wave2.quote, wave2.financials)
            if wave2.is_sec_filer:
                st.divider()
                render.financial_trend(
                    wave2.financials,
                    wave2.annual_filing,
                    wave2.errors.get("financials", ""),
                )
                render.filings_index(wave2.annual_filing, wave2.eight_ks)
            if turn.get("brief"):
                st.divider()
                render.narrative(turn["brief"])
                render.provenance(
                    wave2.annual_filing, wave2.eight_ks, turn["seconds"]
                )
            for note in turn.get("notes", []):
                st.info(note, icon="ℹ️")
        elif kind == "answer":
            render.answer(turn["ans"])
        else:
            st.markdown(turn["text"])


# --- the waves ---------------------------------------------------------------

def run_brief(ticker: str) -> dict:
    turn = {
        "role": "assistant", "kind": "brief", "ticker": ticker,
        "title": ticker, "brief": None, "seconds": 0.0, "notes": [],
    }

    # Wave 2 — quote, XBRL trend and filing index together, ~0.9s cold. The
    # advisor has real, deterministic content before the model has said a word.
    with st.spinner("Fetching quote, XBRL financials and filing index…"):
        wave2 = pipeline.wave2(ticker)
    turn["wave2"] = wave2
    turn["title"] = f"{ticker} — {wave2.company.name}" if wave2.company else ticker

    st.markdown(f"### {turn['title']}")
    render.quote_header(wave2.quote, wave2.financials)

    if not wave2.is_sec_filer:
        note = (
            f"`{ticker}` is not an SEC-registered filer. ETFs and funds have no "
            "corporate filings, so the filing half of this brief is empty by design."
        )
        turn["notes"].append(note)
        st.info(note, icon="ℹ️")
        return turn

    st.divider()
    render.financial_trend(
        wave2.financials, wave2.annual_filing, wave2.errors.get("financials", "")
    )
    render.filings_index(wave2.annual_filing, wave2.eight_ks)

    if wave2.annual_filing is None:
        # An S-1 on file points at a fresh IPO; nothing at all is the ETF/fund
        # case, where a CIK exists but no annual report is ever filed.
        if wave2.other_filings:
            forms = ", ".join(sorted({f.form for f in wave2.other_filings}))
            note = (
                f"No annual report on file yet — only {forms}. A recent IPO has "
                "nothing to summarise until its first 10-K."
            )
        else:
            note = (
                "No 10-K, 20-F or 40-F on file. ETFs and funds file no annual "
                "report, so the filing half of this brief is empty by design."
            )
        turn["notes"].append(note)
        st.info(note, icon="ℹ️")
        return turn

    if not credentials_available():
        note = "Narrative skipped: no Claude credentials in this environment."
        turn["notes"].append(note)
        st.warning(note, icon="🚫")
        return turn

    # Wave 3 — fetch documents, then synthesise. Visible work, not a spinner.
    st.divider()
    scope = f"{wave2.annual_filing.form} filed {wave2.annual_filing.filing_date}"
    if wave2.eight_ks:
        scope += f" + {len(wave2.eight_ks)} 8-Ks since"
    with st.status(f"Fetching {scope}…", expanded=False) as status:
        wave3 = pipeline.wave3(wave2)
        if wave3.errors:
            st.caption("Document fetch issues: " + "; ".join(wave3.errors.values()))
        context = pipeline.build_synthesis_context(wave2, wave3)
        if context is None:
            status.update(label="Nothing to synthesise from", state="error")
            note = "No 10-K text could be fetched, so the narrative was skipped."
            turn["notes"].append(note)
            st.warning(note, icon="⚠️")
            return turn
        st.session_state.contexts[ticker] = context

        size = (len(wave3.tenk_text or "") + sum(
            len(t) for t in wave3.eight_k_texts.values()
        )) // 4
        started = time.monotonic()
        try:
            brief = run_with_timer(
                lambda: synthesize.generate_brief(context),
                f"Reading {scope} — ~{size:,} tokens…",
                status,
            )
        except Exception as exc:  # one dead block must never blank the page
            status.update(label="Narrative unavailable", state="error")
            note = f"Narrative synthesis failed: {type(exc).__name__}: {exc}"
            turn["notes"].append(note)
            st.error(note, icon="⚠️")
            return turn
        turn["seconds"] = time.monotonic() - started
        status.update(label=f"Read in {turn['seconds']:.0f}s", state="complete")

    turn["brief"] = brief
    render.narrative(brief)
    render.provenance(wave2.annual_filing, wave2.eight_ks, turn["seconds"])
    return turn


def run_followup(ticker: str, question: str) -> dict:
    context = st.session_state.contexts.get(ticker)
    if context is None:
        with st.spinner("Re-reading the filings…"):
            wave2 = pipeline.wave2(ticker)
            context = pipeline.build_synthesis_context(wave2, pipeline.wave3(wave2))
        if context is None:
            text = f"I have no filing text for {ticker} to answer from."
            st.warning(text, icon="⚠️")
            return {"role": "assistant", "kind": "note", "text": text}
        st.session_state.contexts[ticker] = context

    thread = st.session_state.threads.setdefault(ticker, [])
    with st.status(f"Searching the {ticker} filings…", expanded=False) as status:
        try:
            ans = qa.answer_question(
                context,
                thread,
                question,
                on_progress=lambda elapsed: status.update(
                    label=f"Searching the {ticker} filings… {elapsed:.0f}s"
                ),
            )
        except Exception as exc:
            status.update(label="Answer unavailable", state="error")
            text = f"Could not answer that: {type(exc).__name__}: {exc}"
            st.error(text, icon="⚠️")
            return {"role": "assistant", "kind": "note", "text": text}
        status.update(label=f"Answered in {ans.seconds:.0f}s", state="complete")

    thread.extend(
        [{"role": "user", "content": question},
         {"role": "assistant", "content": ans.text}]
    )
    render.answer(ans)
    return {"role": "assistant", "kind": "answer", "ticker": ticker, "ans": ans}


def _examples_markdown() -> str:
    """The published boundary, built from the guardrail module itself."""
    published = guardrails.policy()
    lines = ["**I can answer:**"]
    lines += [f"- {question}" for question in published["allowed"][:4]]
    lines += ["", "**I won't answer:**"]
    lines += [
        f"- {entry['examples'][0]} — *{entry['label'].lower()}*"
        for entry in published["refused"]
    ]
    return "\n".join(lines)


HELP = (
    "Type a ticker or a company name — `NVDA`, `$AAPL`, *what do you think about "
    "Nvidia?* — and I'll build the brief. After that, ask anything about the "
    "filings and I'll answer from them, with citations.\n\n" + _examples_markdown()
)


def handle(prompt: str) -> dict:
    parsed = intent_mod.classify(prompt, st.session_state.active)
    if parsed.kind == "help":
        # An advice question naming no company gets the refusal, not the
        # generic "type a ticker" help - it was a specific ask.
        text = guardrails.refusal_text(parsed.refusal) if parsed.refusal else HELP
        st.markdown(text)
        return {"role": "assistant", "kind": "note", "text": text}
    if parsed.kind == "brief":
        # "Is AMD a buy?" - decline the recommendation, then serve the brief,
        # which contains no recommendation and is what they actually need.
        if parsed.refusal:
            st.warning(parsed.refusal.message, icon="🚫")
        st.session_state.active = parsed.ticker
        return run_brief(parsed.ticker)
    if not credentials_available():
        text = "Follow-up questions need Claude credentials."
        st.warning(text, icon="🚫")
        return {"role": "assistant", "kind": "note", "text": text}
    return run_followup(parsed.ticker, parsed.question)


# --- page --------------------------------------------------------------------

init_state()
sidebar()

st.title("GlobalInsight")
st.caption(
    "SEC filing advisor brief — figures retrieved from XBRL, narrative cited to "
    "the filing, item and date."
)

if not st.session_state.turns:
    st.info(HELP, icon="💬")

for turn in st.session_state.turns:
    replay(turn)

if st.session_state.pending:
    prompt = st.session_state.pending
    st.session_state.pending = None
    with st.chat_message("assistant"):
        st.session_state.turns.append(handle(prompt))

if prompt := st.chat_input("Ticker, company name, or a question about the filings…"):
    submit(prompt)
    st.rerun()
