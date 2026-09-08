"""Tests for globalinsight.models: the shared typed data objects.

Citation is the load-bearing compliance object - every narrative claim the
synthesis step emits is required to carry one. These tests just pin its
shape and immutability; the "every data point has a citation" business
invariant is exercised end-to-end in test_financials.py.
"""

import dataclasses

import pytest

from globalinsight.models import Citation, Company, FinancialPoint


def test_citation_is_frozen():
    citation = Citation(
        form="10-K", item="", filed_date="2026-03-15", accession="acc-1", url="https://x"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        citation.form = "8-K"


def test_citation_has_exactly_the_compliance_fields():
    fields = {f.name for f in dataclasses.fields(Citation)}
    assert fields == {"form", "item", "filed_date", "accession", "url"}


def test_company_is_frozen_and_hashable():
    a = Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")
    b = Company(ticker="NVDA", cik=1045810, name="NVIDIA CORP")
    assert a == b
    assert hash(a) == hash(b)


def test_financial_point_requires_a_citation():
    fields = {f.name for f in dataclasses.fields(FinancialPoint)}
    assert "citation" in fields
    citation = Citation(form="10-K", item="", filed_date="2026-03-15", accession="a", url="u")
    point = FinancialPoint(fiscal_year=2025, value=1.0, citation=citation)
    assert point.citation is citation
