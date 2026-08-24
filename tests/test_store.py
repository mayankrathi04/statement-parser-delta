"""Storage invariants — above all, that importing never duplicates.

The duplication bug this guards against was subtle: statements were keyed by
(card, statement date, period start), and when the parser learned to extract a
statement date it had previously missed, the same document stopped matching
itself and was stored a second time. Any field the parser might start or stop
extracting is unfit for an identity key; the billing cycle is not one of those.
"""
from __future__ import annotations

import pytest

from sparser import parse_pdf
from sparser import store

from corpus import CARD_PDFS as PDFS
pytestmark = pytest.mark.skipif(not PDFS, reason="no readable card statements")


def counts(conn) -> tuple[int, int, int]:
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM cards) a, (SELECT COUNT(*) FROM statements) b,"
        " (SELECT COUNT(*) FROM transactions) c"
    ).fetchone()
    return r["a"], r["b"], r["c"]


@pytest.fixture(scope="module")
def parsed():
    return [parse_pdf(p) for p in PDFS]


def test_reimport_is_idempotent(tmp_path, parsed):
    conn = store.connect(tmp_path / "t.db")
    for stmt in parsed:
        store.import_statement(conn, stmt)
    first = counts(conn)

    for _ in range(3):
        for stmt in parsed:
            store.import_statement(conn, stmt)
        assert counts(conn) == first, "re-importing the same statements changed the row counts"


def test_identity_survives_a_newly_extracted_statement_date(tmp_path, parsed):
    """The exact regression: a field appearing later must not fork the identity."""
    conn = store.connect(tmp_path / "t.db")
    stmt = parsed[0]

    original = stmt.statement_date
    stmt.statement_date = None          # as an older parser would have left it
    store.import_statement(conn, stmt)
    before = counts(conn)

    stmt.statement_date = original      # a later parser fills it in
    store.import_statement(conn, stmt)
    assert counts(conn) == before, "a newly extracted statement date created a duplicate"


def test_duplicate_is_visible_before_it_is_imported(tmp_path, parsed):
    """The review list must be able to warn, using the same key as the importer."""
    conn = store.connect(tmp_path / "t.db")
    stmt = parsed[0]

    assert store.find_statement(conn, stmt) is None
    store.import_statement(conn, stmt)
    assert store.find_statement(conn, stmt) is not None


def test_amounts_round_trip_exactly(tmp_path, parsed):
    """Paise in, paise out — no float drift through the store."""
    conn = store.connect(tmp_path / "t.db")
    stmt = next(s for s in parsed if s.transactions)
    store.import_statement(conn, stmt)

    stored = {(t["txn_date"], t["amount"]) for t in store.transactions(conn)}
    for t in stmt.transactions:
        assert (t.date.isoformat(), float(t.amount)) in stored
