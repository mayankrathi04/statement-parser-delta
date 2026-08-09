import datetime as dt
from decimal import Decimal

from sparser import mcp_server, store
from sparser.schema import Check, DocType, Statement, Summary, Transaction, TxnType


def _statement(month: int, amount: str, merchant: str) -> Statement:
    end = dt.date(2026, month, 15)
    return Statement(
        template_id="test_v1",
        issuer="Test Bank",
        product="Test Card",
        account_masked="1234XXXXXXXX5678",
        doc_type=DocType.CREDIT_CARD,
        statement_date=end,
        period_start=dt.date(2026, month, 1),
        period_end=end,
        summary=Summary(total_dues=Decimal(amount)),
        transactions=[
            Transaction(
                date=dt.date(2026, month, 10),
                description=merchant,
                amount=Decimal(amount),
                type=TxnType.DEBIT,
            )
        ],
        checks=[Check(name="test_reconcile", passed=True, detail="ok")],
    )


def test_read_only_mcp_analytics(tmp_path, monkeypatch):
    db_path = tmp_path / "statements.db"
    conn = store.connect(db_path)
    store.import_statement(conn, _statement(1, "100.00", "RECURRING SHOP"))
    store.import_statement(conn, _statement(2, "120.00", "RECURRING SHOP"))
    store.import_statement(conn, _statement(3, "5000.00", "LARGE PURCHASE"))
    conn.close()
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path)

    cards = mcp_server.list_cards()["cards"]
    assert len(cards) == 1
    overview = mcp_server.get_overview()
    assert overview["totals"]["spend"] == 5220.0
    assert mcp_server.search_transactions(query="SHOP")["total_matches"] == 2
    assert mcp_server.spending_breakdown("merchant")["groups"][0]["amount"] == 5000.0
    assert mcp_server.special_ledger("emi")["transactions"] == 0
    assert len(mcp_server.find_recurring_merchants(minimum_months=2)["merchants"]) == 1
    assert mcp_server.statement_health()["fully_reconciled"] == 3
