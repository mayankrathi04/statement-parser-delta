import datetime as dt
from decimal import Decimal

from sparser import bank_pipeline, bank_store, pipeline, store
from sparser.banks import BankStatement, BankTransaction
from sparser.banks.hdfc import _columns, _page_transactions
from sparser.schema import Check, TxnType


def _word(text: str, x0: float, top: float) -> dict:
    """A pdfplumber word for the hand-built pages below.

    The parser reads both edges of a word — a narration ends where the reference
    column starts — so a fixture that carried only x0 would exercise a page no
    PDF can produce. The width is nominal; only the ordering it implies matters.
    """
    return {"text": text, "x0": x0, "x1": x0 + 6.0 * len(text), "top": top}


def statement(closing: str = "1250.00") -> BankStatement:
    return BankStatement(
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
                description="NEFT CR-HDFC0001-EMPLOYER", reference="REF1",
                amount=Decimal("500.00"), type=TxnType.CREDIT,
                balance=Decimal("1500.00"), page=1,
            ),
            BankTransaction(
                date=dt.date(2025, 1, 3), value_date=dt.date(2025, 1, 3),
                description="UPI-CAFE-CAFE@HDFC", reference="REF2",
                amount=Decimal("250.00"), type=TxnType.DEBIT,
                balance=Decimal(closing), page=1,
            ),
        ],
    )


def test_bank_ledger_is_separate_from_card_ledger(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, statement())

    assert store.cards(conn) == []
    assert store.transactions(conn) == []
    assert len(bank_store.accounts(conn)) == 1
    assert len(bank_store.transactions(conn)) == 2
    analytics = bank_store.analytics(conn)
    assert analytics["totals"] == {
        "withdrawals": 250.0,
        "deposits": 500.0,
        "net": 250.0,
        "txn_count": 2,
        "avg_debit": 250.0,
        "largest_debit": 250.0,
        "opening_balance": 1000.0,
        "closing_balance": 1250.0,
    }
    assert analytics["deposits_by_category"] == [
        {"label": "Transfers", "value": 500.0, "n": 1}
    ]
    conn.close()


def test_bank_statement_reimport_replaces_same_period(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    first = bank_store.import_statement(conn, statement())
    replacement = statement("1200.00")
    replacement.transactions[-1].amount = Decimal("300.00")
    second = bank_store.import_statement(conn, replacement)

    assert first[2] is False
    assert second[2] is True
    assert conn.execute("SELECT COUNT(*) FROM bank_statements").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 2
    assert bank_store.analytics(conn)["totals"]["withdrawals"] == 300.0
    conn.close()


def test_account_api_shape_never_exposes_identity_fingerprint(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, statement())
    account = bank_store.accounts(conn)[0]

    assert account["masked_number"] == "•••• 2345"
    assert "account_fingerprint" not in account
    assert "5010000012345" not in repr(account)
    conn.close()


def test_bank_review_queue_does_not_include_or_discard_card_rows(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    run = conn.execute(
        "INSERT INTO ingest_runs (kind,status) VALUES ('bank_scan','done')"
    ).lastrowid
    bank_id = conn.execute(
        """INSERT INTO ingest_files
           (run_id,filename,status,document_type,checks_json,encrypted)
           VALUES (?,?,?,?,?,0)""",
        (run, "bank.pdf", "pending", "bank_account", "[]"),
    ).lastrowid
    card_id = conn.execute(
        """INSERT INTO ingest_files
           (run_id,filename,status,document_type,checks_json,encrypted)
           VALUES (?,?,?,?,?,0)""",
        (run, "card.pdf", "pending", "credit_card", "[]"),
    ).lastrowid
    conn.commit()

    assert [row["id"] for row in bank_pipeline.pending(conn)] == [bank_id]
    assert bank_pipeline.discard(conn, [bank_id, card_id]) == 1
    assert conn.execute("SELECT status FROM ingest_files WHERE id=?", (card_id,)).fetchone()[0] == "pending"
    assert [row["id"] for row in pipeline.pending(conn)] == [card_id]
    assert pipeline.discard(conn, [bank_id, card_id]) == 1
    assert conn.execute("SELECT status FROM ingest_files WHERE id=?", (bank_id,)).fetchone()[0] == "discarded"
    conn.close()


def test_first_transaction_above_page_one_header_position_is_not_skipped():
    """Continuation pages start higher than page 1's transaction header."""
    class Page:
        width = 612
        height = 792

        def extract_words(self, **_kwargs):
            return [
                _word("UPI-TEST", 60.0, 183.0),
                _word("01/04/2025", 8.0, 190.0),
                _word("REF001", 226.0, 190.0),
                _word("01/04/2025", 320.0, 190.0),
                _word("100.00", 395.0, 190.0),
                _word("0.00", 462.0, 190.0),
                _word("900.00", 536.0, 190.0),
            ]

    columns = {
        "date": 8.0, "narration": 60.0, "reference": 226.0, "value": 320.0,
        "withdrawal": 395.0, "deposit": 462.0, "closing": 536.0,
        "narration_left": 23.0, "narration_right": 221.0,
        "reference_right": 273.0, "value_right": 357.5,
        
        # Derived by `_columns` from the headings; the seven-column layout these
        # fixtures describe puts the narration against the reference column and
        # the first amount after the value date.
        "narration_end": 226.0, "withdrawal_left": 357.5,
        "withdrawal_right": 457.0, "deposit_right": 531.0,
        # This page's first transaction is above page 1's header bottom. The
        # old parser used 204 as the lower bound and silently dropped the row.
        "header_bottom": 204.0,
    }

    rows = _page_transactions(Page(), 2, columns)
    assert len(rows) == 1
    assert rows[0].amount == Decimal("100.00")
    assert rows[0].balance == Decimal("900.00")


def test_negative_withdrawal_is_normalized_as_positive_credit():
    class Page:
        width = 612
        height = 792

        def extract_words(self, **_kwargs):
            return [
                _word("POS-REVERSAL", 60.0, 183.0),
                _word("24/07/2023", 8.0, 190.0),
                _word("REF002", 226.0, 190.0),
                _word("24/07/2023", 320.0, 190.0),
                _word("-21.37", 395.0, 190.0),
                _word("0.00", 462.0, 190.0),
                _word("1,120.37", 536.0, 190.0),
            ]

    columns = {
        "date": 8.0, "narration": 60.0, "reference": 226.0, "value": 320.0,
        "withdrawal": 395.0, "deposit": 462.0, "closing": 536.0,
        "narration_left": 23.0, "narration_right": 221.0,
        "reference_right": 273.0, "value_right": 357.5,
        
        # Derived by `_columns` from the headings; the seven-column layout these
        # fixtures describe puts the narration against the reference column and
        # the first amount after the value date.
        "narration_end": 226.0, "withdrawal_left": 357.5,
        "withdrawal_right": 457.0, "deposit_right": 531.0,
        "header_bottom": 204.0,
    }

    row = _page_transactions(Page(), 25, columns)[0]
    assert row.type is TxnType.CREDIT
    assert row.amount == Decimal("21.37")
    assert row.signed == Decimal("21.37")


def test_ach_credit_is_dividend_and_existing_rows_are_refreshed(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    dividend = statement()
    dividend.transactions[0].description = "ACH C- SAMPLE LIMITED-100001"
    bank_store.import_statement(conn, dividend)

    conn.execute("UPDATE bank_transactions SET category='Other'")
    conn.execute(
        """INSERT INTO app_metadata (key,value) VALUES ('bank_enrichment_version','1')
           ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
    )
    conn.commit()

    bank_store.analytics(conn)
    category = conn.execute(
        "SELECT category FROM bank_transactions ORDER BY id LIMIT 1"
    ).fetchone()[0]
    assert category == "Dividends"
    conn.close()


def test_tax_narration_uses_bank_tax_category():
    assert bank_store._category("UPI-INCOME TAX DEPARTMENT-REF123") == "Tax"
    assert bank_store._category("GST PAYMENT CPIN 123456") == "Tax"


def test_bill_payment_apps_use_bills_and_utilities_category():
    assert bank_store._category("IB BILLPAY DR-HDFC-123456") == "Bills & Utilities"
    assert bank_store._category("UPI-CHEQ-CHEQ@YESBANK") == "Bills & Utilities"
    assert bank_store._category("UPI-CRED-CRED.CLUB@AXISBANK") == "Bills & Utilities"
    assert bank_store._category("CREDIT INTEREST CAPITALISED") != "Bills & Utilities"


def test_manual_category_override_drives_analytics_and_survives_refresh_and_reimport(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    original = statement()
    bank_store.import_statement(conn, original)
    transaction_id = conn.execute(
        "SELECT id FROM bank_transactions WHERE direction='debit'"
    ).fetchone()[0]

    saved = bank_store.update_transaction_category(conn, transaction_id, "Coffee & Snacks")
    assert saved == {
        "id": transaction_id,
        "category": "Coffee & Snacks",
        "derived_category": "Food & Dining",
        "category_override": "Coffee & Snacks",
        "category_is_override": True,
    }
    assert bank_store.analytics(conn)["by_category"] == [
        {"label": "Coffee & Snacks", "value": 250.0, "n": 1}
    ]

    conn.execute(
        "UPDATE app_metadata SET value='old' WHERE key='bank_enrichment_version'"
    )
    conn.commit()
    row = [item for item in bank_store.transactions(conn) if item["id"] == transaction_id][0]
    assert row["category"] == "Coffee & Snacks"
    assert row["derived_category"] == "Food & Dining"

    bank_store.import_statement(conn, original)
    replacement = conn.execute(
        "SELECT id FROM bank_transactions WHERE direction='debit'"
    ).fetchone()[0]
    row = [item for item in bank_store.transactions(conn) if item["id"] == replacement][0]
    assert row["category"] == "Coffee & Snacks"
    assert row["category_is_override"] is True

    restored = bank_store.update_transaction_category(conn, replacement, None)
    assert restored["category"] == "Food & Dining"
    assert restored["category_override"] is None
    assert bank_store.analytics(conn)["by_category"] == [
        {"label": "Food & Dining", "value": 250.0, "n": 1}
    ]
    conn.close()


def test_current_layout_assigns_predate_narration_to_the_next_transaction():
    """A page's first row may be unwrapped even when later rows wrap upward."""
    class Page:
        width = 612
        height = 792

        def extract_words(self, **_kwargs):
            return [
                _word("ACME", 60.0, 190.0),
                _word("CORP", 78.0, 190.0),
                _word("09/03/2026", 8.0, 190.0),
                _word("DIVREF", 226.0, 190.0),
                _word("09/03/2026", 320.0, 190.0),
                _word("0.00", 395.0, 190.0),
                _word("132.00", 462.0, 190.0),
                _word("1,132.00", 536.0, 190.0),
                _word("UPI-MID", 60.0, 202.0),
                _word("LAND", 88.0, 202.0),
                _word("09/03/2026", 8.0, 206.0),
                _word("UPIREF", 226.0, 206.0),
                _word("09/03/2026", 320.0, 206.0),
                _word("45.00", 395.0, 206.0),
                _word("0.00", 462.0, 206.0),
                _word("1,087.00", 536.0, 206.0),
                _word("BAKERS@YESBANK-UPI", 60.0, 210.0),
            ]

    columns = {
        "date": 8.0, "narration": 60.0, "reference": 226.0, "value": 320.0,
        "withdrawal": 395.0, "deposit": 462.0, "closing": 536.0,
        "narration_left": 23.0, "narration_right": 221.0,
        "reference_right": 273.0, "value_right": 357.5,
        
        # Derived by `_columns` from the headings; the seven-column layout these
        # fixtures describe puts the narration against the reference column and
        # the first amount after the value date.
        "narration_end": 226.0, "withdrawal_left": 357.5,
        "withdrawal_right": 457.0, "deposit_right": 531.0,
        "header_bottom": 204.0,
    }

    rows = _page_transactions(Page(), 59, columns)
    assert [row.description for row in rows] == [
        "ACME CORP",
        "UPI-MID LAND BAKERS@YESBANK-UPI",
    ]


def test_legacy_layout_keeps_long_narration_with_the_preceding_date():
    class Page:
        width = 612
        height = 792

        def extract_words(self, **_kwargs):
            return [
                _word("02/04/24", 32.0, 236.0),
                _word("UPI-MERCHANT", 68.0, 236.0),
                _word("REF1", 272.0, 236.0),
                _word("02/04/24", 341.0, 236.0),
                _word("100.00", 416.0, 236.0),
                _word("900.00", 559.0, 236.0),
                _word("SECOND", 68.0, 252.0),
                _word("THIRD", 68.0, 268.0),
                _word("03/04/24", 32.0, 285.0),
                _word("UPI-NEXT", 68.0, 285.0),
                _word("REF2", 272.0, 285.0),
                _word("03/04/24", 341.0, 285.0),
                _word("50.00", 416.0, 285.0),
                _word("850.00", 559.0, 285.0),
            ]

    columns = {
        "date": 37.5, "narration": 135.6, "reference": 266.7, "value": 340.0,
        "withdrawal": 381.3, "deposit": 461.9, "closing": 530.8,
        "narration_left": 52.5, "narration_right": 261.7,
        "reference_right": 303.35, "value_right": 360.65,
        
        # Derived by `_columns` from the headings; the seven-column layout these
        # fixtures describe puts the narration against the reference column and
        # the first amount after the value date.
        "narration_end": 266.7, "withdrawal_left": 360.65,
        "withdrawal_right": 456.9, "deposit_right": 525.8,
        "header_bottom": 225.0,
    }

    rows = _page_transactions(Page(), 1, columns)
    assert rows[0].description == "UPI-MERCHANT SECOND THIRD"
    assert rows[1].description == "UPI-NEXT"


# ------------------------------------------------- the mailed five-column layout

class _HeaderPage:
    """A page carrying nothing but one header row, for measuring columns."""

    def __init__(self, words):
        self._words = words
        self.width, self.height = 595.0, 842.0

    def extract_words(self, **_kwargs):
        return self._words


def _header(*labels_at):
    # `_columns` also measures where the header row ends, so these carry a bottom.
    return _HeaderPage([
        {**_word(text, x0, 200.0), "bottom": 210.0} for text, x0 in labels_at
    ])


def test_the_mailed_layout_has_no_reference_or_value_date_column():
    """HDFC's e-statement prints five columns where the net-banking download
    prints seven. Requiring `Chq` rejected every mailed statement outright."""
    columns = _columns(_header(
        ("Txn", 40.0), ("Date", 62.0), ("Narration", 100.0),
        ("Withdrawals", 380.0), ("Deposits", 450.0), ("Closing", 510.0), ("Balance", 548.0),
    ))

    assert columns["reference"] is None and columns["value"] is None
    # The column starts at "Txn", not at the "Date" that follows it.
    assert columns["date"] == 40.0
    # Narration runs to the first amount column, and amounts may not begin
    # before the midpoint between the narration and the withdrawals heading.
    assert columns["narration_end"] == 380.0
    assert columns["narration"] < columns["withdrawal_left"] < columns["withdrawal"]


def test_the_download_layout_still_measures_all_seven_columns():
    columns = _columns(_header(
        ("Date", 40.0), ("Narration", 100.0), ("Chq./Ref.No.", 226.0), ("Value", 320.0),
        ("Dt", 344.0), ("Withdrawal", 395.0), ("Amt.", 440.0), ("Deposit", 462.0),
        ("Closing", 536.0),
    ))

    assert columns["reference"] == 226.0 and columns["value"] == 320.0
    assert columns["narration_end"] == 226.0
    # Unchanged from before the mailed layout was supported: the first amount
    # may appear halfway between the value date and the withdrawal heading.
    assert columns["withdrawal_left"] == (320.0 + 395.0) / 2


def test_a_header_naming_the_reference_column_differently_is_still_found():
    """One download variant prints `Chq. / Ref No.`, another `Ref No.` alone."""
    columns = _columns(_header(
        ("Date", 40.0), ("Narration", 100.0), ("Ref", 226.0), ("No.", 250.0),
        ("Value", 320.0), ("Withdrawal", 395.0), ("Deposit", 462.0), ("Closing", 536.0),
    ))

    assert columns["reference"] == 226.0


# ------------------------------------------------- overlapping statements union

def _txn(day: int, amount: str, kind: TxnType, balance: str, description: str):
    return BankTransaction(
        date=dt.date(2025, 1, day), value_date=dt.date(2025, 1, day),
        description=description, reference=None,
        amount=Decimal(amount), type=kind, balance=Decimal(balance), page=1,
    )


def _period(start: int, end: int, rows: list[BankTransaction]) -> BankStatement:
    return BankStatement(
        parser_id="hdfc_bank_v1", bank_code="HDFC", bank_name="HDFC Bank",
        account_holder="Test Person", account_number="5010000012345",
        account_type="SAVINGS A/C - RESIDENT",
        period_start=dt.date(2025, 1, start), period_end=dt.date(2025, 1, end),
        source_file=f"hdfc_{start}_{end}.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=rows,
    )


def test_an_overlapping_statement_adds_only_the_days_it_brings(tmp_path):
    """A yearly download and a monthly statement describe the same transactions.
    Storing both used to count every shared day twice."""
    conn = store.connect(tmp_path / "statements.db")
    first = [
        _txn(2, "500.00", TxnType.CREDIT, "1500.00", "NEFT CR-EMPLOYER"),
        _txn(3, "250.00", TxnType.DEBIT, "1250.00", "UPI-SHOP"),
    ]
    bank_store.import_statement(conn, _period(1, 15, first))

    overlapping = first + [_txn(20, "100.00", TxnType.DEBIT, "1150.00", "UPI-LATER")]
    _, rows, _, _, already = bank_store.import_statement(conn, _period(10, 31, overlapping))

    assert (rows, already) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 3
    assert bank_store.analytics(conn)["totals"]["withdrawals"] == 350.0
    conn.close()


def test_two_identical_looking_rows_on_one_day_are_two_transactions(tmp_path):
    """Pay 100, receive 100, pay 100: the first and third share a date, an
    amount, a direction and a balance. A "does one exist" test would drop the
    third; counting keeps it."""
    conn = store.connect(tmp_path / "statements.db")
    rows = [
        _txn(5, "100.00", TxnType.DEBIT, "900.00", "UPI-FIRST"),
        _txn(5, "100.00", TxnType.CREDIT, "1000.00", "UPI-REFUND"),
        _txn(5, "100.00", TxnType.DEBIT, "900.00", "UPI-THIRD"),
    ]
    bank_store.import_statement(conn, _period(1, 10, rows))
    assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 3

    # The same day arriving again, with one row the ledger has not seen.
    again = rows + [_txn(5, "100.00", TxnType.DEBIT, "800.00", "UPI-FOURTH")]
    _, stored, _, _, already = bank_store.import_statement(conn, _period(5, 20, again))

    assert (stored, already) == (1, 3)
    assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 4
    conn.close()


def test_a_split_day_unions_rather_than_duplicating(tmp_path):
    """Some of a day in one statement, the rest in another."""
    conn = store.connect(tmp_path / "statements.db")
    early = [_txn(7, "100.00", TxnType.DEBIT, "900.00", "UPI-A")]
    bank_store.import_statement(conn, _period(1, 7, early))

    later = early + [
        _txn(7, "200.00", TxnType.DEBIT, "700.00", "UPI-B"),
        _txn(7, "300.00", TxnType.DEBIT, "400.00", "UPI-C"),
    ]
    _, stored, _, _, already = bank_store.import_statement(conn, _period(7, 14, later))

    assert (stored, already) == (2, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM bank_transactions WHERE txn_date='2025-01-07'"
    ).fetchone()[0] == 3
    conn.close()


def test_reimporting_the_same_period_still_replaces_rather_than_skips(tmp_path):
    """The replaced statement's own rows must not read as "already held" — that
    would turn every correction into an import of nothing."""
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, statement())
    corrected = statement("1200.00")
    corrected.transactions[-1].amount = Decimal("300.00")

    _, stored, replaced, _, already = bank_store.import_statement(conn, corrected)

    assert replaced is True
    assert (stored, already) == (2, 0)
    assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 2
    conn.close()


def test_the_preview_counts_what_approving_would_add(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    rows = [_txn(2, "500.00", TxnType.CREDIT, "1500.00", "NEFT CR-EMPLOYER")]
    assert bank_store.import_preview(conn, _period(1, 15, rows)) == {
        "total": 1, "known": 0, "new": 1,
    }

    bank_store.import_statement(conn, _period(1, 15, rows))

    # The same file again: its own rows are about to be replaced, so they are
    # not counted as already held.
    assert bank_store.import_preview(conn, _period(1, 15, rows))["new"] == 1
    # A different period covering the same day: nothing new to add.
    assert bank_store.import_preview(conn, _period(1, 31, rows)) == {
        "total": 1, "known": 1, "new": 0,
    }
    conn.close()
