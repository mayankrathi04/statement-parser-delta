"""The template-free path must stand on its own.

These tests deliberately bypass the template library: if the generic engine can
recover the same rows from a document it has never been told about, the approach
generalises to issuers we have not seen.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sparser import parse_pdf
from sparser.engine import NoTemplateMatch
from sparser.generic import infer_glyph_fixes, parse_generic
from sparser.geometry import Line, Word

from corpus import CARD_PDFS as PDFS


def _source(name: str) -> Path:
    """The corpus hands back readable paths, which for an encrypted statement
    is a decrypted copy rather than the file in the inbox."""
    return next(p for p in PDFS if p.name == name)


def test_infer_glyph_fixes_spots_broken_rupee():
    lines = [
        Line(top=float(i), words=[Word("C", 520, 524, i, i + 8), Word("840.00", 526, 552, i, i + 8)])
        for i in range(6)
    ]
    assert infer_glyph_fixes(lines) == {"C": "₹"}


def test_infer_glyph_fixes_ignores_ordinary_letters():
    lines = [
        Line(top=float(i), words=[Word("A", 100, 104, i, i + 8), Word("UNIT", 106, 130, i, i + 8)])
        for i in range(6)
    ]
    assert infer_glyph_fixes(lines) == {}


@pytest.mark.skipif(not PDFS, reason="no readable card statements")
@pytest.mark.parametrize("pdf", [p.name for p in PDFS])
def test_generic_never_invents_rows(pdf):
    """Whatever the fallback recovers must be real.

    Under-recovery is acceptable and expected — a table spanning pages can lose
    a continuation block without a header to anchor on. Reporting a transaction
    that is not in the statement is not acceptable, because a validator cannot
    catch it once the totals happen to line up.

    Scoped to the billing period. A statement carries auxiliary tables of dates
    and amounts — an EMI repayment schedule, a rewards ledger — that are
    structurally indistinguishable from transactions without knowing what the
    issuer means. Those carry dates outside the cycle, so the period bounds are
    what separate them; inside the cycle the fallback must be exact.
    """
    templated = parse_pdf(_source(pdf))
    generic = parse_generic(_source(pdf))
    if not (templated.period_start and templated.period_end):
        pytest.skip("no billing period to scope against")

    lo, hi = templated.period_start, templated.period_end
    truth = {(t.date, t.amount, t.type) for t in templated.transactions}
    invented = [
        t
        for t in generic.transactions
        if lo <= t.date <= hi and (t.date, t.amount, t.type) not in truth
    ]
    assert not invented, f"generic invented {len(invented)} in-period rows, e.g. {invented[:1]}"


@pytest.mark.skipif(not PDFS, reason="no readable card statements")
@pytest.mark.parametrize("pdf", [p.name for p in PDFS])
def test_generic_recovers_most_rows(pdf):
    templated = parse_pdf(_source(pdf))
    generic = parse_generic(_source(pdf))
    assert len(generic.transactions) >= 0.6 * len(templated.transactions)


@pytest.mark.skipif(not PDFS, reason="no readable card statements")
def test_generic_fallback_can_be_disabled(tmp_path):
    empty = tmp_path / "templates"
    empty.mkdir()
    with pytest.raises(NoTemplateMatch):
        parse_pdf(PDFS[0], template_dir=empty, generic_fallback=False)


@pytest.mark.skipif(not PDFS, reason="no readable card statements")
def test_generic_fallback_is_flagged_not_silent(tmp_path):
    empty = tmp_path / "templates"
    empty.mkdir()
    stmt = parse_pdf(PDFS[0], template_dir=empty)
    assert stmt.template_id == "generic"
    # Un-reconciled output must never look as clean as a template-matched parse.
    assert any(c.name == "template_matched" and not c.passed for c in stmt.checks)
    assert stmt.confidence < 1.0
