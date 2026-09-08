"""Tests for globalinsight.synthesize: the one LLM call in this package.

Hard constraint: NEVER make a real Anthropic API call. Every test here
injects a fake/mock client into generate_brief(); none touches
synthesize._client() or constructs a real anthropic.Anthropic().
"""

import json
import logging
import types
from unittest.mock import MagicMock

import pytest

from globalinsight import synthesize
from globalinsight.clean import count_tokens_approx
from globalinsight.config import SYNTHESIS_EFFORT, SYNTHESIS_MAX_TOKENS, SYNTHESIS_MODEL
from globalinsight.models import Citation, Company, FinancialPoint, FinancialSeries, Quote
from globalinsight.synthesize import BRIEF_SCHEMA, SynthesisContext, generate_brief


def _sized_text(tokens: int) -> str:
    """Filler text whose count_tokens_approx() is exactly ``tokens`` - lets
    budget tests hit precise, realistic token counts without embedding a
    giant real filing in the test file."""
    return "A" * (tokens * 4)


def _citation(accession="acc-1") -> Citation:
    return Citation(
        form="10-K",
        item="",
        filed_date="2026-03-15",
        accession=accession,
        url=f"https://www.sec.gov/Archives/edgar/data/1045810/{accession}-index.htm",
    )


def _financials(eps_values: dict[int, float] | None = None) -> dict[str, FinancialSeries]:
    series = {
        "Revenue": FinancialSeries(
            concept="Revenue",
            unit="USD",
            points=[FinancialPoint(fiscal_year=2025, value=130_497_000_000.0, citation=_citation())],
        )
    }
    if eps_values:
        series["EPS"] = FinancialSeries(
            concept="EPS",
            unit="USD/shares",
            points=[
                FinancialPoint(fiscal_year=year, value=value, citation=_citation())
                for year, value in eps_values.items()
            ],
        )
    return series


def _ctx(**overrides) -> SynthesisContext:
    base = dict(
        company=Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP"),
        quote=Quote(ticker="NVDA", available=True, price=131.26, change_percent=1.76),
        financials=_financials(),
        tenk_text="Item 1. Business. NVIDIA designs GPUs...",
        tenk_citation=_citation("acc-10k"),
        eight_k_texts={"acc-8k-1": "Record quarterly revenue of $X."},
        eight_k_citations={
            "acc-8k-1": Citation(
                form="8-K", item="2.02", filed_date="2026-06-01",
                accession="acc-8k-1", url="https://example.com/8k1",
            ),
            "acc-8k-2": Citation(
                form="8-K", item="8.01", filed_date="2026-06-15",
                accession="acc-8k-2", url="https://example.com/8k2",
            ),
        },
    )
    base.update(overrides)
    return SynthesisContext(**base)


def _cited_item(text: str, accession: str = "acc-10k", **citation_overrides) -> dict:
    citation = {
        "form": "10-K", "item": None, "filed_date": "2026-03-15",
        "accession": accession, "url": "https://example.com/should-be-overwritten",
    }
    citation.update(citation_overrides)
    return {"text": text, "citation": citation}


def _valid_payload() -> dict:
    return {
        "business_line": _cited_item("NVIDIA designs and sells GPUs for gaming and data centers."),
        "take": [
            _cited_item("Take bullet 1.", accession="acc-10k"),
            _cited_item("Take bullet 2.", accession="acc-10k"),
            _cited_item("Take bullet 3.", accession="acc-10k"),
        ],
        "risks": [
            _cited_item("Risk bullet 1.", accession="acc-10k"),
            _cited_item("Risk bullet 2.", accession="acc-10k"),
            _cited_item("Risk bullet 3.", accession="acc-10k"),
        ],
        "whats_changed": [
            _cited_item("Change bullet 1.", accession="acc-8k-1", form="8-K", item="2.02"),
        ],
    }


_VALID_PAYLOAD = _valid_payload()


def _fake_response(payload: dict, stop_reason: str = "end_turn"):
    text_block = types.SimpleNamespace(type="text", text=json.dumps(payload))
    return types.SimpleNamespace(content=[text_block], stop_reason=stop_reason)


def _thinking_only_response(stop_reason: str = "end_turn"):
    """A response with no text block at all - e.g. a thinking-only turn."""
    thinking_block = types.SimpleNamespace(type="thinking", thinking="...")
    return types.SimpleNamespace(content=[thinking_block], stop_reason=stop_reason)


def _stream_client(response) -> MagicMock:
    """A MagicMock client wired up so that

        with client.beta.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()

    (the streaming call shape generate_brief now uses - beta namespace,
    since fallbacks="default" requires a beta header) yields ``response``.
    """
    client = MagicMock()
    stream_cm = client.beta.messages.stream.return_value
    stream_cm.__enter__.return_value.get_final_message.return_value = response
    return client


def _fake_stream_client(payload: dict, stop_reason: str = "end_turn") -> MagicMock:
    return _stream_client(_fake_response(payload, stop_reason=stop_reason))


# --- prompt construction -------------------------------------------------


def test_build_user_message_includes_company_and_financials():
    msg = synthesize.build_user_message(_ctx())
    assert "NVIDIA CORP" in msg
    assert "NVDA" in msg
    assert "1045810" in msg
    assert "Revenue FY2025" in msg
    assert "acc-10k" in msg


def test_build_user_message_quote_unavailable():
    ctx = _ctx(quote=Quote(ticker="NVDA", available=False, error="429"))
    msg = synthesize.build_user_message(ctx)
    assert "QUOTE: unavailable" in msg


def test_build_user_message_quote_partial_data():
    ctx = _ctx(quote=Quote(ticker="NVDA", available=True, price=None, change_percent=None))
    msg = synthesize.build_user_message(ctx)
    assert "partial data" in msg


def test_build_user_message_no_financials():
    ctx = _ctx(financials={})
    msg = synthesize.build_user_message(ctx)
    assert "FINANCIALS: none available." in msg


def test_filings_block_includes_tenk_and_tiered_8k_text():
    ctx = _ctx()
    msg = synthesize.build_user_message(ctx)
    assert "NVIDIA designs GPUs" in msg
    assert "Record quarterly revenue" in msg
    assert 'accession="acc-10k"' in msg


def test_filings_block_skips_8k_text_with_no_matching_citation():
    """Defensive branch: an eight_k_texts entry with no corresponding
    eight_k_citations entry must be skipped, not raise or render a filing
    block with missing fields."""
    ctx = _ctx(
        eight_k_texts={"orphan-accession": "some text with no citation"},
        eight_k_citations={},
    )
    msg = synthesize.build_user_message(ctx)
    assert "some text with no citation" not in msg


def test_filings_block_lists_metadata_only_8ks_without_body():
    """A tier-3 8-K (no fetched body) still appears so the model knows it
    exists, but must not have fabricated content attached."""
    ctx = _ctx()  # acc-8k-2 has a citation but no entry in eight_k_texts
    msg = synthesize.build_user_message(ctx)
    assert "METADATA-ONLY 8-Ks" in msg
    assert "item 8.01" in msg
    assert "https://example.com/8k2" in msg
    # And it must not appear as a full <filing> body block (no fabricated content).
    assert 'accession="acc-8k-2"' not in msg


# --- Task 2 (C3): EPS precision -------------------------------------------


def test_eps_renders_at_full_precision_not_rounded_to_integer():
    """Regression test for the C3 bug: {:,.0f} rounded EPS 2.94 -> "3" before
    the model ever saw it. Per-share units must always keep 2 decimals."""
    ctx = _ctx(financials=_financials({2023: 1.19, 2024: 2.94, 2025: 4.90}))
    msg = synthesize.build_user_message(ctx)

    assert "1.19 USD/shares" in msg
    assert "2.94 USD/shares" in msg
    assert "4.90 USD/shares" in msg

    # None of the true values may have been rounded down to a bare integer.
    assert "1 USD/shares" not in msg
    assert "3 USD/shares" not in msg
    assert "5 USD/shares" not in msg


def test_format_financial_value_eps_precision_directly():
    assert synthesize._format_financial_value(2.94, "USD/shares") == "2.94 USD/shares"
    assert synthesize._format_financial_value(1.0, "USD/shares") == "1.00 USD/shares"
    assert synthesize._format_financial_value(-0.5, "USD/shares") == "-0.50 USD/shares"


def test_format_financial_value_large_usd_uses_readable_magnitude():
    assert synthesize._format_financial_value(130_497_000_000.0, "USD") == "$130.5B"
    assert synthesize._format_financial_value(45_000_000.0, "USD") == "$45.0M"


def test_format_financial_value_small_usd_keeps_whole_units():
    assert synthesize._format_financial_value(950_000.0, "USD") == "950,000 USD"


# --- schema shape: brevity caps -------------------------------------------


def test_schema_caps_take_and_risks_at_exactly_three_bullets():
    for key in ("take", "risks"):
        schema = BRIEF_SCHEMA["properties"][key]
        assert schema["minItems"] == 3
        assert schema["maxItems"] == 3


def test_schema_caps_whats_changed_at_three_bullets_but_allows_empty():
    schema = BRIEF_SCHEMA["properties"]["whats_changed"]
    assert schema["minItems"] == 0
    assert schema["maxItems"] == 3


def test_schema_has_no_client_questions_or_business_section():
    assert "client_questions" not in BRIEF_SCHEMA["properties"]
    assert "business" not in BRIEF_SCHEMA["properties"]
    assert "business_line" in BRIEF_SCHEMA["properties"]


def test_schema_citation_item_is_nullable_not_required_string():
    item_schema = BRIEF_SCHEMA["properties"]["risks"]["items"]["properties"]["citation"][
        "properties"
    ]["item"]
    assert "null" in item_schema["type"]


# --- generate_brief: exact request shape --------------------------------


def test_generate_brief_sends_exact_request_shape():
    client = _fake_stream_client(_VALID_PAYLOAD)

    generate_brief(_ctx(), client=client)

    # The call goes through beta.messages.stream(...).get_final_message() -
    # beta namespace because fallbacks="default" needs a beta header - never
    # the plain (non-beta) .stream() and never the non-streaming .create().
    assert client.beta.messages.stream.call_count == 1
    client.messages.create.assert_not_called()
    client.messages.stream.assert_not_called()
    client.beta.messages.create.assert_not_called()
    _, kwargs = client.beta.messages.stream.call_args

    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["model"] == SYNTHESIS_MODEL
    assert kwargs["max_tokens"] == 16000
    assert kwargs["max_tokens"] == SYNTHESIS_MAX_TOKENS
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["output_config"]["effort"] == SYNTHESIS_EFFORT
    assert kwargs["output_config"]["format"] == {"type": "json_schema", "schema": BRIEF_SCHEMA}
    assert kwargs["system"] == synthesize.SYSTEM_PROMPT

    # Optional-but-added: server-side refusal fallback, recommended for
    # claude-opus-5 - see shared/model-migration.md -> Migrating to Claude
    # Opus 5 -> New API features.
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"

    # messages: a single user turn, content split into two text blocks
    # (stable/cached 10-K block first, volatile block after).
    assert len(kwargs["messages"]) == 1
    message = kwargs["messages"][0]
    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, list)
    assert len(content) == 2

    stable_block, volatile_block = content
    assert stable_block["type"] == "text"
    assert "NVIDIA designs GPUs" in stable_block["text"]  # the 10-K text
    assert "NVIDIA CORP" in stable_block["text"]
    assert volatile_block["type"] == "text"
    assert "Produce the brief now." in volatile_block["text"]
    assert "Record quarterly revenue" in volatile_block["text"]  # the 8-K text
    # The 10-K must not also appear in the volatile block - it would be
    # duplicated (and double-billed) rather than isolated behind the cache.
    assert "NVIDIA designs GPUs" not in volatile_block["text"]


def test_generate_brief_caches_the_tenk_block_only():
    """M1: the 10-K is static/immutable and must carry the cache_control
    breakpoint. Nothing else in the request should be cached."""
    client = _fake_stream_client(_VALID_PAYLOAD)

    generate_brief(_ctx(), client=client)

    _, kwargs = client.beta.messages.stream.call_args
    stable_block, volatile_block = kwargs["messages"][0]["content"]
    assert stable_block["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in volatile_block


def test_generate_brief_uses_streaming_and_get_final_message():
    """M2: long input / high max_tokens should stream, not block on a
    single non-streaming response."""
    response = _fake_response(_VALID_PAYLOAD)
    client = _stream_client(response)

    brief = generate_brief(_ctx(), client=client)

    client.beta.messages.stream.assert_called_once()
    stream_cm = client.beta.messages.stream.return_value
    stream_cm.__enter__.assert_called_once()
    stream_cm.__enter__.return_value.get_final_message.assert_called_once()
    assert brief.ticker == "NVDA"  # the response the mock stream returned was actually used


def test_generate_brief_never_constructs_a_real_client(monkeypatch):
    """If a client is injected, synthesize._client() (which would construct
    anthropic.Anthropic() and require credentials) must never be called."""

    def fail_client():
        raise AssertionError("_client() must not be called when a client is injected")

    monkeypatch.setattr(synthesize, "_client", fail_client)

    client = _fake_stream_client(_VALID_PAYLOAD)
    generate_brief(_ctx(), client=client)  # must not raise


def test_importing_synthesize_never_constructs_anthropic_client(monkeypatch):
    """The Anthropic client must never be constructed at import time - this
    environment has no ANTHROPIC_API_KEY, so anthropic.Anthropic() would
    raise, and importing the module must not trigger that."""
    import importlib

    import anthropic

    def exploding_client(*a, **k):
        raise AssertionError("anthropic.Anthropic() constructed at import time")

    monkeypatch.setattr(anthropic, "Anthropic", exploding_client)
    importlib.reload(synthesize)  # must not raise


def test_generate_brief_raises_on_model_refusal():
    client = _fake_stream_client(_VALID_PAYLOAD, stop_reason="refusal")

    with pytest.raises(RuntimeError, match="refused"):
        generate_brief(_ctx(), client=client)


# --- M2: truncation / max_tokens / thinking-only responses ---------------


def test_generate_brief_raises_clear_error_on_max_tokens_not_json_decode_error():
    """stop_reason == 'max_tokens' means the JSON is truncated - this must
    surface as a clear, actionable RuntimeError, never a bare
    json.JSONDecodeError from trying to parse the truncated payload."""
    # Truncate the serialized JSON mid-stream, the way a real max_tokens cutoff would.
    truncated = json.dumps(_VALID_PAYLOAD)[:50]
    text_block = types.SimpleNamespace(type="text", text=truncated)
    response = types.SimpleNamespace(content=[text_block], stop_reason="max_tokens")
    client = _stream_client(response)

    with pytest.raises(RuntimeError, match="max_tokens"):
        generate_brief(_ctx(), client=client)


def test_generate_brief_max_tokens_checked_before_json_parsing_even_with_valid_json():
    """Even if the text block happens to contain complete, valid JSON, a
    max_tokens stop_reason must still raise - the response is not trusted
    to be complete just because it happens to parse."""
    client = _fake_stream_client(_VALID_PAYLOAD, stop_reason="max_tokens")

    with pytest.raises(RuntimeError, match="max_tokens"):
        generate_brief(_ctx(), client=client)


def test_generate_brief_raises_clear_error_on_thinking_only_response():
    """A response with only thinking blocks (no text block) must raise a
    clear RuntimeError, not a bare StopIteration from next() with no
    default."""
    client = _stream_client(_thinking_only_response())

    with pytest.raises(RuntimeError, match="no text block"):
        generate_brief(_ctx(), client=client)


# --- generate_brief: new output shape (Task 1) ----------------------------


def test_generate_brief_parses_new_four_field_shape():
    client = _fake_stream_client(_VALID_PAYLOAD)

    brief = generate_brief(_ctx(), client=client)

    assert brief.ticker == "NVDA"
    assert brief.business_line.content == [
        "NVIDIA designs and sells GPUs for gaming and data centers."
    ]
    assert brief.take.heading == "The 30-Second Take"
    assert len(brief.take.content) == 3
    assert brief.risks.heading == "Key Risks"
    assert len(brief.risks.content) == 3
    assert brief.whats_changed.heading == "What's Changed"
    assert len(brief.whats_changed.content) == 1

    # client_questions / business sections no longer exist on Brief.
    assert not hasattr(brief, "client_questions")
    assert not hasattr(brief, "business")


def test_generate_brief_empty_whats_changed_when_none_supplied():
    payload = _valid_payload()
    payload["whats_changed"] = []
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)
    assert brief.whats_changed.content == []
    assert brief.whats_changed.citations == []


# --- Task 3 (C4): citation allow-set validation ---------------------------


def test_valid_accession_passes_through_with_canonical_fields_overwritten():
    """A citation whose accession matches a real supplied filing must render
    with the CANONICAL form/item/filed_date/url - not whatever (possibly
    stale or wrong) copy the model emitted."""
    payload = _valid_payload()
    payload["whats_changed"] = [
        _cited_item(
            "NVIDIA reported record quarterly revenue.",
            accession="acc-8k-1",
            form="WRONG-FORM",
            item="9.99",
            filed_date="1999-01-01",
            url="https://not-the-real-url.example.com",
        )
    ]
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)

    assert len(brief.whats_changed.citations) == 1
    citation = brief.whats_changed.citations[0]
    # Canonical values from ctx.eight_k_citations["acc-8k-1"], not the model's.
    assert citation.form == "8-K"
    assert citation.item == "2.02"
    assert citation.filed_date == "2026-06-01"
    assert citation.url == "https://example.com/8k1"
    assert citation.accession == "acc-8k-1"
    assert brief.dropped_citations == []


def test_fabricated_accession_is_rejected_and_bullet_dropped():
    """A schema-valid but hallucinated accession must not pass through as a
    real citation - the bullet is dropped and the failure is recorded."""
    payload = _valid_payload()
    payload["risks"] = [
        _cited_item("Risk bullet 1.", accession="acc-10k"),
        _cited_item("Risk bullet 2.", accession="acc-10k"),
        _cited_item(
            "A risk citing a filing that was never supplied.",
            accession="acc-does-not-exist",
            form="8-K",
            item="1.01",
            filed_date="2026-07-01",
            url="https://fabricated.example.com",
        ),
    ]
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)

    # Only the 2 validly-cited risk bullets survive.
    assert len(brief.risks.content) == 2
    assert "A risk citing a filing that was never supplied." not in brief.risks.content
    assert all(c.accession != "acc-does-not-exist" for c in brief.risks.citations)

    # The failure is visible, not silent.
    assert "acc-does-not-exist" in brief.dropped_citations


def test_business_line_dropped_entirely_when_its_citation_is_invalid():
    """business_line must not render as though it were sourced when its
    citation doesn't validate - here that means dropping it outright rather
    than showing uncited prose."""
    payload = _valid_payload()
    payload["business_line"] = _cited_item(
        "A business description citing nothing real.", accession="totally-fake"
    )
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)

    assert brief.business_line.content == []
    assert brief.business_line.citations == []
    assert "totally-fake" in brief.dropped_citations


def test_citation_allow_set_includes_tenk_and_all_eight_ks():
    allow_set = synthesize._citation_allow_set(_ctx())
    assert set(allow_set) == {"acc-10k", "acc-8k-1", "acc-8k-2"}
    assert allow_set["acc-10k"].form == "10-K"
    assert allow_set["acc-8k-2"].item == "8.01"


def test_citation_allow_set_empty_when_no_tenk_or_eight_ks():
    ctx = _ctx(tenk_citation=None, eight_k_citations={}, eight_k_texts={})
    assert synthesize._citation_allow_set(ctx) == {}


# --- Task 4 (H5): optional/nullable citation item -------------------------


def test_tenk_citation_with_null_item_parses_cleanly():
    """A 10-K citation with a null/empty item must not raise and must render
    with an empty item, not a fabricated one."""
    payload = _valid_payload()
    payload["take"] = [
        _cited_item("Take bullet citing the 10-K with no item.", accession="acc-10k", item=None),
        _cited_item("Take bullet 2.", accession="acc-10k"),
        _cited_item("Take bullet 3.", accession="acc-10k"),
    ]
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)

    assert len(brief.take.citations) == 3
    tenk_citations = [c for c in brief.take.citations if c.accession == "acc-10k"]
    assert all(c.item == "" for c in tenk_citations)


def test_eight_k_citation_item_comes_from_canonical_submissions_data():
    payload = _valid_payload()
    payload["whats_changed"] = [
        _cited_item(
            "Change bullet.", accession="acc-8k-1", item="wrong-item-the-model-guessed"
        ),
    ]
    client = _fake_stream_client(payload)

    brief = generate_brief(_ctx(), client=client)

    assert brief.whats_changed.citations[0].item == "2.02"  # from ctx, not the model


# --- M1: global token budget ----------------------------------------------


def test_apply_token_budget_no_op_when_under_budget():
    ctx = _ctx()
    trimmed, dropped = synthesize._apply_token_budget(ctx)
    assert dropped == []
    assert trimmed is ctx  # unchanged - not even a copy


def test_apply_token_budget_drops_oldest_8ks_first_keeps_tenk_and_newest_earnings():
    ctx = _ctx(
        tenk_text=_sized_text(100_000),
        eight_k_texts={
            "old-1": _sized_text(20_000),
            "old-2": _sized_text(20_000),
            "earnings-old": _sized_text(20_000),
            "earnings-new": _sized_text(20_000),
        },
        eight_k_citations={
            "old-1": Citation(
                form="8-K", item="1.01", filed_date="2025-01-01",
                accession="old-1", url="https://example.com/old-1",
            ),
            "old-2": Citation(
                form="8-K", item="5.02", filed_date="2025-04-01",
                accession="old-2", url="https://example.com/old-2",
            ),
            "earnings-old": Citation(
                form="8-K", item="2.02", filed_date="2025-07-01",
                accession="earnings-old", url="https://example.com/earnings-old",
            ),
            "earnings-new": Citation(
                form="8-K", item="2.02", filed_date="2025-10-01",
                accession="earnings-new", url="https://example.com/earnings-new",
            ),
        },
    )
    # total = 100_000 (10-K) + 80_000 (8-Ks) = 180_000. A 120_000 budget
    # forces dropping everything except the protected newest-earnings 8-K.
    trimmed, dropped = synthesize._apply_token_budget(ctx, budget=120_000)

    # Oldest-filed-first drop order.
    assert dropped == ["old-1", "old-2", "earnings-old"]
    # The 10-K survives untouched - never dropped.
    assert trimmed.tenk_text == ctx.tenk_text
    # The most recent earnings (item 2.02) 8-K survives - it's protected.
    assert "earnings-new" in trimmed.eight_k_texts
    assert "old-1" not in trimmed.eight_k_texts
    assert "old-2" not in trimmed.eight_k_texts
    assert "earnings-old" not in trimmed.eight_k_texts
    # Citations for dropped 8-Ks are untouched - they degrade to a
    # metadata-only mention (see _eight_k_filings_block) rather than
    # vanishing, and remain valid citation targets (see allow-set tests).
    assert set(trimmed.eight_k_citations) == set(ctx.eight_k_citations)


def test_apply_token_budget_jpm_sized_fixture_matches_measured_numbers():
    """Numbers from the M1 measurement: JPM's 10-K alone is ~352,601 tokens;
    with its tier-1/2 8-Ks the full assembled payload is ~412,729 tokens -
    just over TOTAL_TOKEN_BUDGET (400,000). Enforcing the budget should
    bring the payload back under budget by dropping only the oldest 8-K,
    while the 10-K and the most recent earnings 8-K survive untouched."""
    tenk_tokens = 352_601
    eight_ks = [  # (accession, filed_date, item, tokens)
        ("old-1", "2025-04-01", "1.01", 15_000),
        ("old-2", "2025-07-01", "5.02", 10_000),
        ("earnings-older", "2025-10-14", "2.02", 12_000),
        ("earnings-latest", "2026-01-13", "2.02", 23_128),
    ]
    ctx = _ctx(
        tenk_text=_sized_text(tenk_tokens),
        eight_k_texts={accn: _sized_text(tok) for accn, _, _, tok in eight_ks},
        eight_k_citations={
            accn: Citation(
                form="8-K", item=item, filed_date=filed,
                accession=accn, url=f"https://example.com/{accn}",
            )
            for accn, filed, item, _ in eight_ks
        },
    )

    pre_budget_total = count_tokens_approx(ctx.tenk_text) + sum(
        count_tokens_approx(t) for t in ctx.eight_k_texts.values()
    )
    assert pre_budget_total == 412_729  # matches the measured JPM number

    trimmed, dropped = synthesize._apply_token_budget(ctx)
    post_budget_total = count_tokens_approx(trimmed.tenk_text) + sum(
        count_tokens_approx(t) for t in trimmed.eight_k_texts.values()
    )

    assert dropped == ["old-1"]  # oldest dropped first, only as much as needed
    assert post_budget_total <= synthesize.TOTAL_TOKEN_BUDGET
    assert post_budget_total == 397_729
    assert trimmed.tenk_text == ctx.tenk_text  # 10-K untouched
    assert "earnings-latest" in trimmed.eight_k_texts  # newest earnings 8-K untouched


def test_generate_brief_applies_token_budget_and_logs_dropped_8ks(caplog):
    """End-to-end: generate_brief() must trim an over-budget ctx before
    building the request, and the drop must be visible (logged), not
    silent."""
    ctx = _ctx(
        tenk_text=_sized_text(50_000),
        eight_k_texts={
            "old": _sized_text(200_000),
            "new-earnings": _sized_text(200_000),
        },
        eight_k_citations={
            "old": Citation(
                form="8-K", item="1.01", filed_date="2025-01-01",
                accession="old", url="https://example.com/old",
            ),
            "new-earnings": Citation(
                form="8-K", item="2.02", filed_date="2026-01-01",
                accession="new-earnings", url="https://example.com/new-earnings",
            ),
        },
    )
    client = _fake_stream_client(_VALID_PAYLOAD)

    with caplog.at_level(logging.WARNING, logger="globalinsight.synthesize"):
        generate_brief(ctx, client=client)

    assert any(
        "old" in record.message and "dropped" in record.message.lower()
        for record in caplog.records
    ), "dropping an 8-K for budget must be logged, not silent"

    _, kwargs = client.beta.messages.stream.call_args
    volatile_block = kwargs["messages"][0]["content"][1]["text"]
    # Dropped: degrades to a metadata-only mention, not a full filing body.
    assert 'accession="old"' not in volatile_block
    assert "https://example.com/old" in volatile_block
    # Kept: the newest earnings 8-K's full body still reaches the model.
    assert 'accession="new-earnings"' in volatile_block
