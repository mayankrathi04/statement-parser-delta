"""Ingest pipeline for bank account statements: uploads, local files and mail.

This module does not call the credit-card engine or its YAML templates. Bank
parsers have their own dispatch boundary so a future Axis/ICICI account parser
can be registered here without changing card behaviour.

The mail sweep is the card sweep with different arguments, not a second
implementation of it: ``mailbox.fetch_bank_account`` reuses the same IMAP search,
naming and deduplication, and only the header classifier, the attachment
requirement and the account filter differ. What arrives is parsed and queued for
review exactly like an upload, so approval, re-evaluation and attribution have
one code path regardless of where the PDF came from.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import traceback
from pathlib import Path
from typing import Iterable, Optional

import pdfplumber

from . import accounts, bank_store, inbox, store
from .banks import UnsupportedBankStatement, parse_bank_pdf
from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from .doctype import document_kind
from .pipeline import Recorder, month_range_window, month_window

log = logging.getLogger("sparser.bank_pipeline")


def _password_candidates(db_path: Path, creds: dict) -> list[str]:
    """Ordered: what the caller supplied, what is already known to work, then the
    conventions derived from the saved profiles. Guessing is always the last resort."""
    conn = store.connect(db_path)
    try:
        # Every member's profile, not just the selected one: a sweep files each
        # statement under whoever owns the mailbox it came from, so the password
        # a PDF wants may be derived from a member the run never mentions.
        profiles = accounts.all_profiles(conn, creds.get("_member_id"))
        known = accounts.all_bank_passwords(conn)
    finally:
        conn.close()
    return list(dict.fromkeys(
        ([creds["password"]] if creds.get("password") else [])
        + list(known.values())
        + [
            password
            for profile in profiles
            for password in candidate_passwords(
                creds.get("name") or profile.get("full_name"),
                creds.get("dob") or profile.get("dob"),
            )
        ]
    ))


def ingest_file(
    rec: Recorder,
    pdf: Path,
    creds: dict,
    *,
    commit: bool = True,
    downloaded: bool = False,
    account_filter: Optional["AccountFilter"] = None,
    inbox_root: Optional[Path] = None,
) -> bool:
    """One account statement through every stage, recording each.

    The card pipeline's twin, including where the PDF ends up: a file inside the
    inbox is moved into its account's folder the moment parsing identifies the
    account, so every path recorded below is the filed one.
    """
    rec.begin_file(pdf.name)
    workfile = pdf
    temporary: Optional[Path] = None
    encrypted = False
    used_password: Optional[str] = None
    try:
        rec.step(
            "download", "ok",
            "fetched from mailbox" if downloaded else "received from browser upload",
        )

        rec.timed()
        encrypted = is_encrypted(pdf)
        if encrypted:
            handle = tempfile.NamedTemporaryFile(prefix="sparser-bank-", suffix=".pdf", delete=False)
            temporary = Path(handle.name)
            handle.close()
            candidates = _password_candidates(rec.db_path, creds)
            workfile, used = decrypt_to(pdf, temporary, candidates)
            used_password = used
            if creds.get("password") and used == creds["password"]:
                detail = "encrypted; opened with the supplied password"
            elif used:
                detail = f"encrypted; opened with a saved or derived password " \
                         f"({len(candidates)} candidates available)"
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

        # The account is known now, so the PDF can stop being unsorted.
        conn = store.connect(rec.db_path)
        try:
            label = bank_store.account_label(conn, statement)
        finally:
            conn.close()
        pdf = inbox.file_under(pdf, inbox_root or inbox.root(), inbox.BANK, label)

        # Which account this is can only be known after parsing, so a scan that
        # was asked for specific accounts decides here — the same point the card
        # pipeline decides, and for the same reason.
        if account_filter is not None:
            keep, why = account_filter.verdict(
                statement.account_fingerprint, statement.last4
            )
            if not keep:
                rec.step("validate", "skipped", why)
                rec.step("store", "skipped", why)
                rec.end_file(
                    "skipped", issuer=statement.bank_name,
                    product=statement.account_type or statement.product,
                    card=bank_store.display_name(statement),
                    template_id=statement.parser_id, encrypted=int(encrypted),
                    txn_count=len(statement.transactions), confidence=statement.confidence,
                    path=str(pdf), period_start=statement.period_start.isoformat(),
                    period_end=statement.period_end.isoformat(),
                    document_type="bank_account", error=why,
                )
                return False

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
            # What approving this would add, counted before anything is written.
            preview = bank_store.import_preview(conn, statement)
            existing_owner = conn.execute(
                """SELECT a.member_id, m.name FROM bank_accounts a
                   LEFT JOIN members m ON m.id = a.member_id
                   WHERE a.account_fingerprint = ?""",
                (statement.account_fingerprint,),
            ).fetchone()
        finally:
            conn.close()
        common["duplicate_of"] = (duplicate or {}).get("id")
        common["new_txn_count"] = preview["new"]
        common["known_txn_count"] = preview["known"]

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
            if preview["total"] and not preview["new"]:
                waiting = (
                    f"every one of these {preview['total']} transactions is already in the "
                    f"ledger — approving re-files them, it does not add any"
                )
            elif preview["known"]:
                waiting = (
                    f"{preview['new']} new transaction(s); {preview['known']} already in the "
                    f"ledger from a statement covering the same days"
                )
            elif duplicate:
                waiting = "already imported — approving will replace it"
            else:
                waiting = "awaiting your approval"
            rec.step("store", "skipped", waiting)
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
            _, rows, replaced, account_id, already_held = bank_store.import_statement(
                conn, statement, requested_member
            )
            owner = conn.execute(
                """SELECT a.member_id, m.name FROM bank_accounts a
                   LEFT JOIN members m ON m.id = a.member_id WHERE a.id = ?""",
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
        stored = f"{'replaced existing' if replaced else 'inserted'} statement, {rows} rows"
        if already_held:
            stored += f"; {already_held} already in the ledger from another statement"
        rec.step("store", "ok", stored, rec.elapsed_ms)
        # An account keeps its owner across re-imports, so a statement for an account
        # someone else already owns does not move it. Say so — the alternative is an
        # import that reports success while the data lands under another member.
        if owner and requested_member and owner["member_id"] not in (None, requested_member):
            rec.step(
                "attribute", "warning",
                f"this account already belongs to {owner['name']}, so the statement was filed "
                f"under them. Change the owner on the Bank Accounts tab if that is wrong.",
            )
        # The account is only known after parsing, so a password that worked is
        # recorded here — next month this statement opens on the first try.
        if used_password:
            conn = store.connect(rec.db_path)
            try:
                if not accounts.bank_password(conn, statement.account_fingerprint):
                    accounts.set_bank_password(
                        conn, statement.account_fingerprint, used_password, source="learned"
                    )
            finally:
                conn.close()
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


class AccountFilter:
    """Which parsed statements a scan actually wants.

    The bank counterpart of ``pipeline.CardFilter``, and deliberately the same
    shape. A scan filters on the account the *parser* found, so the decision can
    only be made after extraction. Accounts not saved yet are tracked separately
    from accounts that are saved but unticked: the first is a statement the user
    has never seen and may well want, the second is one they deliberately
    excluded.
    """

    def __init__(
        self,
        allowed: Iterable[tuple[str, str]],
        known: Iterable[tuple[str, str]],
        include_unrecognized: bool,
    ):
        # (fingerprint, last4) pairs. The fingerprint is exact; the last four are
        # what a person recognises, and are used only to explain the verdict.
        self.allowed = {fingerprint for fingerprint, _ in allowed}
        self.known = {fingerprint for fingerprint, _ in known}
        self.include_unrecognized = include_unrecognized

    def verdict(self, fingerprint: str, last4: str) -> tuple[bool, str]:
        shown = f"the account ending {last4}" if last4 else "an unreadable account number"
        if fingerprint in self.allowed:
            return True, f"{shown} is selected for this scan"
        if fingerprint in self.known:
            return False, f"{shown} is a saved account that was not ticked for this scan"
        if self.include_unrecognized:
            return True, f"{shown} matches no saved account — kept as an unrecognized account"
        return False, (
            f"{shown} matches no saved account. Tick “Unrecognized accounts” in the account "
            f"filter to scan statements for accounts you have not imported yet."
        )


def _scan_account_rule_groups(
    conn,
    account_ids: Optional[list[int]],
    default_senders: Iterable[str],
    default_subjects: Iterable[str],
    include_unrecognized: bool = False,
) -> tuple[Optional[AccountFilter], list[tuple[set[str], set[str]]]]:
    """Resolve the account filter and the sender/subject unions for the selection.

    Mirrors ``pipeline._scan_card_rule_groups`` exactly: an empty ``account_ids``
    means every saved account, fully configured accounts share one strict query,
    and accounts missing either field fall back to the bank defaults in a
    separate query so their defaults cannot broaden the strict one.
    """
    rows = conn.execute(
        "SELECT id, account_fingerprint, last4, sender_ids_json, subject_patterns_json "
        "FROM bank_accounts"
    ).fetchall()
    wanted = set(account_ids or [])
    chosen = [row for row in rows if not wanted or row["id"] in wanted]

    account_filter = None
    if account_ids or include_unrecognized:
        account_filter = AccountFilter(
            allowed={(row["account_fingerprint"], row["last4"]) for row in chosen},
            known={(row["account_fingerprint"], row["last4"]) for row in rows},
            include_unrecognized=include_unrecognized,
        )

    default_sender_set = set(default_senders)
    default_subject_set = set(default_subjects)
    strict_senders: set[str] = set()
    strict_subjects: set[str] = set()
    fallback_senders: set[str] = set()
    fallback_subjects: set[str] = set()
    for row in chosen:
        own_senders = set(json.loads(row["sender_ids_json"] or "[]"))
        own_subjects = set(json.loads(row["subject_patterns_json"] or "[]"))
        if own_senders and own_subjects:
            strict_senders.update(own_senders)
            strict_subjects.update(own_subjects)
        else:
            fallback_senders.update(own_senders or default_sender_set)
            fallback_subjects.update(own_subjects or default_subject_set)

    groups: list[tuple[set[str], set[str]]] = []
    if strict_senders and strict_subjects:
        groups.append((strict_senders, strict_subjects))
    if fallback_senders and fallback_subjects:
        groups.append((fallback_senders, fallback_subjects))
    # An account nobody has imported has no saved mail rules to search by, so
    # asking for unrecognized accounts has to widen the query to the bank
    # defaults; without this the statement is never downloaded to be judged.
    default_group = (default_sender_set, default_subject_set)
    if include_unrecognized and default_group not in groups:
        groups.append(default_group)
    if not groups:
        groups.append(default_group)
    return account_filter, groups


def run_scan_mail(
    db_path: Path,
    dest: Path,
    creds: Optional[dict] = None,
    months: int = 1,
    month: Optional[str] = None,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    account_ids: Optional[list[int]] = None,
    connection_ids: Optional[list[int]] = None,
    include_unrecognized_accounts: bool = False,
) -> int:
    """Sweep the connected mailboxes for account statements, storing nothing.

    The card scan's twin. What it downloads lands in the same review queue the
    upload path fills, so nothing here can write to the bank ledger.
    """
    from . import mailbox

    creds = creds or {}
    if month_from and month_to:
        label = f"months {month_from} → {month_to}"
    else:
        label = f"month {month}" if month else f"last {months} month(s)"
    rec = Recorder(db_path, "bank_scan", label, member_id=creds.get("_member_id"))
    try:
        conn = store.connect(db_path)
        try:
            # Only mailboxes the user marked as carrying bank statements.
            accts = mailbox.accounts_from_store(conn, purpose="bank")
            if connection_ids:
                marks = ",".join("?" for _ in connection_ids)
                selected = {
                    row["address"] for row in conn.execute(
                        f"SELECT address FROM mailboxes WHERE id IN ({marks})", connection_ids
                    ).fetchall()
                }
                accts = [acct for acct in accts if acct.address in selected]
            # As on the card side: the fallback lists are the ones the Bank
            # Accounts tab shows, not a hardcoded pair the user cannot see.
            fallback = mailbox.scan_defaults(conn, "bank")
            account_filter, rule_groups = _scan_account_rule_groups(
                conn, account_ids, fallback["senders"], fallback["subjects"],
                include_unrecognized_accounts,
            )
        finally:
            conn.close()
        if not accts:
            rec.begin_file("(mailbox)")
            rec.step(
                "download", "failed",
                "No mailbox is enabled for bank statements. Connect one, or tick “Bank "
                "statements” for an existing connection on the Connections tab.",
            )
            rec.end_file("failed", error="no mailbox credentials", document_type="bank_account")
            rec.finish("failed", "no mailbox enabled for bank statements")
            return rec.run_id

        if month_from and month_to:
            since, before = month_range_window(month_from, month_to)
        elif month:
            since, before = month_window(month)
        else:
            since, before = dt.date.today() - dt.timedelta(days=31 * months), None

        # HDFC's smart statement is fetched, not attached, and the gate wants the
        # same password the PDF would have wanted. Resolve the candidates once.
        passwords = _password_candidates(db_path, creds)

        found: list[Path] = []
        member_of: dict[Path, Optional[int]] = {}
        for acct in accts:
            conn = store.connect(db_path)
            try:
                account_found: list[Path] = []
                for scan_senders, scan_subjects in rule_groups:
                    account_found += mailbox.fetch_bank_account(
                        acct, Path(dest), since=since, before=before, verbose=False,
                        include_existing=True, senders=scan_senders,
                        subject_searches=scan_subjects, passwords=passwords,
                    )
                found += account_found
                for path in account_found:
                    member_of.setdefault(path, acct.member_id)
                accounts.mark(
                    conn, acct.address, "connected",
                    f"{len(set(account_found))} account statement(s) for {label}", synced=True,
                )
            except Exception as exc:
                accounts.mark(conn, acct.address, "failed", str(exc)[:200])
                rec.begin_file(f"({acct.address})")
                rec.step("download", "failed", str(exc)[:300])
                rec.end_file("failed", error=str(exc)[:300], document_type="bank_account")
            finally:
                conn.close()

        found = list(dict.fromkeys(found))
        for pdf in found:
            owner = member_of.get(pdf)
            rec.member_id = owner if owner is not None else creds.get("_member_id")
            ingest_file(
                rec, pdf, creds, commit=False, downloaded=True, account_filter=account_filter,
                inbox_root=dest,
            )
        pending_count = rec.conn.execute(
            "SELECT COUNT(*) FROM ingest_files WHERE run_id=? AND status='pending'",
            (rec.run_id,),
        ).fetchone()[0]
        rec.finish(
            "done",
            f"{pending_count} selected statement(s) found for {label} — awaiting approval "
            f"({len(found)} matching attachment(s) examined)",
        )
    except Exception as exc:
        log.exception("bank mail scan failed")
        rec.finish("failed", str(exc))
    return rec.run_id


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
