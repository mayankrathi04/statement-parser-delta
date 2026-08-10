import datetime as dt
from decimal import Decimal

from sparser import bank_store, mcp_server, store
from sparser.bank_parser import BankStatement, BankTransaction
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


def test_mcp_bank_analytics_and_scoped_category_override(tmp_path, monkeypatch):
    db_path = tmp_path / "bank.db"
    conn = store.connect(db_path)
    bank_store.import_statement(conn, BankStatement(
        parser_id="hdfc_bank_v1",
        bank_code="HDFC",
        bank_name="HDFC Bank",
        account_holder="Test Person",
        account_number="5010000012345",
        account_type="SAVINGS A/C - RESIDENT",
        period_start=dt.date(2025, 1, 1),
        period_end=dt.date(2025, 1, 31),
        source_file="hdfc.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=[
            BankTransaction(
                date=dt.date(2025, 1, 2), value_date=dt.date(2025, 1, 2),
                description="ACH C- SAMPLE LIMITED-100001", reference="REF1",
                amount=Decimal("500.00"), type=TxnType.CREDIT,
                balance=Decimal("1500.00"), page=1,
            ),
            BankTransaction(
                date=dt.date(2025, 1, 3), value_date=dt.date(2025, 1, 3),
                description="UPI-CAFE-CAFE@HDFC", reference="REF2",
                amount=Decimal("250.00"), type=TxnType.DEBIT,
                balance=Decimal("1250.00"), page=1,
            ),
        ],
    ))
    debit_id = conn.execute(
        "SELECT id FROM bank_transactions WHERE direction='debit'"
    ).fetchone()[0]
    conn.close()
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path)

    accounts = mcp_server.list_bank_accounts()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["statement_periods"][0]["period_start"] == "2025-01-01"
    assert mcp_server.get_bank_overview()["totals"]["net_cash_flow"] == 250.0
    assert mcp_server.search_bank_transactions(query="CAFE")["total_matches"] == 1
    assert mcp_server.bank_cashflow_breakdown("category", "credit")["groups"] == [
        {"label": "Dividends", "amount": 500.0, "transactions": 1, "share_percent": 100.0}
    ]
    assert mcp_server.bank_statement_health()["fully_reconciled"] == 1
    assert mcp_server.bank_pipeline_status() == {"runs": [], "pending_review": []}

    saved = mcp_server.set_bank_transaction_category(debit_id, "Coffee & Snacks")
    assert saved["category"] == "Coffee & Snacks"
    row = mcp_server.search_bank_transactions(category="Coffee & Snacks")["transactions"][0]
    assert row["category_is_override"] is True
    assert row["derived_category"] == "Food & Dining"
    assert mcp_server.bank_cashflow_breakdown("category", "debit")["groups"][0]["label"] == "Coffee & Snacks"

    restored = mcp_server.set_bank_transaction_category(debit_id, None)
    assert restored["category"] == "Food & Dining"


def test_emi_ledger_uses_conversion_evidence_not_eligibility_marker(tmp_path):
    db_path = tmp_path / "emi.db"
    conn = store.connect(db_path)
    statement = Statement(
        template_id="hdfc_cc_v1",
        issuer="HDFC Bank",
        product="Test Card",
        account_masked="XXXX0011",
        doc_type=DocType.CREDIT_CARD,
        statement_date=dt.date(2025, 8, 15),
        period_start=dt.date(2025, 7, 16),
        period_end=dt.date(2025, 8, 15),
        transactions=[
            Transaction(
                date=dt.date(2025, 8, 2), description="ELIGIBLE ONLY PURCHASE",
                amount=Decimal("2500"), type=TxnType.DEBIT, is_emi=True,
            ),
            Transaction(
                date=dt.date(2025, 8, 3), description="GADGET WORLD",
                amount=Decimal("150000"), type=TxnType.DEBIT,
            ),
            Transaction(
                date=dt.date(2025, 8, 6),
                description="AGGREGATOR EMI - OFFUS CREDIT",
                amount=Decimal("150000"), type=TxnType.CREDIT,
            ),
            Transaction(
                date=dt.date(2025, 8, 15),
                description="Principal Amount Amortization - <1/3>GADGET WORLD",
                amount=Decimal("90000"), type=TxnType.DEBIT,
            ),
        ],
    )
    store.import_statement(conn, statement)

    ledger = store.analytics(conn)["emi"]
    assert ledger["count"] == 1
    assert ledger["total"] == 150000.0
    assert ledger["rows"][0]["description"] == "GADGET WORLD"
    assert store.analytics(conn)["totals"]["spend"] == 152500.0
    conn.close()
