"""A statement must land on the member it was uploaded for.

Before this was fixed, the member was resolved at approval time from the request
alone, so every new card/account was created under the default member no matter
who the upload was for.
"""
import datetime as dt
from decimal import Decimal

from sparser import bank_pipeline, bank_store, pipeline, portal, store
from sparser.banks import BankStatement, BankTransaction
from sparser.schema import Check, TxnType


def _statement() -> BankStatement:
    return BankStatement(
        parser_id="hdfc_bank_v1",
        bank_code="HDFC",
        bank_name="HDFC Bank",
        account_holder="Second Person",
        account_number="5010000099999",
        account_type="SAVINGS A/C - RESIDENT",
        period_start=dt.date(2025, 3, 1),
        period_end=dt.date(2025, 3, 31),
        source_file="second.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=[
            BankTransaction(
                date=dt.date(2025, 3, 2), value_date=dt.date(2025, 3, 2),
                description="UPI-SHOP-SHOP@HDFC", reference="REF1",
                amount=Decimal("100.00"), type=TxnType.DEBIT,
                balance=Decimal("900.00"), page=1,
            ),
        ],
    )


def _pending_row(conn, member_id, path, doc_type="bank_account"):
    run = conn.execute(
        "INSERT INTO ingest_runs (kind,status) VALUES ('bank_scan','done')"
    ).lastrowid
    file_id = conn.execute(
        """INSERT INTO ingest_files
           (run_id,filename,status,document_type,checks_json,encrypted,path,member_id)
           VALUES (?,?,?,?,?,0,?,?)""",
        (run, path.name, "pending", doc_type, "[]", str(path), member_id),
    ).lastrowid
    conn.commit()
    return int(file_id)


def test_scan_records_the_uploading_member_on_each_pending_row(tmp_path):
    db_path = tmp_path / "statements.db"
    recorder = pipeline.Recorder(db_path, "bank_scan", "1 file", member_id=7)
    recorder.begin_file("second.pdf")
    recorder.end_file("pending", document_type="bank_account")
    recorder.finish("done")

    conn = store.connect(db_path)
    stored = conn.execute("SELECT member_id FROM ingest_files").fetchone()["member_id"]
    conn.close()
    assert stored == 7


def test_approval_uses_the_member_from_upload_not_the_default(tmp_path, monkeypatch):
    """The picker may have moved on by the time the user approves days later."""
    db_path = tmp_path / "statements.db"
    conn = store.connect(db_path)
    owner = portal.register(conn, "owner", "secure-pass", "Owner")
    default_member = owner["members"][0]["id"]
    second_member = portal.add_member(conn, owner["id"], "Golu")["id"]

    pdf = tmp_path / "second.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really parsed here")
    file_id = _pending_row(conn, second_member, pdf)
    conn.close()

    seen: list[dict] = []

    def fake_ingest(rec, path, creds, *, commit=True):
        seen.append(dict(creds))
        rec.begin_file(path.name)
        rec.end_file("ok", document_type="bank_account")
        return True

    monkeypatch.setattr(bank_pipeline, "ingest_file", fake_ingest)
    # Approval carries the *default* member, exactly as the API resolves it when
    # the request names none.
    bank_pipeline.run_approve(db_path, [file_id], {"_member_id": default_member})

    assert seen and seen[0]["_member_id"] == second_member


def test_card_approval_uses_the_member_from_scan_not_the_default(tmp_path, monkeypatch):
    db_path = tmp_path / "statements.db"
    conn = store.connect(db_path)
    owner = portal.register(conn, "owner", "secure-pass", "Owner")
    default_member = owner["members"][0]["id"]
    second_member = portal.add_member(conn, owner["id"], "Golu")["id"]

    pdf = tmp_path / "card.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really parsed here")
    file_id = _pending_row(conn, second_member, pdf, doc_type="credit_card")
    conn.close()

    seen: list[dict] = []

    def fake_ingest(rec, path, creds, force=False, **kwargs):
        seen.append(dict(creds))
        rec.begin_file(path.name)
        rec.end_file("ok")
        return True

    monkeypatch.setattr(pipeline, "ingest_file", fake_ingest)
    pipeline.run_approve(db_path, [file_id], {"_member_id": default_member})

    assert seen and seen[0]["_member_id"] == second_member


def test_reparse_keeps_the_original_member(tmp_path, monkeypatch):
    db_path = tmp_path / "statements.db"
    conn = store.connect(db_path)
    pdf = tmp_path / "second.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really parsed here")
    file_id = _pending_row(conn, 42, pdf)
    conn.close()

    def fake_ingest(rec, path, creds, *, commit=True):
        rec.begin_file(path.name)
        rec.end_file("pending", document_type="bank_account")
        return False

    monkeypatch.setattr(bank_pipeline, "ingest_file", fake_ingest)
    bank_pipeline.run_reevaluate(db_path, [file_id], {"_member_id": 1})

    conn = store.connect(db_path)
    replacement = conn.execute(
        "SELECT member_id FROM ingest_files WHERE status='pending'"
    ).fetchone()
    conn.close()
    assert replacement["member_id"] == 42


def test_new_account_is_created_under_the_supplied_member(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    bank_store.import_statement(conn, _statement(), member_id=5)
    assert conn.execute("SELECT member_id FROM bank_accounts").fetchone()["member_id"] == 5

    # A later statement for the same account must not move it to another member.
    bank_store.import_statement(conn, _statement(), member_id=9)
    assert conn.execute("SELECT member_id FROM bank_accounts").fetchone()["member_id"] == 5
    conn.close()
