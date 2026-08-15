"""Every bank statement in samples/ must reconcile to the paisa.

No golden file here, unlike the card corpus: a bank narration carries UPI handles
and counterparty names, and freezing those into git would publish the PII the
sample folder is gitignored to keep out. The arithmetic is the gate instead — a
row list that walks the printed running balance from the first row to the last
cannot be missing a row, inventing one, or reading a column it does not own.
"""
from __future__ import annotations

import pytest

from sparser.banks import parse_bank_pdf

from corpus import BANK_PDFS

pytestmark = pytest.mark.skipif(
    not BANK_PDFS, reason="no bank-account PDFs in samples/"
)


@pytest.fixture(scope="module", params=[p.name for p in BANK_PDFS])
def parsed(request):
    return parse_bank_pdf(next(p for p in BANK_PDFS if p.name == request.param))


def test_a_parser_recognised_the_statement(parsed):
    assert parsed.parser_id
    assert parsed.last4, "no account number was recovered"
    assert parsed.period_start <= parsed.period_end


def test_every_check_passes(parsed):
    failed = [c for c in parsed.checks if not c.passed]
    assert not failed, "\n".join(f"{c.name}: {c.detail}" for c in failed)
    assert parsed.confidence == 1.0


def test_running_balance_walks_from_opening_to_closing(parsed):
    balance = parsed.opening_balance
    for row in parsed.transactions:
        assert row.amount > 0
        assert row.description.strip(), f"empty narration on {row.date} {row.amount}"
        balance += row.signed
        assert balance == row.balance, f"{row.date} {row.description} broke the running balance"
    assert balance == parsed.closing_balance
