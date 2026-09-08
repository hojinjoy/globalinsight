"""SQLite-backed ticker -> CIK/company store, refreshed from SEC.

Before this module, the ticker -> CIK map (SEC's company_tickers.json, ~797
KB) lived only in http.py's URL cache: fast, but not queryable - no name
search, no way to see every ticker mapped to one CIK, no way to distinguish
"never heard of this ticker" from "this ticker used to exist and was
delisted". This module gives it a real, queryable home.

Verified shape of the source file (do not re-derive from memory/guesswork):
    - 10,415 ticker rows, but only 8,010 DISTINCT CIKs. 1,449 CIKs map to
      more than one ticker: share classes (BRK-A / BRK-B), multiple listings
      (Alphabet -> GOOGL/GOOG/GOOGM/GOOGN), and especially preferred stock
      (JPMorgan alone has 9 rows for its one CIK). So ``cik`` CANNOT be the
      primary key - ``ticker`` is unique and is.
    - Every row has a ``title`` (company name). Longest ticker is 7 chars.

Lookup flow (see edgar.resolve, which is the caller): a DB read
(``lookup()``) first; on a miss, there is no per-ticker SEC endpoint - the
company_tickers.json file is the *only* source - so a miss triggers one
full ``refresh()`` and one retry, guarded by ``maybe_refresh()``'s cooldown
so a mistyped ticker can't trigger repeated full refetches of SEC.

Soft delete, never hard delete: a ticker that drops out of SEC's file
(delisted, merged, whatever) is marked ``active=0``, never removed from the
table. A previously generated brief that cited that ticker must still be
able to resolve it for audit purposes (``lookup(ticker, active_only=False)``
or ``search_by_name(..., active_only=False)``); a real DELETE would make
that impossible.
"""

import sqlite3
import time
from dataclasses import dataclass
from datetime import date

from . import http
from .config import CACHE_DIR, TTL_TICKERS
from .models import Company

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Referenced dynamically (never captured as a function-default) so tests can
# monkeypatch store.DB_PATH and have every function in this module pick up
# the redirected path - see tests/conftest.py's isolated_ticker_store.
DB_PATH = CACHE_DIR / "tickers.db"

# If a refresh() fetch returns fewer than this fraction of the tickers
# currently marked active, treat it as a truncated/errored SEC response and
# abort rather than soft-deleting almost the entire table. 90%, not
# 99%, because SEC does add/remove a handful of tickers on an ordinary day;
# only a wholesale drop should trip this guard.
MIN_FETCH_RATIO = 0.90

# resolve()'s miss -> full refresh -> retry path (via maybe_refresh()) is
# throttled to at most one real refresh per this many seconds, so an
# advisor mistyping a ticker 50 times in a row can't trigger 50 full
# refetches of SEC's ticker file.
REFRESH_COOLDOWN_SECONDS = 3600.0

_LAST_REFRESH_KEY = "last_refresh_attempt"


@dataclass
class RefreshReport:
    """What one refresh() call did, for scripts/refresh_tickers.py to log."""

    fetched: int
    upserted: int
    deactivated: int
    aborted: bool = False
    reason: str = ""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            ticker     TEXT PRIMARY KEY,
            cik        INTEGER NOT NULL,
            name       TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_companies_cik  ON companies(cik);
        CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _today() -> str:
    return date.today().isoformat()


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def row_count(active_only: bool = True) -> int:
    """Number of rows currently in the table (active-only by default)."""
    conn = _connect()
    try:
        sql = "SELECT COUNT(*) FROM companies"
        if active_only:
            sql += " WHERE active = 1"
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def lookup(ticker: str, active_only: bool = True) -> Company | None:
    """Exact ticker lookup: ``SELECT ... WHERE ticker=? [AND active=1]``.

    Args:
        ticker: An already-uppercased ticker symbol (callers - edgar.resolve
            - are responsible for normalizing case/whitespace and trying
            alternate share-class spellings; this function does one exact
            match).
        active_only: True (default) for the normal "is this a live ticker"
            path. False for audit lookups that must still resolve a
            soft-deleted (delisted) ticker cited in a previously generated
            brief.

    Returns:
        The matching Company, or None.
    """
    conn = _connect()
    try:
        sql = "SELECT ticker, cik, name FROM companies WHERE ticker = ?"
        params: list = [ticker]
        if active_only:
            sql += " AND active = 1"
        row = conn.execute(sql, params).fetchone()
        return Company(ticker=row[0], cik=row[1], name=row[2]) if row else None
    finally:
        conn.close()


def search_by_name(query: str, limit: int = 10, active_only: bool = True) -> list[Company]:
    """Companies whose name contains ``query`` (case-insensitive substring).

    Advisors think in company names ("Nvidia", "berkshire"), not tickers -
    this is the main reason the table earns its place over the plain URL
    cache it replaced.

    Args:
        query: A substring to search for in the company name.
        limit: Max rows to return.
        active_only: True (default) excludes soft-deleted tickers.

    Returns:
        Matching Companies, ordered by name. One row per matching TICKER,
        so a company with several share classes/listings (e.g. Alphabet)
        can appear more than once, each with a different ticker.
    """
    conn = _connect()
    try:
        sql = "SELECT ticker, cik, name FROM companies WHERE name LIKE ? COLLATE NOCASE"
        params: list = [f"%{query.strip()}%"]
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [Company(ticker=r[0], cik=r[1], name=r[2]) for r in rows]
    finally:
        conn.close()


def fetch_rows(ttl: float | None = None) -> dict:
    """Raw company_tickers.json payload: {"0": {"cik_str", "ticker", "title"}, ...}.

    Goes through http.py's usual URL cache (24h TTL by default, same as
    edgar.TTL_TICKERS) - refresh()'s own cooldown (maybe_refresh) is what
    guards against hammering SEC, not bypassing this cache.
    """
    return http.get_json(TICKERS_URL, ttl=ttl if ttl is not None else TTL_TICKERS)


def refresh(ttl: float | None = None) -> RefreshReport:
    """Idempotent full upsert of the companies table from SEC's ticker file.

    - Present in the fetch -> upsert (cik, name, last_seen=today, active=1).
      ``first_seen`` is preserved on an update (only set on first insert).
    - Absent from the fetch but currently active in the DB -> soft delete
      (active=0). Never DELETE - see module docstring.
    - CRITICAL GUARD: if the fetch returns fewer than MIN_FETCH_RATIO of the
      currently-active row count, abort without writing anything and report
      it loudly (RefreshReport.aborted) - a truncated or errored SEC
      response must never silently wipe out ~10,415 rows.

    Safe to call from a cron job (scripts/refresh_tickers.py) or from
    edgar.resolve()'s miss path (via maybe_refresh(), which adds a cooldown
    on top of this).
    """
    conn = _connect()
    try:
        raw = fetch_rows(ttl)
        fetched_rows = list(raw.values())
        existing_active = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE active = 1"
        ).fetchone()[0]

        if existing_active > 0 and len(fetched_rows) < existing_active * MIN_FETCH_RATIO:
            reason = (
                f"refresh aborted: fetched {len(fetched_rows)} tickers, fewer than "
                f"{MIN_FETCH_RATIO:.0%} of the {existing_active} currently active in "
                "the store - this looks like a truncated or errored SEC response, "
                "not a real drop in registered tickers. Existing table left untouched."
            )
            return RefreshReport(
                fetched=len(fetched_rows), upserted=0, deactivated=0,
                aborted=True, reason=reason,
            )

        today = _today()
        seen: set[str] = set()
        upserted = 0
        deactivated = 0
        with conn:
            for row in fetched_rows:
                ticker = str(row["ticker"]).upper()
                cik = int(row["cik_str"])
                name = str(row["title"])
                seen.add(ticker)
                conn.execute(
                    """
                    INSERT INTO companies (ticker, cik, name, first_seen, last_seen, active)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(ticker) DO UPDATE SET
                        cik = excluded.cik,
                        name = excluded.name,
                        last_seen = excluded.last_seen,
                        active = 1
                    """,
                    (ticker, cik, name, today, today),
                )
                upserted += 1

            currently_active = {
                r[0] for r in conn.execute(
                    "SELECT ticker FROM companies WHERE active = 1"
                ).fetchall()
            }
            to_deactivate = currently_active - seen
            deactivated = len(to_deactivate)
            if to_deactivate:
                conn.executemany(
                    "UPDATE companies SET active = 0 WHERE ticker = ?",
                    [(t,) for t in to_deactivate],
                )

        return RefreshReport(fetched=len(fetched_rows), upserted=upserted, deactivated=deactivated)
    finally:
        conn.close()


def maybe_refresh(cooldown: float = REFRESH_COOLDOWN_SECONDS) -> RefreshReport | None:
    """Run refresh() unless the last attempt was within ``cooldown`` seconds.

    This is the guard edgar.resolve()'s miss path calls: without it, an
    advisor mistyping a ticker 50 times in a row would trigger 50 full
    refetches of SEC's 797 KB ticker file (there's no per-ticker endpoint to
    fall back to - the whole-file fetch is the only source).

    Returns:
        None if skipped by the cooldown. Otherwise the RefreshReport from
        the refresh that ran (including an aborted one - the <90% guard
        tripping still counts as "a refresh attempt happened").
    """
    conn = _connect()
    try:
        now = time.time()
        last = _get_meta(conn, _LAST_REFRESH_KEY)
        if last is not None and (now - float(last)) < cooldown:
            return None
        with conn:
            _set_meta(conn, _LAST_REFRESH_KEY, str(now))
    finally:
        conn.close()
    return refresh()
