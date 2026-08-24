"""Golden-corpus regression for the card ledger.

Real statements are PII and stay out of git (see .gitignore). The statements
come from the inbox, decrypted on the fly; `python scripts/make_golden.py` locks
in the exact parse of each. With no statements readable the module skips, so CI
stays green on a clean checkout. Bank-account statements belong to
tests/test_bank_corpus.py.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from sparser import parse_pdf

from corpus import CARD_PDFS, GOLDEN

PDFS = CARD_PDFS
pytestmark = pytest.mark.skipif(not PDFS, reason="no PDFs in samples/")


@pytest.fixture(scope="module", params=[p.name for p in PDFS])
def parsed(request):
    return parse_pdf(next(p for p in PDFS if p.name == request.param))


def test_validation_passes(parsed):
    """Every arithmetic check must reconcile — this is the real accuracy gate."""
    failed = [c for c in parsed.checks if not c.passed and c.severity == "error"]
    assert not failed, "\n".join(f"{c.name}: {c.detail}" for c in failed)


def test_ledger_sides_reconcile_exactly(parsed):
    if parsed.template_id == "generic":
        pytest.skip("generic parses carry no issuer totals to reconcile against")
    s = parsed.summary
    debits = sum((t.amount for t in parsed.transactions if t.type.value == "debit"), Decimal("0"))
    credits = sum((t.amount for t in parsed.transactions if t.type.value == "credit"), Decimal("0"))
    assert debits == s.purchases_debits
    assert credits == s.payments_credits


def test_no_transaction_is_empty(parsed):
    for t in parsed.transactions:
        assert t.description.strip(), f"empty description on {t.date} {t.amount}"
        assert t.amount > 0


def test_matches_golden(parsed):
    path = GOLDEN / f"{Path(parsed.source_file).stem}.json"
    if not path.exists():
        pytest.skip(f"no golden file for {parsed.source_file}; run scripts/make_golden.py")
    want = json.loads(path.read_text())
    got = json.loads(parsed.model_dump_json())
    got.pop("checks", None)
    want.pop("checks", None)
    assert got == want
