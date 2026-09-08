"""Starlette HTTP layer. Routes translate to backend calls; no domain logic.

Blocking backend work runs in a thread so the event loop can keep emitting
heartbeats - the wave-3 status line is driven by server events, not a
client-side timer, so it stays truthful if the work stalls.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from api import fixtures, guardrails, qa, serialize
from globalinsight import config, edgar, pipeline, quote as quote_mod, synthesize
from chatui import intent as intent_mod

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

# Per-ticker synthesis context, so a follow-up does not refetch documents.
# Single-process demo state, deliberately not a real cache.
_CONTEXTS: dict[str, Any] = {}
_WAVES: dict[str, tuple[Any, Any]] = {}

HEARTBEAT_SECONDS = 1.0


# --- helpers -----------------------------------------------------------------

def sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


def error_body(kind: str, message: str, actionable: str = "") -> dict:
    return {"error": {"kind": kind, "message": message, "actionable": actionable}}


def classify_exception(exc: BaseException) -> dict:
    """Map a backend exception to the spec's error envelope.

    Raw exception text never reaches the browser; `message` is written for a
    stakeholder in the room, `actionable` names the fix.
    """
    text = str(exc)
    if "credit balance is too low" in text:
        return error_body(
            "no_credits",
            "Synthesis unavailable - the API account has no credits.",
            "Waves 1-2 are complete and unaffected. Set GI_FIXTURES=1 to demo "
            "from recorded data.",
        )
    if "403" in text and "sec.gov" in text.lower():
        return error_body(
            "sec_forbidden",
            "The SEC rejected our request.",
            "Set SEC_USER_AGENT to a real contact address; generic browser "
            "strings and example.com are refused.",
        )
    if "429" in text:
        return error_body(
            "rate_limited", "Upstream rate limit reached.", "Retry in a moment."
        )
    return error_body(
        "synthesis_failed",
        "The narrative could not be generated.",
        "Waves 1-2 above are unaffected.",
    )


async def run_blocking(function: Callable, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: function(*args))


async def with_heartbeat(
    function: Callable, emit: Callable[[float], bytes]
) -> AsyncIterator:
    """Run blocking `function`, yielding a heartbeat ~1s until it finishes.

    Yields heartbeat bytes, then finally a ("result", value, error) tuple. The
    executor's exception is captured rather than raised so a synthesis failure
    becomes a block_error event on a stream that still completes, instead of
    tearing down the connection.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    future = loop.run_in_executor(None, function)
    while True:
        done, _ = await asyncio.wait({future}, timeout=HEARTBEAT_SECONDS)
        if done:
            try:
                value, error = future.result(), None
            except Exception as exc:
                value, error = None, exc
            yield ("result", value, error)
            return
        yield emit(time.monotonic() - started)


# --- plain endpoints ---------------------------------------------------------

async def health(request: Request) -> JSONResponse:
    """Powers the environment strip. Must never fail."""
    credentials = False
    try:
        import anthropic

        anthropic.Anthropic()
        credentials = True
    except Exception:
        credentials = False

    cache_bytes = 0
    try:
        cache_bytes = sum(
            p.stat().st_size for p in config.CACHE_DIR.rglob("*") if p.is_file()
        )
    except OSError:
        pass

    agent = config.SEC_USER_AGENT or ""
    # SEC refuses generic browser strings and example.com addresses with a 403.
    configured = bool(
        "@" in agent and "example.com" not in agent and not agent.startswith("Mozilla")
    )
    return JSONResponse(
        {
            "mode": "replay" if fixtures.enabled() else "live",
            "credentials": credentials,
            "sec_user_agent": agent,
            "sec_user_agent_configured": configured,
            "cache_bytes": cache_bytes,
            "model": config.SYNTHESIS_MODEL,
            "effort": config.SYNTHESIS_EFFORT,
            "fixtures_available": fixtures.available(),
            "fixture_speed": fixtures.speed(),
        }
    )


async def policy(request: Request) -> JSONResponse:
    """What the tool will and will not answer, with examples of each.

    Served from the guardrail module itself rather than restated here, so the
    boundary a client displays is the one the gate enforces.
    """
    return JSONResponse(guardrails.policy())


async def tickers(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip().upper()
    limit = min(int(request.query_params.get("limit") or 8), 25)
    if len(query) < 1:
        return JSONResponse([])
    try:
        rows = await run_blocking(edgar._raw_ticker_rows)
    except Exception as exc:
        return JSONResponse(classify_exception(exc), status_code=200)

    starts, contains = [], []
    for row in rows.values():
        symbol = str(row.get("ticker", "")).upper()
        name = str(row.get("title", ""))
        if symbol.startswith(query):
            starts.append({"ticker": symbol, "name": name})
        elif query in symbol or query.lower() in name.lower():
            contains.append({"ticker": symbol, "name": name})
        if len(starts) >= limit:
            break
    return JSONResponse((starts + contains)[:limit])


async def resolve(request: Request) -> JSONResponse:
    text = request.query_params.get("q") or ""
    active = request.query_params.get("active") or None
    parsed = await run_blocking(intent_mod.classify, text, active)
    return JSONResponse(
        {
            "kind": parsed.kind,
            "ticker": parsed.ticker,
            "question": parsed.question,
            # Present on an advice-seeking message. On a "brief" intent the
            # client shows this above the brief it still renders; there is no
            # recommendation in a brief, so the factual need is served while
            # the boundary is stated once.
            "refusal": guardrails.serialize(parsed.refusal),
        }
    )


async def quote(request: Request) -> JSONResponse:
    ticker = request.path_params["ticker"].upper()
    try:
        result = await run_blocking(quote_mod.get_quote, ticker)
    except Exception as exc:
        # A failed quote is a data state, not a transport error.
        return JSONResponse(
            {
                "ticker": ticker,
                "available": False,
                "error": classify_exception(exc)["error"]["message"],
            }
        )
    return JSONResponse(serialize.quote(result))


# --- brief stream ------------------------------------------------------------

async def brief_stream(ticker: str) -> AsyncIterator[bytes]:
    replay = fixtures.enabled()
    fixture = fixtures.load(ticker) if replay else None
    yield sse(
        "meta",
        {
            "ticker": ticker,
            "request_id": uuid.uuid4().hex[:12],
            # Only claim replay when a fixture actually backs THIS ticker;
            # otherwise an unknown symbol shows a replay banner it never used.
            "mode": "replay" if (replay and fixture) else "live",
            "replay_note": (fixture or {}).get("provenance_note") if replay else None,
        },
    )

    # --- wave 2. Served from the on-disk cache; no credits involved.
    try:
        wave2_result = await run_blocking(pipeline.wave2, ticker)
    except Exception as exc:
        if fixture and fixture.get("wave2"):
            wave2_payload = fixture["wave2"]  # offline fallback
            wave2_result = None
        else:
            yield sse("block_error", {"block": "wave2", **classify_exception(exc)["error"]})
            yield sse("done", {"elapsed_seconds": 0})
            return
    else:
        wave2_payload = serialize.wave2(wave2_result)

    yield sse("wave2", wave2_payload)

    classification = wave2_payload.get("classification")
    if classification != "filer" or wave2_result is None:
        # ETF or pre-annual: structurally nothing to synthesise. Not an error.
        yield sse("done", {"elapsed_seconds": 0, "classification": classification})
        return

    # --- wave 3. Document fetch, then the paid synthesis call.
    started = time.monotonic()
    try:
        wave3_result = await run_blocking(pipeline.wave3, wave2_result)
    except Exception as exc:
        yield sse("block_error", {"block": "documents", **classify_exception(exc)["error"]})
        yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        return

    _WAVES[ticker] = (wave2_result, wave3_result)
    bodies = set(getattr(wave3_result, "eight_k_texts", {}) or {})
    tenk_text = getattr(wave3_result, "tenk_text", None) or ""
    approx_tokens = (
        len(tenk_text) + sum(len(t) for t in (wave3_result.eight_k_texts or {}).values())
    ) // 4

    yield sse(
        "wave3_start",
        {
            "annual": {
                "form": (wave2_payload.get("annual_filing") or {}).get("form"),
                "filed": (wave2_payload.get("annual_filing") or {}).get("filing_date"),
            },
            "eight_k_bodies": len(bodies),
            "eight_k_indexed_only": max(
                len(wave2_payload.get("eight_ks") or []) - len(bodies), 0
            ),
            # Confirms the per-filing marker the wave2 event could not yet know.
            "bodies_retrieved": sorted(bodies),
            "tokens_approx": approx_tokens,
            "errors": dict(getattr(wave3_result, "errors", {}) or {}),
        },
    )
    if getattr(wave3_result, "errors", None):
        for name, message in wave3_result.errors.items():
            yield sse(
                "block_error",
                {
                    "block": "documents",
                    "kind": "filing_fetch_failed",
                    "message": f"Could not retrieve {name}.",
                    "actionable": "The narrative proceeds on the text that was retrieved.",
                },
            )

    context = await run_blocking(
        pipeline.build_synthesis_context, wave2_result, wave3_result
    )
    if context is None:
        yield sse(
            "block_error",
            {
                "block": "synthesis",
                "kind": "filing_fetch_failed",
                "message": "No annual report text was retrieved, so no narrative was generated.",
                "actionable": "The quote and financial blocks above are unaffected.",
            },
        )
        yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        return
    _CONTEXTS[ticker] = context

    # --- replay: reproduce the recorded elapsed so the wait state is exercised.
    if replay and fixture and fixture.get("brief"):
        target = float(fixture.get("elapsed_seconds") or 20.0) / fixtures.speed()
        replay_started = time.monotonic()
        while True:
            elapsed = time.monotonic() - replay_started
            if elapsed >= target:
                break
            yield sse(
                "progress",
                {
                    "elapsed_seconds": round(elapsed * fixtures.speed(), 1),
                    "phase": "synthesis",
                },
            )
            await asyncio.sleep(min(HEARTBEAT_SECONDS, target - elapsed))
        yield sse("brief", fixture["brief"])
        yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        return

    synthesis_started = time.monotonic()
    result = None
    failure = None

    def call() -> Any:
        return synthesize.generate_brief(context)

    async for item in with_heartbeat(
        call,
        lambda elapsed: sse(
            "progress", {"elapsed_seconds": round(elapsed, 1), "phase": "synthesis"}
        ),
    ):
        if isinstance(item, tuple) and item and item[0] == "result":
            result, failure = item[1], item[2]
        else:
            yield item

    if result is None and failure is None:
        failure = RuntimeError("synthesis returned nothing")

    if failure is not None:
        yield sse("block_error", {"block": "synthesis", **classify_exception(failure)["error"]})
    else:
        yield sse("brief", serialize.brief(result))
    yield sse(
        "done",
        {
            "elapsed_seconds": round(time.monotonic() - synthesis_started, 1),
            "total_seconds": round(time.monotonic() - started, 1),
        },
    )


async def brief(request: Request) -> Response:
    ticker = request.path_params["ticker"].upper()

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in brief_stream(ticker):
                yield chunk
        except asyncio.CancelledError:  # client navigated away
            raise
        except Exception as exc:  # never leak a traceback into the stream
            yield sse("block_error", {"block": "stream", **classify_exception(exc)["error"]})
            yield sse("done", {"elapsed_seconds": 0})

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- ask stream --------------------------------------------------------------

async def ask_stream(ticker: str, question: str, history: list) -> AsyncIterator[bytes]:
    replay = fixtures.enabled()
    yield sse("meta", {"ticker": ticker, "mode": "replay" if replay else "live"})

    # Policy gate first: an advice question is refused before any document
    # fetch or paid call, so a blocked question costs nothing and answers in
    # the same shape as any other. qa.answer_question checks again for callers
    # that don't come through this route.
    refusal = guardrails.check_question(question)
    if refusal is not None:
        declined = qa.refusal_answer(refusal)
        yield sse("delta", {"text": declined.text})
        yield sse("answer", serialize.answer(declined))
        yield sse("done", {"elapsed_seconds": 0})
        return

    context = _CONTEXTS.get(ticker)
    if context is None:
        try:
            wave2_result = await run_blocking(pipeline.wave2, ticker)
            wave3_result = await run_blocking(pipeline.wave3, wave2_result)
            context = await run_blocking(
                pipeline.build_synthesis_context, wave2_result, wave3_result
            )
        except Exception as exc:
            yield sse("block_error", {"block": "answer", **classify_exception(exc)["error"]})
            yield sse("done", {"elapsed_seconds": 0})
            return
        if context is None:
            yield sse(
                "block_error",
                {
                    "block": "answer",
                    "kind": "filing_fetch_failed",
                    "message": f"No filing text is available for {ticker}.",
                    "actionable": "Load the brief for this ticker first.",
                },
            )
            yield sse("done", {"elapsed_seconds": 0})
            return
        _CONTEXTS[ticker] = context

    started = time.monotonic()

    if replay:
        fixture = fixtures.load(ticker) or {}
        recorded = fixtures.answer_for(fixture, question)
        if recorded is None:
            yield sse(
                "block_error",
                {
                    "block": "answer",
                    "kind": "no_credits",
                    "message": "No recorded answer for that question.",
                    "actionable": "Replay mode answers only recorded questions.",
                },
            )
            yield sse("done", {"elapsed_seconds": 0})
            return
        text = recorded.get("text") or ""
        target = float((recorded.get("usage") or {}).get("seconds") or 5.0) / fixtures.speed()
        words = text.split(" ")
        step = max(target / max(len(words), 1), 0.0)
        for index, word in enumerate(words):
            yield sse("delta", {"text": word + (" " if index < len(words) - 1 else "")})
            if step:
                await asyncio.sleep(min(step, 0.05))
        yield sse("answer", recorded)
        yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        return

    # Live: bridge the blocking, callback-driven call onto the event loop.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_delta(text: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("delta", text))

    def on_progress(elapsed: float) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("progress", elapsed))

    def call() -> Any:
        return qa.answer_question(
            context, history, question, on_progress=on_progress, on_delta=on_delta
        )

    future = loop.run_in_executor(None, call)
    last_beat = 0.0
    while True:
        if future.done() and queue.empty():
            break
        try:
            kind, payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            yield sse(
                "progress",
                {"elapsed_seconds": round(time.monotonic() - started, 1), "phase": "answer"},
            )
            continue
        if kind == "delta":
            yield sse("delta", {"text": payload})
        elif payload - last_beat >= HEARTBEAT_SECONDS:
            last_beat = payload
            yield sse("progress", {"elapsed_seconds": round(payload, 1), "phase": "answer"})

    try:
        result = future.result()
    except Exception as exc:
        yield sse("block_error", {"block": "answer", **classify_exception(exc)["error"]})
        yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        return

    yield sse("answer", serialize.answer(result))
    yield sse("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})


async def ask(request: Request) -> Response:
    try:
        body_json = await request.json()
    except Exception:
        return JSONResponse(
            error_body("internal", "Malformed request body.", "Send JSON."),
            status_code=400,
        )
    ticker = (body_json.get("ticker") or "").upper()
    question = body_json.get("question") or ""
    history = body_json.get("history") or []
    if not ticker or not question:
        return JSONResponse(
            error_body("internal", "ticker and question are required.", ""),
            status_code=400,
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in ask_stream(ticker, question, history):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield sse("block_error", {"block": "answer", **classify_exception(exc)["error"]})
            yield sse("done", {"elapsed_seconds": 0})

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


routes = [
    Route("/api/health", health),
    Route("/api/policy", policy),
    Route("/api/tickers", tickers),
    Route("/api/resolve", resolve),
    Route("/api/quote/{ticker}", quote),
    Route("/api/brief/{ticker}", brief),
    Route("/api/ask", ask, methods=["POST"]),
    Mount("/", app=StaticFiles(directory=str(WEB_ROOT), html=True), name="web"),
]

app = Starlette(routes=routes)
