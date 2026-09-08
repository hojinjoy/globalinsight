"""Tests for the advice-seeking question gate.

Two corpora, and the second one matters as much as the first: REFUSED is what
the guardrail must catch, ANSWERABLE is what it must not. A filter that
refuses "what do they sell?" or "did they announce a buyback?" has broken the
product to enforce a rule that was never about those questions, so those cases
are asserted here rather than left to be discovered in a demo.

Hard constraint inherited from conftest: no network, no API key. Nothing here
makes a model call - the point of the gate is that it decides without one.
"""

from unittest.mock import MagicMock

import pytest

from api import guardrails, qa
from chatui import intent as intent_mod
from globalinsight import edgar
from globalinsight.models import Company
from globalinsight.synthesize import SynthesisContext

# --- what must be refused, and as what ---------------------------------------

REFUSED: list[tuple[str, str]] = [
    # The literal asks this guardrail was built for.
    ("Is AMD a buy?", "recommendation"),
    ("is amd a buy", "recommendation"),
    ("Should I buy NVDA?", "recommendation"),
    ("Should I sell my position here?", "recommendation"),
    ("should my clients hold this through earnings?", "recommendation"),
    ("Would you buy it at this level?", "recommendation"),
    ("Is it a good investment?", "recommendation"),
    ("Is this a strong buy or a sell?", "recommendation"),
    ("buy, sell or hold?", "recommendation"),
    ("Do you recommend it?", "recommendation"),
    ("What would you recommend for a conservative client?", "recommendation"),
    ("Any recommendations?", "recommendation"),
    ("Can you give me investment advice on this name?", "recommendation"),
    ("Are you bullish on the stock?", "recommendation"),
    ("Would you overweight it versus the sector?", "recommendation"),
    ("Is it worth buying ahead of the print?", "recommendation"),
    ("Is now a good time to buy?", "recommendation"),
    ("What's a good entry point?", "recommendation"),
    ("Should I average down here?", "recommendation"),
    ("AMD a buy?", "recommendation"),
    ("should we be adding here?", "recommendation"),
    ("Thoughts on buying AMD before the print?", "recommendation"),
    ("Would you get in here?", "recommendation"),
    ("Is it too late to buy?", "recommendation"),
    ("Time to take profits?", "recommendation"),
    ("What would you do?", "recommendation"),
    ("My client wants to buy AMD - thoughts?", "recommendation"),
    ("Is this a value trap?", "recommendation"),
    ("Worth a look?", "recommendation"),
    ("What's your call on it?", "recommendation"),
    ("Is it a hold?", "recommendation"),
    # Valuation judgments.
    ("What's your price target?", "valuation_judgment"),
    ("Do you have a target price for the shares?", "valuation_judgment"),
    ("Is the stock overvalued?", "valuation_judgment"),
    ("is it undervalued relative to peers", "valuation_judgment"),
    ("Is it cheap right now?", "valuation_judgment"),
    ("What's the stock really worth?", "valuation_judgment"),
    ("Can you run a DCF on it?", "valuation_judgment"),
    ("What's your PT on AMD?", "valuation_judgment"),
    ("whats your target on amd", "valuation_judgment"),
    ("Can you value it for me?", "valuation_judgment"),
    ("How much upside is there from here?", "valuation_judgment"),
    # Price forecasts.
    ("Will the stock go up after earnings?", "price_forecast"),
    ("Do you think it will keep climbing?", "price_forecast"),
    ("How high can the stock go?", "price_forecast"),
    ("Where will the shares be in 12 months?", "price_forecast"),
    ("What's your price forecast?", "price_forecast"),
    ("Will it hit $200?", "price_forecast"),
    # Sizing / allocation.
    ("How much should I put into it?", "allocation"),
    ("What position size makes sense?", "allocation"),
    ("How much of my portfolio should this be?", "allocation"),
    ("How many shares should I pick up?", "allocation"),
]

# --- what must NOT be refused ------------------------------------------------
#
# Each of these is a legitimate question about a filing that a naive
# keyword filter ("buy", "sell", "hold", "value", "target") would eat.
ANSWERABLE: list[str] = [
    "What do they sell?",
    "What products does the company sell to data centre customers?",
    "Did they announce a buyback?",
    "How big is the share repurchase authorisation?",
    "What were selling, general and administrative expenses last year?",
    "Who did they buy in 2024?",
    "What acquisitions closed during the fiscal year?",
    "Should I be worried about their debt load?",
    "Should I read the risk factors section?",
    "What does the 10-K say about fair value measurements?",
    "Is there a sell-off risk disclosed in the filing?",
    "Did the board make a recommendation on the merger?",
    "Are they overweight in any one customer?",
    "What are the key risks?",
    "How does the company make money?",
    "What's changed since the annual report?",
    "What did the last earnings 8-K report?",
    "How have revenue and margins moved over three years?",
    "What's the bull case management lays out?",
    "Who are their largest customers by concentration?",
    "What did they say about supply constraints?",
    "Will revenue fall next year according to their guidance?",
    "What is the value of their inventory?",
    "How many shares are outstanding?",
    "Did any officers hold options that vested?",
    # README documents this exact phrasing as a way to ask for a brief. It is
    # an information request about a company, not a request for a view on it,
    # and refusing it would break a documented entry point.
    "What do you think about Nvidia?",
    "what do you think about the 8-K they filed last week?",
    "What did management say on the earnings call?",
    "Should I look at selling, general and administrative expenses?",
    "What's the value of goodwill on the balance sheet?",
    "",
]


@pytest.mark.parametrize("question,reason", REFUSED, ids=[q for q, _ in REFUSED])
def test_advice_questions_are_refused(question, reason):
    refusal = guardrails.check_question(question)
    assert refusal is not None, f"should have been refused: {question!r}"
    assert refusal.reason == reason
    assert refusal.message
    assert refusal.suggestions


@pytest.mark.parametrize("question", ANSWERABLE, ids=[q or "<empty>" for q in ANSWERABLE])
def test_filing_questions_are_not_refused(question):
    assert guardrails.check_question(question) is None, (
        f"false positive - this is an ordinary filing question: {question!r}"
    )


# --- the published examples must match the enforced behaviour ----------------
#
# These are what the UI displays and the design doc publishes. If a pattern
# change breaks one, the suite fails here rather than the documentation
# quietly becoming a lie.


@pytest.mark.parametrize("question", guardrails.ALLOWED_EXAMPLES)
def test_published_allowed_example_is_actually_allowed(question):
    assert guardrails.check_question(question) is None


@pytest.mark.parametrize(
    "reason,question",
    [(r, q) for r, qs in guardrails.REFUSED_EXAMPLES.items() for q in qs],
)
def test_published_refused_example_is_actually_refused(reason, question):
    refusal = guardrails.check_question(question)
    assert refusal is not None
    assert refusal.reason == reason


def test_policy_covers_every_reason_the_checker_can_return():
    payload = guardrails.policy()
    published = {entry["reason"] for entry in payload["refused"]}
    assert published == {reason for reason, _ in guardrails._COMPILED}
    assert payload["allowed"] and payload["stance"]
    for entry in payload["refused"]:
        assert entry["label"] and entry["message"] and entry["examples"]


def test_refusal_text_names_the_boundary_and_offers_a_way_forward():
    refusal = guardrails.check_question("Is AMD a buy?")
    text = guardrails.refusal_text(refusal)
    assert refusal.message in text
    for suggestion in refusal.suggestions:
        assert suggestion in text


def test_serialize_shape_for_the_http_layer():
    payload = guardrails.serialize(guardrails.check_question("What's your price target?"))
    assert payload["reason"] == "valuation_judgment"
    assert isinstance(payload["suggestions"], list)
    assert payload["text"].startswith(payload["message"][:20])
    assert guardrails.serialize(None) is None


# --- the gate as wired into the answer path -----------------------------------


def _ctx() -> SynthesisContext:
    # Deliberately empty of filing text: a refusal must be decided before any
    # of it is read, so an unusable context is enough to prove the point.
    return SynthesisContext(company=Company(ticker="AMD", cik=2488, name="AMD INC"), quote=None)


def test_answer_question_refuses_without_calling_the_model():
    client = MagicMock()
    answer = qa.answer_question(_ctx(), [], "Should I buy AMD here?", client=client)

    assert client.messages.stream.call_count == 0
    assert client.messages.create.call_count == 0
    assert answer.refusal_reason == "recommendation"
    assert answer.claims == []
    assert answer.cost == 0.0
    assert answer.input_tokens == 0 and answer.output_tokens == 0
    assert "recommendation" in answer.text


def test_refused_answer_streams_its_text_to_the_delta_callback():
    seen: list[str] = []
    qa.answer_question(
        _ctx(), [], "Is it a buy?", client=MagicMock(), on_delta=seen.append
    )
    assert "".join(seen)


def test_answerable_question_is_not_short_circuited():
    """The gate must not swallow a real question before the model call."""
    client = MagicMock()
    client.messages.stream.side_effect = RuntimeError("reached the model call")
    with pytest.raises(RuntimeError, match="reached the model call"):
        qa.answer_question(_ctx(), [], "What are the key risks?", client=client)


def test_serialized_answer_marks_the_refusal_for_the_client():
    from api import serialize

    payload = serialize.answer(
        qa.refusal_answer(guardrails.check_question("Is AMD a buy?"))
    )
    assert payload["refusal"] == "recommendation"
    assert payload["claims"] == []
    assert payload["usage"]["cost"] == 0.0

    answered = serialize.answer(qa.Answer(text="Revenue rose.", seconds=1.0))
    assert answered["refusal"] is None


# --- the gate at the intent layer ---------------------------------------------


@pytest.fixture
def fake_tickers(monkeypatch):
    """AMD only, with no network - see conftest's no_network fixture."""
    rows = {"0": {"ticker": "AMD", "cik_str": 2488, "title": "Advanced Micro Devices Inc"}}
    monkeypatch.setattr(edgar, "load_ticker_map", lambda *a, **k: {"AMD": 2488})
    monkeypatch.setattr(edgar, "_raw_ticker_rows", lambda *a, **k: rows)
    intent_mod._name_index.cache_clear()
    yield
    intent_mod._name_index.cache_clear()


def test_advice_question_naming_a_ticker_still_serves_the_brief(fake_tickers):
    """"Is AMD a buy?" is a refusal AND a real information need about AMD."""
    parsed = intent_mod.classify("Is AMD a buy?", active=None)
    assert parsed.kind == "brief"
    assert parsed.ticker == "AMD"
    assert parsed.refusal is not None
    assert parsed.refusal.reason == "recommendation"


def test_advice_followup_on_the_active_ticker_is_flagged(fake_tickers):
    parsed = intent_mod.classify("should I hold it through earnings?", active="AMD")
    assert parsed.kind == "followup"
    assert parsed.refusal is not None


def test_ordinary_followup_carries_no_refusal(fake_tickers):
    parsed = intent_mod.classify("what are the risks?", active="AMD")
    assert parsed.kind == "followup"
    assert parsed.refusal is None
