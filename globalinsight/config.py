"""Central, env-driven configuration. No secrets live in code.

Every tunable referenced by the empirical facts in the design doc (SEC
User-Agent, rate limit, cache TTLs, 8-K tier caps) is collected here so a
future reader has one place to check before "fixing" a magic number.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a convenience, not a hard dep
    pass

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.getenv("GI_CACHE_DIR", ROOT / ".cache"))

# SEC requires a User-Agent naming a real contact address; a generic browser
# string (or no contact info) gets a 403, sometimes at the IP level. See
# empirical fact #1 in the design doc.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "GlobalInsight Research your-email@example.com"
)

# SEC's documented rate limit is 10 requests/second, enforced globally (not
# per-connection), so the limiter in http.py must be a single shared token
# bucket across every thread - a per-call sleep() under-throttles as soon as
# more than one thread is in flight (empirical fact #2).
SEC_RATE_PER_SEC = 10.0

# Cache TTLs, in seconds. None means "cache forever" - used for anything under
# /Archives/, since a filed document is immutable the moment it's accepted.
TTL_QUOTE = 60
TTL_SUBMISSIONS = 24 * 3600
TTL_COMPANYFACTS = 24 * 3600
TTL_TICKERS = 24 * 3600
TTL_PERMANENT = None

# 8-K tiering (empirical fact re: tier policy). Item 2.02 (earnings) is always
# worth the tokens; 5.02/1.01/2.03 are worth fetching but capped; 8.01/7.01/
# 5.07 are catch-all/procedural items that are frequently pure boilerplate (one
# NVDA 8.01 measured at 92,430 tokens - 72% of all 8-K content that quarter -
# and was boilerplate) so their bodies are never fetched, only their metadata.
TIER1_ITEMS = {"2.02"}
TIER1_CAP_TOKENS = 40_000
TIER2_ITEMS = {"5.02", "1.01", "2.03"}
TIER2_CAP_TOKENS = 25_000
TIER3_ITEMS = {"8.01", "7.01", "5.07"}

MAX_8K_WORKERS = 5  # bounded fan-out pool for 8-K document fetches

# Claude synthesis call parameters (see synthesize.py).
SYNTHESIS_MODEL = "claude-opus-5"
SYNTHESIS_EFFORT = "high"
SYNTHESIS_MAX_TOKENS = 16_000
