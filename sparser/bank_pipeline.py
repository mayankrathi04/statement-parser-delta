"""Upload-only ingest pipeline for bank account statements.

This module does not call the credit-card engine or its YAML templates. Bank
parsers have their own dispatch boundary so a future Axis/ICICI account parser
can be registered here without changing card behaviour.
"""
from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path
from typing import Iterable, Optional

import pdfplumber

from . import accounts, bank_store, store
from .banks import UnsupportedBankStatement, parse_bank_pdf
from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from .doctype import document_kind
from .pipeline import Recorder


def _password_candidates(db_path: Path, creds: dict) -> list[str]:
    conn = store.connect(db_path)
    try:
        profile = accounts.get_profile(conn)
    finally:
        conn.close()
    return list(dict.fromkeys(
        ([creds["password"]] if creds.get("password") else [])
        + candidate_passwords(
            creds.get("name") or profile.get("full_name"),
            creds.get("dob") or profile.get("dob"),
        )
    ))


def ingest_file(rec: Recorder, pdf: Path, creds: dict, *, commit: bool = True) -> bool:
    rec.begin_file(pdf.name)
    workfile = pdf
    temporary: Optional[Path] = None
    encrypted = False
    try:
        rec.step("download", "ok", "received from browser upload")

        rec.timed()
        encrypted = is_encrypted(pdf)
        if encrypted:
            handle = tempfile.NamedTemporaryFile(prefix="sparser-bank-", suffix=".pdf", delete=False)
            temporary = Path(handle.name)
            handle.close()
            workfile, used = decrypt_to(pdf, temporary, _password_candidates(rec.db_path, creds))
            if creds.get("password") and used == creds["password"]:
                detail = "encrypted; opened with the supplied password"
            elif used:
                detail = "encrypted; opened with a profile-derived password"
            else:
                detail = "no password needed"
            rec.step("decrypt", "ok", detail, rec.elapsed_ms)
        else:
            rec.step("decrypt", "skipped", "not password protected", rec.elapsed_ms)

        rec.timed()
        with pdfplumber.open(workfile) as document:
            pages = len(document.pages)
            chars = sum(len(page.chars) for page in document.pages)
            text = "\n".join((page.extract_text() or "") for page in document.pages)
        if not chars:
            rec.step("classify", "failed", f"{pages} pages; image-only PDF", rec.elapsed_ms)
            rec.end_file(
                "failed", error="scanned PDF; OCR is not supported yet",
                encrypted=int(encrypted), document_type="bank_account",
            )
            return False
        kind, reason = document_kind(text)
        if kind != "bank_account":
            rec.step("classify", "failed", reason, rec.elapsed_ms)
            rec.end_file(
                "failed", error="uploaded PDF is not a bank account statement",
                encrypted=int(encrypted), document_type="bank_account",
            )
            return False
        rec.step("classify", "ok", f"{pages} pages, {chars:,} chars — {reason}", rec.elapsed_ms)

        rec.timed()
        statement = parse_bank_pdf(workfile)
        statement.source_file = pdf.name
        elapsed = rec.elapsed_ms
        rec.step("fingerprint", "ok", f"matched {statement.parser_id}")
        rec.step(
            "extract", "ok" if statement.transactions else "failed",
            f"{len(statement.transactions)} transactions, statement period "
            f"{statement.period_start} → {statement.period_end}", elapsed,
        )

        errors = [check for check in statement.checks if not check.passed and check.severity == "error"]
        detail = "; ".join(f"{check.name}: {check.detail}" for check in statement.checks)
        rec.step(
            "validate", "failed" if errors else "ok",
            f"{len(statement.checks) - len(errors)}/{len(statement.checks)} passed — {detail}",
        )
        common = dict(
            issuer=statement.bank_name,
            product=statement.account_type or statement.product,
            card=bank_store.display_name(statement),
            template_id=statement.parser_id,
            encrypted=int(encrypted),
            txn_count=len(statement.transactions),
            confidence=statement.confidence,
            checks_json=json.dumps([check.model_dump() for check in statement.checks]),
            path=str(pdf),
            period_start=statement.period_start.isoformat(),
            period_end=statement.period_end.isoformat(),
            document_type="bank_account",
        )
        conn = store.connect(rec.db_path)
        try:
            duplicate = bank_store.find_statement(conn, statement)
            existing_owner = conn.execute(
                """SELECT a.member_id, m.name FROM bank_accounts a
                   LEFT JOIN members m ON m.id = a.member_id
                   WHERE a.account_fingerprint = ?""",
                (statement.account_fingerprint,),
            ).fetchone()
        finally:
            conn.close()
        common["duplicate_of"] = (duplicate or {}).get("id")

        # Warn before the import, not after: the account's existing owner wins, so
        # this is the last point where the user can still redirect it.
        requested_member = creds.get("_member_id")
        if (
            existing_owner and requested_member
            and existing_owner["member_id"] not in (None, requested_member)
        ):
            rec.step(
                "attribute", "warning",
                f"this account already belongs to {existing_owner['name']} — importing files "
                f"the statement under them, not the member selected above.",
            )

        if not commit:
            rec.step(
                "store", "skipped",
                "already imported — approving will replace it" if duplicate
                else "awaiting your approval",
            )
            rec.end_file(
                "pending", error="failed validation" if errors else None, **common
            )
            return False
        if errors:
            rec.step("store", "skipped", "validation failed; bank statement was not imported")
            rec.end_file("failed", error="failed validation", **common)
            return False

        rec.timed()
        conn = store.connect(rec.db_path)
        try:
            _, rows, replaced, account_id = bank_store.import_statement(
                conn, statement, requested_member
            )
            owner = conn.execute(
                """SELECT a.member_id, m.name FROM bank_accounts a
                   LEFT JOIN members m ON m.id = a.member_id WHERE a.id = ?""",
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
        rec.step(
            "store", "ok",
            f"{'replaced existing' if replaced else 'inserted'} statement, {rows} rows",
            rec.elapsed_ms,
        )
        # An account keeps its owner across re-imports, so a statement for an account
        # someone else already owns does not move it. Say so — the alternative is an
        # import that reports success while the data lands under another member.
        if owner and requested_member and owner["member_id"] not in (None, requested_member):
            rec.step(
                "attribute", "warning",
                f"this account already belongs to {owner['name']}, so the statement was filed "
                f"under them. Change the owner on the Bank Accounts tab if that is wrong.",
            )
        rec.end_file("ok", bank_account_id=account_id, **common)
        return True
    except (UnsupportedBankStatement, DecryptError) as exc:
        rec.step("extract", "failed", str(exc))
        rec.end_file(
            "failed", error=str(exc), encrypted=int(encrypted), document_type="bank_account",
        )
        return False
    except Exception as exc:
        rec.step("extract", "failed", f"{type(exc).__name__}: {exc}")
        rec.end_file(
            "failed", error=traceback.format_exc(limit=3), encrypted=int(encrypted),
            document_type="bank_account",
        )
        return False
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def run_scan(db_path: Path, pdfs: Iterable[Path], creds: Optional[dict] = None) -> int:
    files = [Path(pdf) for pdf in pdfs]
    recorder = Recorder(
        db_path, "bank_scan", f"{len(files)} uploaded file(s)",
        member_id=(creds or {}).get("_member_id"),
    )
    try:
        for pdf in files:
            ingest_file(recorder, pdf, creds or {}, commit=False)
        pending_count = recorder.conn.execute(
            "SELECT COUNT(*) FROM ingest_files WHERE run_id=? AND status='pending'",
            (recorder.run_id,),
        ).fetchone()[0]
        recorder.finish("done", f"{pending_count} statement(s) awaiting approval")
    except Exception as exc:
        recorder.finish("failed", str(exc))
    return recorder.run_id


def pending(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT f.*, r.kind, r.note, m.name AS member_name FROM ingest_files f
           JOIN ingest_runs r ON r.id=f.run_id
           LEFT JOIN members m ON m.id=f.member_id
           WHERE f.status='pending' AND f.document_type='bank_account'
           ORDER BY f.id DESC"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["checks"] = json.loads(item.pop("checks_json") or "[]")
        item["encrypted"] = bool(item["encrypted"])
        item["is_duplicate"] = item.get("duplicate_of") is not None
        result.append(item)
    return result


def discard(conn, file_ids: list[int]) -> int:
    if not file_ids:
        return 0
    cur = conn.execute(
        f"""UPDATE ingest_files SET status='discarded'
            WHERE status='pending' AND document_type='bank_account'
            AND id IN ({','.join('?' * len(file_ids))})""",
        file_ids,
    )
    conn.commit()
    return cur.rowcount


def run_reevaluate(db_path: Path, file_ids: list[int], creds: Optional[dict] = None) -> int:
    """Reparse bank review rows while keeping the previous row until replacement succeeds."""
    conn = store.connect(db_path)
    try:
        where = "WHERE status='pending' AND document_type='bank_account'"
        args: list[int] = []
        if file_ids:
            where += f" AND id IN ({','.join('?' * len(file_ids))})"
            args = file_ids
        rows = conn.execute(
            f"SELECT id, path, member_id FROM ingest_files {where} ORDER BY id", args
        ).fetchall()
        sources = [
            (int(row["id"]), Path(row["path"]), row["member_id"]) for row in rows if row["path"]
        ]
    finally:
        conn.close()

    recorder = Recorder(db_path, "bank_reevaluate", f"{len(sources)} pending statement(s)")
    refreshed = 0
    try:
        for source_id, pdf, source_member in sources:
            if not pdf.exists():
                continue
            # A reparse must not relabel the statement; carry the original member.
            recorder.member_id = source_member
            ingest_file(recorder, pdf, creds or {}, commit=False)
            replacement_id = recorder.file_id
            resolved = store.connect(db_path)
            try:
                replacement = resolved.execute(
                    "SELECT status FROM ingest_files WHERE id=?", (replacement_id,)
                ).fetchone()
                if replacement and replacement["status"] == "pending":
                    resolved.execute(
                        "UPDATE ingest_files SET status='reevaluated' WHERE id=?", (source_id,)
                    )
                    resolved.commit()
                    refreshed += 1
            finally:
                resolved.close()
        recorder.finish("done", f"{refreshed}/{len(sources)} pending statement(s) refreshed")
    except Exception as exc:
        recorder.finish("failed", str(exc))
    return recorder.run_id


def run_approve(db_path: Path, file_ids: list[int], creds: Optional[dict] = None) -> int:
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT id, path, member_id FROM ingest_files
                WHERE status='pending' AND document_type='bank_account'
                AND id IN ({','.join('?' * len(file_ids))}) ORDER BY id""",
            file_ids,
        ).fetchall()
        sources = [
            (int(row["id"]), Path(row["path"]), row["member_id"]) for row in rows if row["path"]
        ]
    finally:
        conn.close()

    creds = creds or {}
    recorder = Recorder(db_path, "bank_approve", f"{len(sources)} approved")
    imported = 0
    try:
        for source_id, pdf, source_member in sources:
            # The member chosen when the file was uploaded wins: approval can happen
            # days later, with a different member selected in the UI.
            member_id = source_member if source_member is not None else creds.get("_member_id")
            recorder.member_id = member_id
            file_creds = {**creds, "_member_id": member_id}
            if not pdf.exists():
                recorder.begin_file(pdf.name)
                recorder.step("download", "failed", "file no longer on disk — upload it again")
                recorder.end_file("failed", error="missing file", document_type="bank_account")
                continue
            imported += bool(ingest_file(recorder, pdf, file_creds, commit=True))
            resolved = store.connect(db_path)
            try:
                resolved.execute(
                    "UPDATE ingest_files SET status='approved' WHERE id=?", (source_id,)
                )
                resolved.commit()
            finally:
                resolved.close()
        recorder.finish("done", f"{imported}/{len(sources)} imported")
    except Exception as exc:
        recorder.finish("failed", str(exc))
    return recorder.run_id


def runs(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id) files,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id AND f.status='ok') ok,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id AND f.status='pending') pending
           FROM ingest_runs r WHERE r.kind LIKE 'bank_%'
           ORDER BY r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
