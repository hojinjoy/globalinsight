"""Compatibility shim - the implementation moved to ``api/qa.py``.

The Streamlit prototype (``app.py``) imports ``chatui.qa``; keeping this
re-export means the fallback demo keeps working unchanged after the move.
See UI-SPEC section 3.
"""

from api.qa import (  # noqa: F401
    MAX_TOKENS,
    Answer,
    AnswerTextStream,
    Claim,
    answer_question,
    refusal_answer,
)

__all__ = [
    "Answer",
    "AnswerTextStream",
    "Claim",
    "answer_question",
    "refusal_answer",
    "MAX_TOKENS",
]
