"""Tests for globalinsight.financials: XBRL companyfacts -> financial trend.

Priority 1 test (test_revenue_concept_selection_prefers_most_recent_data) is
a REGRESSION TEST FOR A CONFIRMED, CURRENTLY-UNFIXED BUG. It is expected to
FAIL against the current implementation - see its docstring. Do not "fix" it
by weakening the assertion; the failure is the point.
"""

import pytest

from globalinsight import financials as financials_mod
from globalinsight import http
from globalinsight.models import Citation, FinancialPoint, FinancialSeries

CIK = 1045810


def _entry(year: int, val: float, form: str, filed: str, accn: str) -> dict:
    """One companyfacts unit entry for a clean, full fiscal year ending in
    December (so fiscal_year == end.year per financials._annual_points, since
    end.month=12 > 5), ~364 days long (within the 300-400 day annual window).
    """
    return {
        "start": f"{year}-01-01",
        "end": f"{year}-12-31",
        "val": val,
        "form": form,
        "filed": filed,
        "accn": accn,
        "fy": year,
        "fp": "FY",
    }


def _companyfacts(us_gaap: dict) -> dict:
    return {"facts": {"us-gaap": us_gaap}}


# --- Priority 1: confirmed production bug ------------------------------------


def test_revenue_concept_selection_prefers_most_recent_data(monkeypatch):
    """Regression test for the NVDA revenue bug.

    `uv run python -m globalinsight NVDA` currently returns Revenue for
    fiscal years [2019, 2020, 2021] cited to a filing dated 2022-03-18, while
    NetIncomeLoss/GrossProfit/EPS correctly return [2023, 2024, 2025] from the
    2026 filing - an advisor would see NVIDIA revenue of ~$10.9B instead of
    ~$130B.

    Root cause: financials.get_financials() tries Revenue's candidate concept
    tags in a fixed preference order and takes the FIRST tag that has *any*
    annual data point, rather than the tag holding the most RECENT data. This
    fixture models that exactly: the first-tried tag
    (RevenueFromContractWithCustomerExcludingAssessedTax) is deprecated for
    this filer and only holds old data (FY2019-2021, last filed 2022-03-18);
    the correct, current data (FY2023-2025, filed with the 2026 annual
    report) lives under a tag tried later
    (RevenueFromContractWithCustomerIncludingAssessedTax). Because the loop
    breaks on the first tag with any points, it never looks at the later tag.

    THIS TEST IS EXPECTED TO FAIL against the current financials.py. That is
    correct and intentional - it demonstrates the bug. Do not weaken this
    assertion, and do not modify globalinsight/financials.py to satisfy it;
    fixing the source is a separate piece of work.
    """
    us_gaap = {
        # Deprecated tag: tried FIRST, holds only stale years.
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {
                "USD": [
                    _entry(2019, 10_918_000_000, "10-K", "2020-03-01", "acc-2020-old"),
                    _entry(2020, 16_675_000_000, "10-K", "2021-03-01", "acc-2021-old"),
                    _entry(2021, 26_914_000_000, "10-K", "2022-03-18", "acc-2022-old"),
                ]
            }
        },
        # Current tag: tried SECOND, holds the correct recent years - but the
        # buggy code never gets here because the first tag already had data.
        "RevenueFromContractWithCustomerIncludingAssessedTax": {
            "units": {
                "USD": [
                    _entry(2023, 60_922_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2024, 96_307_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2025, 130_497_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _entry(2023, 29_760_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2024, 42_600_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2025, 65_800_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                ]
            }
        },
        "GrossProfit": {
            "units": {
                "USD": [
                    _entry(2023, 44_301_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2024, 68_400_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2025, 95_700_000_000, "10-K", "2026-03-15", "acc-2026-annual"),
                ]
            }
        },
        "EarningsPerShareDiluted": {
            "units": {
                "USD/shares": [
                    _entry(2023, 1.19, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2024, 1.72, "10-K", "2026-03-15", "acc-2026-annual"),
                    _entry(2025, 2.66, "10-K", "2026-03-15", "acc-2026-annual"),
                ]
            }
        },
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))

    result = financials_mod.get_financials(CIK, years=3)

    # Sanity: the other concepts correctly land on the recent 2026 filing.
    assert [p.fiscal_year for p in result["NetIncomeLoss"].points] == [2023, 2024, 2025]
    assert [p.fiscal_year for p in result["GrossProfit"].points] == [2023, 2024, 2025]
    assert [p.fiscal_year for p in result["EPS"].points] == [2023, 2024, 2025]

    # The bug: Revenue should ALSO be [2023, 2024, 2025] from the 2026
    # filing, matching every other concept - not the stale 2019-2021 data
    # from the deprecated tag's 2022-03-18 filing.
    revenue_years = [p.fiscal_year for p in result["Revenue"].points]
    assert revenue_years == [2023, 2024, 2025], (
        f"BUG: Revenue picked the first concept tag with ANY data "
        f"(deprecated, stale) instead of the most recent data. "
        f"Got fiscal years {revenue_years} cited to "
        f"{result['Revenue'].points[-1].citation.filed_date!r} instead of "
        f"2023-2025 cited to the 2026-03-15 filing."
    )


# --- C2: hard recency gate (single-tag stale concept) ------------------------


def test_gross_profit_marked_stale_relative_to_other_concepts(monkeypatch):
    """C2 regression test for the ORCL bug: a SINGLE-TAG concept (GrossProfit
    has no fallback tags, unlike Revenue) that a filer simply stopped
    reporting years ago must be flagged stale, not rendered beside a
    current Revenue/NetIncomeLoss as if it were contemporaneous.

    This is a DISTINCT defect from C1: there is only one candidate tag here,
    so tag-selection ordering cannot be the cause, and a C1-only fix would
    not catch it. An advisor computing a margin from what's on screen would
    otherwise divide a fiscal-2025 revenue by an 8-years-stale fiscal-2017
    gross profit.

    The stale series is kept (not dropped) with ``stale=True`` set, so a
    consuming UI can render "GrossProfit not reported in recent filings"
    explicitly rather than a missing key looking identical to "this filer
    never reports GrossProfit at all" or to a fetch failure.
    """
    us_gaap = {
        "Revenues": {
            "units": {
                "USD": [
                    _entry(2023, 52_960_000_000, "10-K", "2023-06-20", "acc-2023"),
                    _entry(2024, 57_400_000_000, "10-K", "2024-06-20", "acc-2024"),
                    _entry(2025, 67_360_000_000, "10-K", "2025-06-20", "acc-2025"),
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {"USD": [_entry(2025, 12_400_000_000, "10-K", "2025-06-20", "acc-2025")]}
        },
        # Stale: this filer stopped tagging GrossProfit after fiscal 2017,
        # eight years before the current Revenue/NetIncomeLoss.
        "GrossProfit": {
            "units": {
                "USD": [
                    _entry(2015, 22_410_000_000, "10-K", "2015-06-20", "acc-2015"),
                    _entry(2016, 23_020_000_000, "10-K", "2016-06-20", "acc-2016"),
                    _entry(2017, 24_290_000_000, "10-K", "2017-06-20", "acc-2017"),
                ]
            }
        },
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK, years=3)

    assert "Revenue" in result
    assert "NetIncomeLoss" in result
    assert "GrossProfit" in result, "a stale series must still be returned, just flagged"
    assert result["Revenue"].stale is False
    assert result["NetIncomeLoss"].stale is False
    assert result["GrossProfit"].stale is True, (
        "BUG: a GrossProfit series 8 fiscal years stale relative to Revenue "
        "was rendered beside current data without being flagged stale"
    )
    # The actual (stale) figures are still available for a UI that wants
    # to show them with a warning rather than hide them outright.
    assert [p.fiscal_year for p in result["GrossProfit"].points] == [2015, 2016, 2017]


def test_series_not_marked_stale_when_within_one_year_lag(monkeypatch):
    """Sanity check on the C2 gate: a concept only one fiscal year behind
    the newest (a normal, non-stale lag - e.g. a concept whose latest 10-K
    data simply hasn't posted yet) must NOT be flagged stale."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {"USD": [_entry(2025, 100, "10-K", "2025-03-01", "acc-2025")]}
        },
        "GrossProfit": {
            "units": {"USD": [_entry(2024, 200, "10-K", "2024-03-01", "acc-2024")]}
        },
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert result["NetIncomeLoss"].stale is False
    assert result["GrossProfit"].stale is False
    assert "NetIncomeLoss" in result
    assert "GrossProfit" in result


# --- H1: fiscal-year labels derived from the filer's own filing metadata ----
#
# dei:DocumentFiscalYearFocus (the textbook source) turns out not to be
# exposed by SEC's companyfacts API in practice (verified against live
# NVDA/WMT/AAPL/ORCL data - see financials._fiscal_year_labels' docstring),
# so the fix instead trusts each filing's own `fy` metadata field, but ONLY
# for the one entry in that filing whose period is genuinely current (the
# latest `end` date within that accession) - never applied uniformly across
# an accession's comparative periods, which is the exact misuse the task
# warns against.


def _gaap_entry(start, end, val, form, filed, accn, fy):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "accn": accn, "fy": fy}


def test_fiscal_year_label_for_january_fiscal_year_end(monkeypatch):
    """H1 regression: NVDA/WMT-shape January fiscal-year-end. The issuer's
    own filing labels the year ended 2026-01-25 as fiscal 2026 (fy=2026 on
    its own current-period entry) - the old `end.month > 5` heuristic would
    label it FY2025, contradicting the very filing being cited."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _gaap_entry(
                        "2025-01-27", "2026-01-25", 65_800_000_000,
                        "10-K", "2026-02-25", "acc-nvda-fy26", fy=2026,
                    )
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert result["NetIncomeLoss"].points[0].fiscal_year == 2026


def test_fiscal_year_label_for_may_fiscal_year_end(monkeypatch):
    """H1 regression: ORCL-shape May fiscal-year-end - lands exactly on the
    old `month > 5` cutoff boundary. Oracle labels its year ended
    2026-05-31 as fiscal 2026, not FY2025."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _gaap_entry(
                        "2025-06-01", "2026-05-31", 12_000_000_000,
                        "10-K", "2026-06-22", "acc-orcl-fy26", fy=2026,
                    )
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert result["NetIncomeLoss"].points[0].fiscal_year == 2026


def test_fiscal_year_label_for_june_fiscal_year_end(monkeypatch):
    """H1 shape check: MSFT-shape June fiscal-year-end already matched the
    old month-based heuristic by coincidence; confirm the fix keeps it
    correct rather than regressing it."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _gaap_entry(
                        "2025-07-01", "2026-06-30", 90_000_000_000,
                        "10-K", "2026-07-30", "acc-msft-fy26", fy=2026,
                    )
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert result["NetIncomeLoss"].points[0].fiscal_year == 2026


def test_fiscal_year_label_ignores_current_filing_fy_for_comparative_periods(monkeypatch):
    """The specific misuse the task warns against: every entry in a 10-K
    (current period AND prior-year comparatives) carries the SAME `fy`
    value in real SEC data. Naively trusting `fy` per-entry would relabel
    the two comparative years as fiscal 2026 too. Only the entry whose
    period actually ends latest within its own accession may be trusted;
    the earlier comparative periods must be labeled from the filing that
    was current when THEY were reported.
    """
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    # All three periods below are filed together in NVDA's
                    # fiscal-2026 10-K and all carry fy=2026 - exactly like
                    # live SEC data - but only the 2026-01-25 period is
                    # actually fiscal 2026.
                    _gaap_entry(
                        "2023-01-30", "2024-01-28", 29_760_000_000,
                        "10-K", "2026-02-25", "acc-fy26-10k", fy=2026,
                    ),
                    _gaap_entry(
                        "2024-01-29", "2025-01-26", 72_880_000_000,
                        "10-K", "2026-02-25", "acc-fy26-10k", fy=2026,
                    ),
                    _gaap_entry(
                        "2025-01-27", "2026-01-25", 120_067_000_000,
                        "10-K", "2026-02-25", "acc-fy26-10k", fy=2026,
                    ),
                    # The prior year's own 10-K, filed a year earlier, whose
                    # own current period was 2025-01-26 - correctly fy=2025.
                    _gaap_entry(
                        "2023-01-30", "2024-01-28", 29_760_000_000,
                        "10-K", "2025-02-26", "acc-fy25-10k", fy=2025,
                    ),
                    _gaap_entry(
                        "2024-01-29", "2025-01-26", 72_880_000_000,
                        "10-K", "2025-02-26", "acc-fy25-10k", fy=2025,
                    ),
                    # And the filing before THAT, whose own current period
                    # was 2024-01-28 - correctly fy=2024. Without this
                    # anchor, 2024-01-28 never has a filing where it was
                    # the current period, and there'd be nothing to trust.
                    _gaap_entry(
                        "2023-01-30", "2024-01-28", 29_760_000_000,
                        "10-K", "2024-02-21", "acc-fy24-10k", fy=2024,
                    ),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK, years=3)
    points_by_year = {p.fiscal_year: p.value for p in result["NetIncomeLoss"].points}
    assert points_by_year == {
        2024: 29_760_000_000,
        2025: 72_880_000_000,
        2026: 120_067_000_000,
    }


def test_fiscal_year_label_reproduces_opposite_naming_convention(monkeypatch):
    """Target-shape check: some Jan/Feb fiscal-year-end filers use the
    OPPOSITE convention from NVDA/WMT/ORCL, labeling by the calendar year
    the period mostly overlaps rather than the year it ends in (Target's
    real fy=2023 for the period ended 2024-02-03). No fixed month cutoff -
    old or new - can get both conventions right; only per-filer data can,
    which is exactly what this fix uses."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _gaap_entry(
                        "2023-01-29", "2024-02-03", 2_780_000_000,
                        "10-K", "2024-03-13", "acc-tgt-fy23", fy=2023,
                    )
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert result["NetIncomeLoss"].points[0].fiscal_year == 2023


def test_fiscal_year_label_for_december_year_end_ignores_bad_fy_metadata(monkeypatch):
    """Regression guard found while verifying H1 against live data: SHOPIFY's
    real companyfacts has an entry for its period ended 2023-12-31 tagged
    with fy=2022 (a genuine SEC/filer metadata quirk, not a period-labeling
    convention question) that is also the max-end entry within its own
    accession - so a fy-trust lookup applied unconditionally would mislabel
    a Dec-year-end period, which is never actually ambiguous. The fix scopes
    the fy-trust lookup to Jan-May period ends only; Jun-Dec period ends
    always use the (unambiguous) ending calendar year."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _gaap_entry(
                        "2022-01-01", "2022-12-31", 132_000_000,
                        "10-K", "2023-02-16", "acc-shop-2022", fy=2022,
                    ),
                    # Real SHOP data shape: this filing's own fy metadata
                    # (2022) is wrong for a period ending 2023-12-31, but
                    # since the period ends in December the lookup must
                    # never be consulted for it in the first place.
                    _gaap_entry(
                        "2023-01-01", "2023-12-31", 2_019_000_000,
                        "10-K", "2024-02-13", "acc-shop-2023", fy=2022,
                    ),
                    _gaap_entry(
                        "2024-01-01", "2024-12-31", 1_231_000_000,
                        "10-K", "2025-02-11", "acc-shop-2024", fy=2024,
                    ),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK, years=3)
    years = [p.fiscal_year for p in result["NetIncomeLoss"].points]
    assert years == [2022, 2023, 2024], (
        f"BUG: a Dec-year-end period was mislabeled from bad fy metadata "
        f"instead of using its unambiguous ending calendar year; got {years}"
    )


# --- H2: IFRS foreign private issuer fallback ---------------------------------


def test_ifrs_fallback_for_non_usd_foreign_private_issuer(monkeypatch):
    """H2 regression: an IFRS 20-F filer (TSM/ASML/SAP-shape) exposes facts
    only under ifrs-full, in its own reporting currency (TWD here) - not
    us-gaap/USD. Previously financials.py hardcoded us-gaap + USD, so every
    such filer silently yielded {} with no financials and no warning, even
    though _ANNUAL_FORMS already listed 20-F/40-F as annual forms."""
    ifrs_full = {
        "Revenue": {
            "units": {
                "TWD": [
                    _entry(2023, 2_161_700_000_000, "20-F", "2024-04-15", "acc-2024"),
                    _entry(2024, 2_894_300_000_000, "20-F", "2025-04-15", "acc-2025"),
                    _entry(2025, 3_200_000_000_000, "20-F", "2026-04-15", "acc-2026"),
                ]
            }
        },
        "ProfitLoss": {
            "units": {"TWD": [_entry(2025, 1_400_000_000_000, "20-F", "2026-04-15", "acc-2026")]}
        },
        "BasicEarningsLossPerShare": {
            "units": {"TWD/shares": [_entry(2025, 54.0, "20-F", "2026-04-15", "acc-2026")]}
        },
    }
    monkeypatch.setattr(
        http, "get_json", lambda *a, **k: {"facts": {"ifrs-full": ifrs_full}}
    )
    result = financials_mod.get_financials(CIK, years=3)

    assert [p.fiscal_year for p in result["Revenue"].points] == [2023, 2024, 2025]
    assert result["Revenue"].unit == "TWD"
    assert result["NetIncomeLoss"].unit == "TWD"
    assert result["NetIncomeLoss"].points[0].value == 1_400_000_000_000
    assert result["EPS"].unit == "TWD/shares"


# --- _annual_points filtering -------------------------------------------------


def test_annual_points_skips_non_annual_forms(monkeypatch):
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _entry(2024, 999, "10-Q", "2024-11-01", "acc-q3"),  # not annual
                    _entry(2024, 42_600_000_000, "10-K", "2025-03-01", "acc-annual"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert len(result["NetIncomeLoss"].points) == 1
    assert result["NetIncomeLoss"].points[0].value == 42_600_000_000


def test_annual_points_skips_non_annual_duration(monkeypatch):
    """A ~90-day quarterly entry tagged under an annual-eligible concept must
    not be mistaken for a full fiscal year, even if its form is 10-K (e.g. a
    10-K sometimes carries comparative quarterly figures)."""
    us_gaap = {
        "GrossProfit": {
            "units": {
                "USD": [
                    {
                        "start": "2024-10-01",
                        "end": "2024-12-31",  # ~92 days: quarterly, not annual
                        "val": 111,
                        "form": "10-K",
                        "filed": "2025-03-01",
                        "accn": "acc-q",
                    },
                    _entry(2024, 68_400_000_000, "10-K", "2025-03-01", "acc-annual"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert len(result["GrossProfit"].points) == 1
    assert result["GrossProfit"].points[0].value == 68_400_000_000


def test_annual_points_restatement_prefers_later_filed_value(monkeypatch):
    """Two entries for the same fiscal year, both on a plain 10-K: the
    later-filed one is a restatement and must win over the originally
    reported figure."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _entry(2023, 100, "10-K", "2024-03-01", "acc-original"),
                    _entry(2023, 105, "10-K", "2024-06-01", "acc-restated"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert len(result["NetIncomeLoss"].points) == 1
    assert result["NetIncomeLoss"].points[0].value == 105
    assert result["NetIncomeLoss"].points[0].citation.accession == "acc-restated"


def test_annual_points_skips_entries_missing_start_or_end(monkeypatch):
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    {
                        "start": None,
                        "end": "2024-12-31",
                        "val": 999,
                        "form": "10-K",
                        "filed": "2025-03-01",
                        "accn": "acc-bad",
                    },
                    _entry(2024, 42_600_000_000, "10-K", "2025-03-01", "acc-good"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert len(result["NetIncomeLoss"].points) == 1
    assert result["NetIncomeLoss"].points[0].citation.accession == "acc-good"


def test_annual_points_skips_entries_with_unparseable_dates(monkeypatch):
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    {
                        "start": "not-a-date",
                        "end": "2024-12-31",
                        "val": 999,
                        "form": "10-K",
                        "filed": "2025-03-01",
                        "accn": "acc-bad",
                    },
                    _entry(2024, 42_600_000_000, "10-K", "2025-03-01", "acc-good"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert len(result["NetIncomeLoss"].points) == 1
    assert result["NetIncomeLoss"].points[0].citation.accession == "acc-good"


def test_filing_index_url_empty_when_no_accession(monkeypatch):
    """A financial point with no accession number gets no citation URL
    (rather than a malformed one) - exercised via a companyfacts entry
    that omits 'accn' entirely."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    {
                        "start": "2024-01-01",
                        "end": "2024-12-31",
                        "val": 100,
                        "form": "10-K",
                        "filed": "2025-03-01",
                        # no "accn" key at all
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    point = result["NetIncomeLoss"].points[0]
    assert point.citation.accession == ""
    assert point.citation.url == ""


def test_get_financials_missing_concept_is_absent_not_error(monkeypatch):
    """A filer that never breaks out GrossProfit simply has no 'GrossProfit'
    key in the result - never a raised error or a null placeholder."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {"USD": [_entry(2024, 42_600_000_000, "10-K", "2025-03-01", "acc")]}
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)
    assert "GrossProfit" not in result
    assert "Revenue" not in result
    assert "NetIncomeLoss" in result


def test_get_financials_propagates_companyfacts_fetch_failure(monkeypatch):
    """H4 regression test: a failed companyfacts fetch must PROPAGATE, not
    silently degrade to an empty dict.

    Previously get_financials() swallowed every exception from the
    companyfacts fetch and returned {}, which made a genuine SEC outage
    indistinguishable from "this filer simply has no XBRL data" - the
    advisor brief would render with no numbers and no visible sign anything
    was wrong, and pipeline.wave2()'s `errors["financials"]` handler could
    never actually fire. Letting the exception propagate lets wave2's
    existing try/except record it in errors, while wave2's failure
    isolation (tested in test_pipeline.py) keeps the rest of the brief from
    being blanked.
    """

    def boom(*a, **k):
        raise RuntimeError("404 not found")

    monkeypatch.setattr(http, "get_json", boom)
    with pytest.raises(RuntimeError, match="404 not found"):
        financials_mod.get_financials(CIK)


def test_get_financials_uses_companyfacts_ttl(monkeypatch):
    from globalinsight import config

    seen = {}

    def fake_get_json(url, ttl=None, headers=None):
        seen["url"] = url
        seen["ttl"] = ttl
        return _companyfacts({})

    monkeypatch.setattr(http, "get_json", fake_get_json)
    financials_mod.get_financials(CIK)
    assert seen["ttl"] == config.TTL_COMPANYFACTS
    assert seen["url"] == f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json"


def test_get_financials_respects_years_window(monkeypatch):
    us_gaap = {
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _entry(2021, 1, "10-K", "2022-03-01", "a1"),
                    _entry(2022, 2, "10-K", "2023-03-01", "a2"),
                    _entry(2023, 3, "10-K", "2024-03-01", "a3"),
                    _entry(2024, 4, "10-K", "2025-03-01", "a4"),
                ]
            }
        }
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK, years=3)
    assert [p.fiscal_year for p in result["NetIncomeLoss"].points] == [2022, 2023, 2024]


# --- absolute staleness (new): submissions.json vs. companyfacts.json -------
#
# _mark_stale_series (C2, above) is purely RELATIVE - it only catches a
# concept lagging behind the filer's *other* concepts. A filer whose entire
# companyfacts payload is uniformly one year behind (every concept agrees
# with every other) sails through it with nothing flagged. Confirmed live:
# TSM's companyfacts currently tops out at FY2024 (filed 2025-04-17) even
# though TSM filed a FY2025 20-F on 2026-04-16 that SEC hasn't back-filled
# XBRL facts for yet. mark_absolute_staleness cross-references
# submissions.json (which DOES know about the newer 20-F) to catch this.


def _submissions(forms, accessions, filing_dates, report_dates, cik=1046179):
    n = len(forms)
    return {
        "cik": str(cik),
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions,
                "filingDate": filing_dates,
                "reportDate": report_dates,
                "primaryDocument": ["doc.htm"] * n,
                "items": [""] * n,
            }
        },
    }


def test_absolute_staleness_flagged_when_submissions_show_newer_annual_filing(monkeypatch):
    """TSM regression fixture: companyfacts tops out at FY2024 (filed
    2025-04-17), but submissions.json shows a FY2025 20-F already filed
    (2026-04-16, period ended 2025-12-31) that companyfacts hasn't caught up
    to yet. Every concept here agrees with every other (both stop at
    FY2024), so the relative check (_mark_stale_series) has nothing to
    flag - only the absolute, submissions-aware check can catch this."""
    ifrs_full = {
        "Revenue": {
            "units": {
                "TWD": [
                    _entry(2023, 2_161_700_000_000, "20-F", "2024-04-15", "acc-2024"),
                    _entry(2024, 2_894_300_000_000, "20-F", "2025-04-17", "acc-2025"),
                ]
            }
        },
        "ProfitLoss": {
            "units": {"TWD": [_entry(2024, 1_100_000_000_000, "20-F", "2025-04-17", "acc-2025")]}
        },
    }
    monkeypatch.setattr(
        http, "get_json", lambda *a, **k: {"facts": {"ifrs-full": ifrs_full}}
    )
    result = financials_mod.get_financials(CIK, years=3)
    assert result["Revenue"].stale is False  # nothing looks relatively stale yet
    assert result["NetIncomeLoss"].stale is False

    submissions = _submissions(
        forms=["20-F", "20-F"],
        accessions=["acc-2025", "acc-2026"],
        filing_dates=["2025-04-17", "2026-04-16"],
        report_dates=["2024-12-31", "2025-12-31"],
    )

    financials_mod.mark_absolute_staleness(result, submissions)

    assert result["Revenue"].stale is True, (
        "BUG: SEC's submissions.json shows a FY2025 20-F already filed, but "
        "companyfacts still tops out at FY2024 - this uniform, filer-wide "
        "lag must be flagged even though nothing looks relatively stale"
    )
    assert result["NetIncomeLoss"].stale is True
    # The actual (lagging) figures are still returned, just flagged.
    assert result["Revenue"].points[-1].fiscal_year == 2024


def test_absolute_staleness_not_flagged_when_data_matches_latest_annual_filing(monkeypatch):
    """Sanity check: when companyfacts already reflects the newest annual
    filing on record, the absolute check must not spuriously flag it."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {"USD": [_entry(2025, 100, "10-K", "2026-03-01", "acc-2026")]}
        },
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)

    submissions = _submissions(
        forms=["10-K"], accessions=["acc-2026"], filing_dates=["2026-03-01"],
        report_dates=["2025-12-31"],
    )
    financials_mod.mark_absolute_staleness(result, submissions)

    assert result["NetIncomeLoss"].stale is False


def test_absolute_staleness_noop_when_submissions_has_no_annual_filing():
    """No 10-K/20-F/40-F in submissions.json at all (e.g. an ETF/fund) ->
    nothing to compare against, so the absolute check must be a no-op
    rather than flagging (or erroring on) the absence of a signal."""
    result = {
        "NetIncomeLoss": FinancialSeries(
            concept="NetIncomeLoss", unit="USD",
            points=[
                FinancialPoint(
                    fiscal_year=2025, value=1.0,
                    citation=Citation(form="10-K", item="", filed_date="2026-03-01",
                                       accession="acc", url=""),
                )
            ],
        )
    }
    submissions = _submissions(forms=["497"], accessions=["a"], filing_dates=["2026-01-01"],
                                report_dates=[""])
    financials_mod.mark_absolute_staleness(result, submissions)
    assert result["NetIncomeLoss"].stale is False


def test_absolute_staleness_noop_on_empty_financials_result():
    """An empty financials dict (e.g. a filer with no XBRL data at all) must
    not error out of the absolute staleness check."""
    submissions = _submissions(
        forms=["10-K"], accessions=["acc"], filing_dates=["2026-03-01"],
        report_dates=["2025-12-31"],
    )
    result: dict = {}
    assert financials_mod.mark_absolute_staleness(result, submissions) == {}


def test_latest_annual_report_fiscal_year_picks_most_recently_filed():
    submissions = _submissions(
        forms=["10-K", "10-K", "8-K"],
        accessions=["acc-old", "acc-new", "acc-8k"],
        filing_dates=["2025-03-01", "2026-03-01", "2026-06-01"],
        report_dates=["2024-12-31", "2025-12-31", ""],
    )
    assert financials_mod.latest_annual_report_fiscal_year(submissions) == 2025


def test_latest_annual_report_fiscal_year_none_when_no_annual_forms():
    submissions = _submissions(
        forms=["8-K", "497"], accessions=["a", "b"],
        filing_dates=["2026-01-01", "2026-02-01"], report_dates=["", ""],
    )
    assert financials_mod.latest_annual_report_fiscal_year(submissions) is None


# --- citation compliance -----------------------------------------------------


def test_every_financial_point_carries_a_complete_citation(monkeypatch):
    """A financial value with no citation is a compliance failure - this
    asserts it cannot happen for well-formed SEC companyfacts data."""
    us_gaap = {
        "NetIncomeLoss": {
            "units": {"USD": [_entry(2024, 100, "10-K", "2025-03-01", "acc-1")]}
        },
        "GrossProfit": {
            "units": {"USD": [_entry(2024, 200, "10-K", "2025-03-01", "acc-2")]}
        },
    }
    monkeypatch.setattr(http, "get_json", lambda *a, **k: _companyfacts(us_gaap))
    result = financials_mod.get_financials(CIK)

    checked_any = False
    for series in result.values():
        for point in series.points:
            checked_any = True
            citation = point.citation
            assert citation.form, "citation.form must not be empty"
            assert citation.filed_date, "citation.filed_date must not be empty"
            assert citation.accession, "citation.accession must not be empty"
            assert citation.url, "citation.url must not be empty"
    assert checked_any
