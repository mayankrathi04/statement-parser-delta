"""Bulk re-categorisation: the search-and-select flow on Bank Analysis."""
import datetime as dt
from decimal import Decimal

from sparser import bank_store, store
from sparser.banks import BankStatement, BankTransaction
from sparser.schema import Check, TxnType


def _statement(account: str = "5010000012345", holder: str = "Test Person") -> BankStatement:
    def txn(day: int, description: str, amount: str, balance: str) -> BankTransaction:
        return BankTransaction(
            date=dt.date(2025, 1, day), value_date=dt.date(2025, 1, day),
            description=description, reference=f"REF{day}",
            amount=Decimal(amount), type=TxnType.DEBIT,
            balance=Decimal(balance), page=1,
        )

    return BankStatement(
        parser_id="hdfc_bank_v1", bank_code="HDFC", bank_name="HDFC Bank",
        account_holder=holder, account_number=account,
        account_type="SAVINGS A/C - RESIDENT",
        period_start=dt.date(2025, 1, 1), period_end=dt.date(2025, 1, 31),
        source_file="hdfc.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=[
            txn(2, "UPI-SWIGGY-swiggy@hdfc", "250.00", "1750.00"),
            txn(3, "UPI-SWIGGY-swiggy@hdfc", "150.00", "1600.00"),
            txn(4, "UPI-CAFE-cafe@hdfc", "100.00", "1500.00"),
        ],
    )


def test_bulk_set_and_restore_across_many_rows(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _statement())
    swiggy = [
        row["id"] for row in bank_store.transactions(conn) if "SWIGGY" in row["description"]
    ]
    assert len(swiggy) == 2

    updated = bank_store.update_transaction_categories(conn, swiggy, "Food delivery")
    assert len(updated) == 2
    assert {row["category"] for row in updated} == {"Food delivery"}
    assert {row["derived_category"] for row in updated} == {"Food & Dining"}

    by_category = {row["label"]: row["n"] for row in bank_store.analytics(conn)["by_category"]}
    assert by_category["Food delivery"] == 2

    restored = bank_store.update_transaction_categories(conn, swiggy, None)
    assert {row["category"] for row in restored} == {"Food & Dining"}
    assert all(row["category_override"] is None for row in restored)
    conn.close()


def test_bulk_update_cannot_reach_rows_outside_the_allowed_accounts(tmp_path):
    """The API passes the signed-in user's accounts; ids outside them must not move."""
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _statement())
    bank_store.import_statement(conn, _statement("5010000099999", "Other Person"))

    accounts = {row["display_name"]: row["id"] for row in bank_store.accounts(conn)}
    mine, theirs = sorted(accounts.values())
    every_id = [row["id"] for row in bank_store.transactions(conn)]

    updated = bank_store.update_transaction_categories(conn, every_id, "Mine", [mine])
    assert updated and len(updated) == 3

    moved = {
        row["id"] for row in bank_store.transactions(conn) if row["category"] == "Mine"
    }
    untouched = [row for row in bank_store.transactions(conn) if row["account_id"] == theirs]
    assert moved and all(row["category"] != "Mine" for row in untouched)
    conn.close()


def test_bulk_update_ignores_unknown_ids_and_rejects_an_overlong_label(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _statement())
    ids = [row["id"] for row in bank_store.transactions(conn)]

    assert bank_store.update_transaction_categories(conn, [], "Anything") == []
    assert bank_store.update_transaction_categories(conn, [999999], "Anything") == []

    try:
        bank_store.update_transaction_categories(conn, ids, "x" * 500)
    except ValueError as exc:
        assert "at most" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("an overlong category label should be rejected")
    conn.close()
