"""The category filter on Bank Analysis and Card Analysis.

The filter narrows the whole page — tiles, charts and the ledger alike — so the
same clause has to reach every aggregate, and a manual override has to filter
under the label the user can see rather than the one the parser derived.
"""
import datetime as dt
import importlib
from decimal import Decimal

from fastapi.testclient import TestClient

from sparser import bank_store, store
from sparser.banks import BankStatement, BankTransaction
from sparser.schema import Check, DocType, Statement, Transaction, TxnType


def _bank_statement() -> BankStatement:
    def txn(day: int, description: str, amount: str, balance: str) -> BankTransaction:
        return BankTransaction(
            date=dt.date(2025, 1, day), value_date=dt.date(2025, 1, day),
            description=description, reference=f"REF{day}",
            amount=Decimal(amount), type=TxnType.DEBIT,
            balance=Decimal(balance), page=1,
        )

    return BankStatement(
        parser_id="hdfc_bank_v1", bank_code="HDFC", bank_name="HDFC Bank",
        account_holder="Test Person", account_number="5010000012345",
        account_type="SAVINGS A/C - RESIDENT",
        period_start=dt.date(2025, 1, 1), period_end=dt.date(2025, 1, 31),
        source_file="hdfc.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=[
            txn(2, "UPI-SWIGGY-swiggy@hdfc", "250.00", "1750.00"),
            txn(3, "UPI-SWIGGY-swiggy@hdfc", "150.00", "1600.00"),
            txn(4, "IRCTC RAIL BOOKING", "100.00", "1500.00"),
        ],
    )


def _card_statement() -> Statement:
    def txn(day: int, description: str, amount: str, category: str) -> Transaction:
        return Transaction(
            date=dt.date(2025, 1, day), description=description,
            amount=Decimal(amount), type=TxnType.DEBIT, category=category, page=1,
        )

    return Statement(
        doc_type=DocType.CREDIT_CARD,
        template_id="test_v1", issuer="Axis Bank", product="Flipkart",
        account_masked="XXXX1234", currency="INR",
        statement_date=dt.date(2025, 1, 20),
        period_start=dt.date(2025, 1, 1), period_end=dt.date(2025, 1, 31),
        source_file="card.pdf",
        transactions=[
            txn(5, "SWIGGY BANGALORE", "300.00", "RESTAURANTS"),
            txn(6, "SWIGGY BANGALORE", "200.00", "RESTAURANTS"),
            txn(7, "IRCTC RAIL BOOKING", "100.00", "TRAVEL"),
        ],
    )


def _labels(conn) -> set[str]:
    return {row["category"] for row in bank_store.transactions(conn)}


def test_bank_filter_narrows_rows_and_aggregates_together(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())
    picked = sorted(_labels(conn) - {"Travel"})

    rows = bank_store.transactions(conn, categories=picked)
    assert {row["category"] for row in rows} == set(picked)

    analysis = bank_store.analytics(conn, categories=picked)
    assert analysis["totals"]["txn_count"] == len(rows)
    assert {row["label"] for row in analysis["by_category"]} == set(picked)
    assert analysis["totals"]["withdrawals"] == sum(row["amount"] for row in rows)
    conn.close()


def test_bank_filter_absent_means_everything(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())

    assert len(bank_store.transactions(conn, categories=None)) == 3
    assert bank_store.analytics(conn, categories=None)["totals"]["txn_count"] == 3
    conn.close()


def test_bank_filter_with_nothing_ticked_matches_nothing(tmp_path):
    """An empty selection is a real selection, not a missing one."""
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())

    assert bank_store.transactions(conn, categories=[]) == []
    analysis = bank_store.analytics(conn, categories=[])
    assert analysis["totals"]["txn_count"] == 0
    assert analysis["by_category"] == []
    conn.close()


def test_bank_filter_follows_a_manual_override(tmp_path):
    """Retagged rows must filter under their new label, not the derived one."""
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())
    swiggy = [row["id"] for row in bank_store.transactions(conn)
              if "SWIGGY" in row["description"]]
    bank_store.update_transaction_categories(conn, swiggy, "Food delivery")

    rows = bank_store.transactions(conn, categories=["Food delivery"])
    assert len(rows) == len(swiggy)
    assert {row["derived_category"] for row in rows} == {"Food & Dining"}
    # The derived label no longer describes any row, so it selects none.
    assert bank_store.transactions(conn, categories=["Food & Dining"]) == []
    conn.close()


def test_bank_balances_ignore_the_category_filter(tmp_path):
    """A running balance read from a subset of the rows would be nonsense."""
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())
    unfiltered = bank_store.analytics(conn)["totals"]
    filtered = bank_store.analytics(conn, categories=["Travel"])["totals"]

    assert filtered["opening_balance"] == unfiltered["opening_balance"]
    assert filtered["closing_balance"] == unfiltered["closing_balance"]
    assert filtered["withdrawals"] < unfiltered["withdrawals"]
    conn.close()


def test_bank_facets_list_every_category_regardless_of_the_filter(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _bank_statement())

    facets = bank_store.category_facets(conn)
    assert {row["name"] for row in facets} == _labels(conn)
    assert sum(row["n"] for row in facets) == 3
    # Scoped to the accounts asked for: -1 is the "no visible account" scope.
    assert bank_store.category_facets(conn, [-1]) == []
    conn.close()


def test_card_filter_narrows_rows_and_aggregates_together(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    store.import_statement(conn, _card_statement())
    food = next(row["category"] for row in store.transactions(conn)
                if "SWIGGY" in row["description"])

    rows = store.transactions(conn, categories=[food])
    assert len(rows) == 2
    assert {row["category"] for row in rows} == {food}

    analysis = store.analytics(conn, categories=[food])
    assert analysis["totals"]["txn_count"] == 2
    assert analysis["totals"]["spend"] == 500.0
    assert {row["label"] for row in analysis["by_category"]} == {food}
    assert {row["label"] for row in analysis["top_merchants"]} == {rows[0]["merchant"]}
    conn.close()


def test_card_spend_by_category_groups_on_the_override(tmp_path):
    """Retagged rows must move between bars, not just relabel the one they were in.

    Regression: the aggregate grouped on a name SQLite bound to `t.category`,
    the derived column, so an override changed the bar's caption and left every
    rupee where it was.
    """
    conn = store.connect(tmp_path / "statements.db")
    store.import_statement(conn, _card_statement())
    # Only one of the two rows the parser filed together moves, so the split has
    # to be visible: grouping on the derived column would keep them as one bar.
    swiggy = [row for row in store.transactions(conn) if "SWIGGY" in row["description"]]
    derived = {row["category"] for row in swiggy}
    assert len(swiggy) == 2 and len(derived) == 1
    moved = next(row for row in swiggy if row["amount"] == 300.0)

    store.update_transaction_categories(conn, [moved["id"]], "Food delivery")

    bars = {row["label"]: row for row in store.analytics(conn)["by_category"]}
    assert bars["Food delivery"]["n"] == 1
    assert bars["Food delivery"]["value"] == 300.0
    assert bars[derived.pop()]["n"] == 1
    conn.close()


def test_card_filter_with_nothing_ticked_matches_nothing(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    store.import_statement(conn, _card_statement())

    assert store.transactions(conn, categories=[]) == []
    analysis = store.analytics(conn, categories=[])
    assert analysis["totals"]["txn_count"] == 0
    assert analysis["totals"]["spend"] == 0.0
    assert analysis["monthly"] == []
    conn.close()


def test_card_facets_list_every_category_regardless_of_the_filter(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    store.import_statement(conn, _card_statement())

    facets = store.category_facets(conn)
    assert {row["name"] for row in facets} == {row["category"] for row in store.transactions(conn)}
    assert sum(row["n"] for row in facets) == 3
    assert store.category_facets(conn, [-1]) == []
    conn.close()


# ------------------------------------------------------------- over the wire

def _client(tmp_path, monkeypatch):
    """A signed-in client over a database holding one statement of each kind."""
    db_path = tmp_path / "statements.db"
    monkeypatch.setenv("SPARSER_DB", str(db_path))
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    from sparser import api as api_module

    importlib.reload(api_module)
    client = TestClient(api_module.app)
    created = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()
    client.headers.update({"Authorization": f"Bearer {created['token']}"})
    # Filed under the signed-in user's member, or the member scope hides it all.
    member = created["user"]["members"][0]["id"]

    conn = store.connect(db_path)
    bank_store.import_statement(conn, _bank_statement(), member)
    store.import_statement(conn, _card_statement(), member)
    conn.close()
    return client


def test_bootstrap_offers_the_categories_to_filter_by(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    bank = client.get("/api/bank/bootstrap").json()["categories"]
    assert sum(row["n"] for row in bank) == 3
    cards = client.get("/api/bootstrap").json()["categories"]
    assert sum(row["n"] for row in cards) == 3


def test_wire_filter_narrows_both_ledgers(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    bank_pick = client.get("/api/bank/bootstrap").json()["categories"][0]["name"]
    card_pick = client.get("/api/bootstrap").json()["categories"][0]["name"]

    rows = client.get("/api/bank/transactions", params={"categories": [bank_pick]}).json()
    assert rows and {row["category"] for row in rows} == {bank_pick}
    totals = client.get("/api/bank/analytics", params={"categories": [bank_pick]}).json()["totals"]
    assert totals["txn_count"] == len(rows)

    rows = client.get("/api/transactions", params={"categories": [card_pick]}).json()
    assert rows and {row["category"] for row in rows} == {card_pick}
    totals = client.get("/api/analytics", params={"categories": [card_pick]}).json()["totals"]
    assert totals["txn_count"] == len(rows)


def test_wire_filter_omitted_shows_everything(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert len(client.get("/api/bank/transactions").json()) == 3
    assert len(client.get("/api/transactions").json()) == 3


def test_wire_blank_value_is_how_the_client_says_nothing_is_ticked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/bank/transactions", params={"categories": ""}).json() == []
    assert client.get("/api/transactions", params={"categories": ""}).json() == []
    assert client.get(
        "/api/bank/analytics", params={"categories": ""}
    ).json()["totals"]["txn_count"] == 0


def test_wire_filter_handles_a_name_containing_a_comma(tmp_path, monkeypatch):
    """Names are repeated parameters, so a comma inside one is just a comma."""
    client = _client(tmp_path, monkeypatch)
    rows = client.get("/api/bank/transactions").json()
    client.put("/api/bank/transactions/category",
               json={"ids": [rows[0]["id"]], "category": "Rent, rates and bills"})

    picked = client.get(
        "/api/bank/transactions", params={"categories": ["Rent, rates and bills"]}
    ).json()
    assert [row["id"] for row in picked] == [rows[0]["id"]]
