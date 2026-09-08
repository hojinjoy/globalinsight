"""Replay mode - recorded synthesis, so the UI runs with zero API credits.

Waves 1-2 need nothing from here: 100+ MB of EDGAR, XBRL and filing text is
already on disk and serves offline. Only wave 3 (the paid call) is replayed.

Replay reproduces the recorded elapsed time, scaled by GI_FIXTURE_SPEED, so the
wave-3 wait state is actually exercised instead of shipping untested.
"""

import hashlib
import json
import os
import re
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent / "fixtures"


def enabled() -> bool:
    return os.getenv("GI_FIXTURES", "").strip() not in ("", "0", "false", "False")


def speed() -> float:
    try:
        value = float(os.getenv("GI_FIXTURE_SPEED", "1.0"))
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def available() -> list[str]:
    if not DIRECTORY.exists():
        return []
    return sorted(p.stem.upper() for p in DIRECTORY.glob("*.json"))


def load(ticker: str) -> dict | None:
    path = DIRECTORY / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def question_key(question: str) -> str:
    """Normalised hash, so wording drift still hits the recorded answer."""
    normalised = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.sha1(normalised.encode()).hexdigest()[:16]


def answer_for(fixture: dict, question: str) -> dict | None:
    answers = (fixture or {}).get("answers") or {}
    return answers.get(question_key(question)) or answers.get("_default")


def save(ticker: str, payload: dict) -> Path:
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = DIRECTORY / f"{ticker.upper()}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path
