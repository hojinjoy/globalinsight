"""Tests for globalinsight.store: the SQLite ticker/CIK store.

All HTTP is faked by monkeypatching globalinsight.http.get_json - store.py
never touches httpx directly. DB isolation comes from conftest.py's
autouse isolated_ticker_store fixture (store.DB_PATH -> tmp_path).
"""

import sqlite3

import pytest

from globalinsight import http, store
from globalinsight.models import Company

# Ten distinct tickers, eight distinct CIKs - two CIKs (Alphabet's and
# JPMorgan's) each mapped to more than one ticker, mirroring the real file's
# shape (1,449 CIKs -> multiple tickers; 8,010 distinct CIKs overall).
BASE_ROWS = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "3": {"cik_str": 1067983, "ticker": "BRK-A", "title": "BERKSHIRE HATHAWAY INC"},
    "4": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "5": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "6": {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"},
    "7": {"cik_str": 19617, "ticker": "JPM-PC", "title": "JPMORGAN CHASE & CO"},
    "8": {"cik_str": 320187, "ticker": "NKE", "title": "NIKE INC"},
    "9": {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"},
}


def _fake_get_json(rows):
    def _fn(url, ttl=None, headers=None):
        return rows
    return _fn


def _raw_query(sql, params=()):
    conn = sqlite3.connect(str(store.DB_PATH))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- refresh(): upsert, multi-ticker CIKs, soft delete, abort guard --------


def test_refresh_populates_all_rows(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    report = store.refresh()
    assert report.aborted is False
    assert report.fetched == len(BASE_ROWS)
    assert report.upserted == len(BASE_ROWS)
    assert store.row_count() == len(BASE_ROWS)


def test_multi_ticker_ciks_dont_collide(monkeypatch):
    """Alphabet (GOOGL/GOOG) and JPMorgan (JPM/JPM-PC) each map two tickers
    to one CIK - the table must keep both rows distinctly queryable rather
    than colliding on cik as if it were a primary key."""
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()

    googl = store.lookup("GOOGL")
    goog = store.lookup("GOOG")
    assert googl == Company(ticker="GOOGL", cik=1652044, name="Alphabet Inc.")
    assert goog == Company(ticker="GOOG", cik=1652044, name="Alphabet Inc.")
    assert googl.cik == goog.cik  # same company, two live tickers

    jpm = store.lookup("JPM")
    jpm_pref = store.lookup("JPM-PC")
    assert jpm.cik == jpm_pref.cik == 19617
    assert jpm.ticker != jpm_pref.ticker

    # Both rows for the shared CIK are independently present in the table.
    rows = _raw_query("SELECT ticker FROM companies WHERE cik = ?", (1652044,))
    assert {r[0] for r in rows} == {"GOOGL", "GOOG"}


def test_refresh_is_idempotent_upsert(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    store.refresh()
    assert store.row_count() == len(BASE_ROWS)

    # first_seen must survive a second upsert of the same ticker unchanged.
    rows = _raw_query("SELECT first_seen FROM companies WHERE ticker = 'NVDA'")
    assert len(rows) == 1


def test_soft_delete_never_removes_the_row(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    assert store.lookup("NKE") is not None

    shrunk = {k: v for k, v in BASE_ROWS.items() if v["ticker"] != "NKE"}
    monkeypatch.setattr(http, "get_json", _fake_get_json(shrunk))
    report = store.refresh()

    assert report.aborted is False
    assert report.deactivated == 1
    # Gone from the normal (active-only) lookup...
    assert store.lookup("NKE") is None
    # ...but never DELETEd - a previously generated brief citing NKE must
    # still be able to resolve it for audit.
    assert store.lookup("NKE", active_only=False) == Company(
        ticker="NKE", cik=320187, name="NIKE INC"
    )
    row = _raw_query("SELECT active FROM companies WHERE ticker = 'NKE'")
    assert row == [(0,)]
    # Total row count in the table is unchanged - nothing was deleted.
    assert store.row_count(active_only=False) == len(BASE_ROWS)
    assert store.row_count(active_only=True) == len(BASE_ROWS) - 1


def test_soft_deleted_ticker_reactivates_if_it_reappears(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    shrunk = {k: v for k, v in BASE_ROWS.items() if v["ticker"] != "NKE"}
    monkeypatch.setattr(http, "get_json", _fake_get_json(shrunk))
    store.refresh()
    assert store.lookup("NKE") is None

    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    assert store.lookup("NKE") is not None


def test_abort_guard_below_90_percent_leaves_table_untouched(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    assert store.row_count() == 10

    # A truncated/errored SEC response: only 2 of 10 rows (20%, well under
    # the 90% floor).
    truncated = {"0": BASE_ROWS["0"], "1": BASE_ROWS["1"]}
    monkeypatch.setattr(http, "get_json", _fake_get_json(truncated))
    report = store.refresh()

    assert report.aborted is True
    assert report.reason  # a loud, non-empty explanation
    assert report.upserted == 0
    assert report.deactivated == 0
    # Existing table is completely untouched, not wiped.
    assert store.row_count() == 10
    assert store.lookup("NKE") is not None
    assert store.lookup("AMZN") is not None


def test_abort_guard_allows_normal_small_churn(monkeypatch):
    """A drop of one ticker out of ten (90%) must NOT trip the guard - only
    a wholesale drop should."""
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    nine_of_ten = {k: v for k, v in BASE_ROWS.items() if k != "0"}
    monkeypatch.setattr(http, "get_json", _fake_get_json(nine_of_ten))
    report = store.refresh()
    assert report.aborted is False
    assert report.deactivated == 1


def test_abort_guard_does_not_apply_to_first_ever_refresh(monkeypatch):
    """An empty table (existing_active == 0) has nothing to compare a ratio
    against - the very first refresh must always populate it."""
    monkeypatch.setattr(http, "get_json", _fake_get_json({"0": BASE_ROWS["0"]}))
    report = store.refresh()
    assert report.aborted is False
    assert report.upserted == 1


# --- maybe_refresh(): cooldown -------------------------------------------


def test_maybe_refresh_runs_when_never_attempted(monkeypatch):
    calls = {"n": 0}

    def counting_get_json(url, ttl=None, headers=None):
        calls["n"] += 1
        return BASE_ROWS

    monkeypatch.setattr(http, "get_json", counting_get_json)
    report = store.maybe_refresh()
    assert report is not None
    assert calls["n"] == 1


def test_maybe_refresh_is_cooldown_guarded(monkeypatch):
    """A second maybe_refresh() call within the cooldown window must not
    hit SEC again - this is what protects against an advisor mistyping a
    ticker 50 times in a row."""
    calls = {"n": 0}

    def counting_get_json(url, ttl=None, headers=None):
        calls["n"] += 1
        return BASE_ROWS

    monkeypatch.setattr(http, "get_json", counting_get_json)
    first = store.maybe_refresh(cooldown=3600)
    second = store.maybe_refresh(cooldown=3600)
    assert first is not None
    assert second is None  # skipped by the cooldown
    assert calls["n"] == 1


def test_maybe_refresh_runs_again_after_cooldown_expires(monkeypatch):
    calls = {"n": 0}

    def counting_get_json(url, ttl=None, headers=None):
        calls["n"] += 1
        return BASE_ROWS

    monkeypatch.setattr(http, "get_json", counting_get_json)
    store.maybe_refresh(cooldown=0.0)
    store.maybe_refresh(cooldown=0.0)
    assert calls["n"] == 2


# --- resolve()'s miss -> refresh -> retry path, end to end -----------------


def test_edgar_resolve_miss_triggers_refresh_then_retries(monkeypatch):
    from globalinsight import edgar

    calls = {"n": 0}

    def counting_get_json(url, ttl=None, headers=None):
        calls["n"] += 1
        return BASE_ROWS

    monkeypatch.setattr(http, "get_json", counting_get_json)
    company = edgar.resolve("nvda")  # DB starts empty -> forces a refresh
    assert company == Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")
    assert calls["n"] == 1


def test_edgar_resolve_unknown_ticker_after_refresh_still_raises(monkeypatch):
    from globalinsight import edgar

    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    with pytest.raises(edgar.UnknownTicker):
        edgar.resolve("ZZZZNOTREAL")


def test_edgar_resolve_repeated_unknown_ticker_only_refreshes_once(monkeypatch):
    """The cooldown must span across separate resolve() calls, not just
    within one - otherwise 50 mistyped lookups still mean 50 refetches."""
    from globalinsight import edgar

    calls = {"n": 0}

    def counting_get_json(url, ttl=None, headers=None):
        calls["n"] += 1
        return BASE_ROWS

    monkeypatch.setattr(http, "get_json", counting_get_json)
    for _ in range(5):
        with pytest.raises(edgar.UnknownTicker):
            edgar.resolve("ZZZZNOTREAL")
    assert calls["n"] == 1


# --- search_by_name(): the main reason the table earns its place ----------


def test_search_by_name_case_insensitive_substring(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()

    results = store.search_by_name("nvidia")
    assert [c.ticker for c in results] == ["NVDA"]

    results = store.search_by_name("BERKSHIRE")
    assert {c.ticker for c in results} == {"BRK-A", "BRK-B"}


def test_search_by_name_no_match_returns_empty(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    assert store.search_by_name("totally not a company") == []


def test_search_by_name_respects_limit(monkeypatch):
    rows = {
        str(i): {"cik_str": 1000 + i, "ticker": f"T{i}", "title": f"Widget Co {i}"}
        for i in range(5)
    }
    monkeypatch.setattr(http, "get_json", _fake_get_json(rows))
    store.refresh()
    assert len(store.search_by_name("Widget", limit=2)) == 2
    assert len(store.search_by_name("Widget", limit=10)) == 5


def test_search_by_name_excludes_inactive_by_default(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    shrunk = {k: v for k, v in BASE_ROWS.items() if v["ticker"] != "NKE"}
    monkeypatch.setattr(http, "get_json", _fake_get_json(shrunk))
    store.refresh()

    assert store.search_by_name("NIKE") == []
    assert [c.ticker for c in store.search_by_name("NIKE", active_only=False)] == ["NKE"]


# --- schema shape -----------------------------------------------------------


def test_schema_matches_verified_constraints(monkeypatch):
    monkeypatch.setattr(http, "get_json", _fake_get_json(BASE_ROWS))
    store.refresh()
    cols = _raw_query("PRAGMA table_info(companies)")
    by_name = {c[1]: c for c in cols}  # (cid, name, type, notnull, dflt, pk)
    assert by_name["ticker"][5] == 1  # primary key
    assert by_name["cik"][5] == 0  # NOT the primary key
    assert by_name["cik"][3] == 1  # NOT NULL
    assert by_name["name"][3] == 1  # NOT NULL

    indexes = {row[1] for row in _raw_query("PRAGMA index_list(companies)")}
    # sqlite auto-names the PK index; just check our two explicit ones exist
    # by inspecting the schema SQL instead.
    schema_sql = _raw_query(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='companies'"
    )
    combined = " ".join(row[0] for row in schema_sql if row[0])
    assert "idx_companies_cik" in combined
    assert "idx_companies_name" in combined
