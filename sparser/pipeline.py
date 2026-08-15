"""Ingest orchestration with a recorded, step-by-step audit trail.

Every PDF walks the same stages — download, decrypt, classify, fingerprint,
extract, validate, store — and each stage's outcome is written to the database as
it happens. That makes the pipeline observable *while it runs* (the UI polls) and
still explicable months later, which matters because "why is this statement
missing?" is the question you actually get asked.

The recorder writes on its own connection: the run happens on a worker thread and
SQLite connections are not shareable across threads.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import traceback
from pathlib import Path
from typing import Iterable, Optional

import pdfplumber

from . import store
from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from .doctype import document_kind
from .engine import NoTemplateMatch, TemplateError, parse_pdf

STEPS = ["download", "decrypt", "classify", "fingerprint", "extract", "validate", "store"]
log = logging.getLogger("sparser.pipeline")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Recorder:
    """Writes run/file/step rows as the pipeline advances."""

    def __init__(
        self, db_path: Path, kind: str, note: str = "", member_id: Optional[int] = None
    ):
        self.db_path = Path(db_path)
        self.conn = store.connect(self.db_path)
        cur = self.conn.execute(
            "INSERT INTO ingest_runs (kind, status, started_at, note) VALUES (?,?,?,?)",
            (kind, "running", _now(), note),
        )
        self.run_id = int(cur.lastrowid)
        self.conn.commit()
        self.file_id: Optional[int] = None
        # Stamped onto every file row. Callers that mix members in one run (a mail
        # sweep across mailboxes, an approval of a batch) reassign this per file.
        self.member_id = member_id
        self._seq = 0
        self._t0 = 0.0
        log.info("run #%s started: %s (%s)", self.run_id, kind, note or "no note")

    # ---------------------------------------------------------- file scope
    def begin_file(self, filename: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_files (run_id, filename, status, started_at, member_id)"
            " VALUES (?,?,?,?,?)",
            (self.run_id, filename, "running", _now(), self.member_id),
        )
        self.file_id = int(cur.lastrowid)
        self._seq = 0
        self.conn.commit()
        log.info("run #%s file started: %s", self.run_id, filename)
        return self.file_id

    def end_file(self, status: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        args = list(fields.values())
        self.conn.execute(
            f"UPDATE ingest_files SET status = ?, finished_at = ?{', ' + sets if sets else ''}"
            " WHERE id = ?",
            [status, _now(), *args, self.file_id],
        )
        self.conn.commit()
        log.info("run #%s file finished: %s", self.run_id, status)

    # ---------------------------------------------------------- step scope
    def step(self, name: str, status: str, detail: str = "", ms: Optional[int] = None) -> None:
        self._seq += 1
        self.conn.execute(
            "INSERT INTO ingest_steps (file_id, seq, name, status, detail, ms) VALUES (?,?,?,?,?,?)",
            (self.file_id, self._seq, name, status, detail[:400], ms),
        )
        self.conn.commit()
        timing = f" [{ms} ms]" if ms is not None else ""
        log.info(
            "run #%s %s: %s%s — %s",
            self.run_id, name, status, timing, detail[:240] or "no detail",
        )

    def timed(self):
        self._t0 = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def finish(self, status: str = "done", note: Optional[str] = None) -> None:
        if note is None:
            self.conn.execute(
                "UPDATE ingest_runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), self.run_id),
            )
        else:
            self.conn.execute(
                "UPDATE ingest_runs SET status = ?, finished_at = ?, note = ? WHERE id = ?",
                (status, _now(), note, self.run_id),
            )
        self.conn.commit()
        self.conn.close()
        log.info("run #%s finished: %s — %s", self.run_id, status, note or "complete")


def ingest_file(
    rec: Recorder,
    pdf: Path,
    creds: dict,
    *,
    force: bool,
    downloaded: bool = False,
    commit: bool = True,
    card_filter: Optional["CardFilter"] = None,
) -> bool:
    """Run one PDF through every stage, recording each.

    With commit=False the document is parsed and validated but nothing is written
    to the analytics tables. That is what makes the review step honest: the list
    the user approves is built from a real parse, not a guess from the filename.
    """
    rec.begin_file(pdf.name)
    workfile = pdf
    tmp: Optional[Path] = None
    used_password: Optional[str] = None
    try:
        rec.step("download", "ok" if downloaded else "skipped",
                 "fetched from mailbox" if downloaded else "already on disk")

        # --- decrypt -------------------------------------------------------
        rec.timed()
        encrypted = is_encrypted(pdf)
        if encrypted:
            # Order matters: an explicit password, then the ones already known to
            # work for a card, then the issuer conventions. Guessing is the last
            # resort, and a card whose password does not follow any convention
            # only ever needs to be entered once.
            from . import accounts as acct_store

            conn = store.connect(rec.db_path)
            try:
                known = acct_store.all_card_passwords(conn)
            finally:
                conn.close()

            # Name and DOB come from the saved profile unless the caller passed
            # their own, so a scheduled fetch needs no arguments at all.
            conn = store.connect(rec.db_path)
            try:
                profile = acct_store.get_profile(conn)
            finally:
                conn.close()
            who = creds.get("name") or profile.get("full_name")
            born = creds.get("dob") or profile.get("dob")

            pws = (
                ([creds["password"]] if creds.get("password") else [])
                + list(dict.fromkeys(known.values()))
                + candidate_passwords(who, born, creds.get("card_last4"))
            )
            tmp = pdf.with_suffix(".decrypted.pdf")
            workfile, used = decrypt_to(pdf, tmp, pws)
            used_password = used
            if creds.get("password") and used == creds["password"]:
                how = "the password you supplied"
            elif used in known.values():
                how = "a password already saved for one of your cards"
            elif used:
                how = f"a derived issuer pattern ({len(pws)} candidates tried)"
            else:
                how = "no password needed"
            rec.step("decrypt", "ok", f"encrypted; opened with {how}", rec.elapsed_ms)
        else:
            rec.step("decrypt", "skipped", "not password protected", rec.elapsed_ms)

        # --- classify ------------------------------------------------------
        rec.timed()
        with pdfplumber.open(workfile) as doc:
            pages = len(doc.pages)
            chars = sum(len(p.chars) for p in doc.pages)
            text = "\n".join((p.extract_text() or "") for p in doc.pages)
        kind = "digital text layer" if chars > 50 * pages else "image-only (needs OCR)"
        if not chars:
            rec.step("classify", "failed",
                     f"{pages} pages, {chars:,} chars — {kind}", rec.elapsed_ms)
            rec.end_file("failed", error="scanned PDF; OCR not supported yet", encrypted=int(encrypted))
            return False

        doc_kind, doc_reason = document_kind(text)
        if doc_kind == "bank_account":
            rec.step("classify", "failed",
                     f"{pages} pages, {chars:,} chars — {doc_reason}", rec.elapsed_ms)
            rec.end_file("skipped", error=doc_reason, encrypted=int(encrypted))
            return False
        rec.step("classify", "ok",
                 f"{pages} pages, {chars:,} chars — {kind}; {doc_reason}", rec.elapsed_ms)

        # --- fingerprint + extract ----------------------------------------
        rec.timed()
        stmt = parse_pdf(workfile)
        stmt.source_file = pdf.name
        parse_ms = rec.elapsed_ms
        generic = stmt.template_id == "generic"
        rec.step("fingerprint", "ok" if not generic else "failed",
                 f"matched {stmt.template_id}" if not generic
                 else "no template matched — parsed generically, totals unverified")
        rec.step("extract", "ok" if stmt.transactions else "failed",
                 f"{len(stmt.transactions)} transactions, period "
                 f"{stmt.period_start} → {stmt.period_end}", parse_ms)

        if card_filter is not None:
            keep, why = card_filter.verdict(stmt.account_masked)
            if not keep:
                rec.step("validate", "skipped", why)
                rec.step("store", "skipped", why)
                rec.end_file(
                    "skipped", issuer=stmt.issuer, product=stmt.product,
                    card=f"{stmt.issuer} {stmt.product or ''}".strip(),
                    template_id=stmt.template_id, encrypted=int(encrypted),
                    txn_count=len(stmt.transactions), confidence=stmt.confidence,
                    path=str(pdf), statement_date=str(stmt.statement_date or ""),
                    period_start=str(stmt.period_start or ""), period_end=str(stmt.period_end or ""),
                    error=why,
                )
                return False

        # --- validate ------------------------------------------------------
        errs = [c for c in stmt.checks if not c.passed and c.severity == "error"]
        detail = "; ".join(f"{c.name}: {c.detail}" for c in stmt.checks) or "no checks defined"
        rec.step("validate", "ok" if not errs else "failed",
                 f"{len(stmt.checks) - len(errs)}/{len(stmt.checks)} passed — {detail}")

        card = f"{stmt.issuer} {stmt.product or ''}".strip()
        conn = store.connect(rec.db_path)
        try:
            dupe = store.find_statement(conn, stmt)
        finally:
            conn.close()

        common = dict(
            issuer=stmt.issuer, product=stmt.product, card=card, template_id=stmt.template_id,
            encrypted=int(encrypted), txn_count=len(stmt.transactions),
            confidence=stmt.confidence,
            checks_json=json.dumps([c.model_dump() for c in stmt.checks]),
            path=str(pdf), statement_date=str(stmt.statement_date or ""),
            period_start=str(stmt.period_start or ""), period_end=str(stmt.period_end or ""),
            duplicate_of=(dupe or {}).get("id"),
        )

        if not commit:
            rec.step(
                "store", "skipped",
                "already imported — approving will replace it" if dupe
                else "awaiting approval",
            )
            rec.end_file("pending", **common)
            return False

        if errs and not force:
            rec.step("store", "skipped", "validation failed; not imported (use force to override)")
            rec.end_file("failed", error="failed validation", **common)
            return False

        # --- store ---------------------------------------------------------
        rec.timed()
        requested_member = creds.get("_member_id")
        conn = store.connect(rec.db_path)
        try:
            _, rows, replaced = store.import_statement(conn, stmt, requested_member)
            owner = conn.execute(
                """SELECT c.member_id, m.name FROM cards c
                   LEFT JOIN members m ON m.id = c.member_id
                   WHERE c.masked_number = ?""",
                (stmt.account_masked,),
            ).fetchone()
        finally:
            conn.close()
        rec.step("store", "ok",
                 f"{'replaced existing' if replaced else 'inserted'} statement, {rows} rows",
                 rec.elapsed_ms)
        # A card keeps its owner across re-imports, so a statement for a card someone
        # else already owns does not move it. Say so rather than reporting a plain
        # success while the data lands under another member.
        if owner and requested_member and owner["member_id"] not in (None, requested_member):
            rec.step(
                "attribute", "warning",
                f"this card already belongs to {owner['name']}, so the statement was filed "
                f"under them. Change the owner on the Cards tab if that is wrong.",
            )

        # The card is only known after parsing, so a working password is recorded
        # here — next month this file opens on the first try instead of guessing.
        if used_password and stmt.account_masked:
            from . import accounts as acct_store

            conn = store.connect(rec.db_path)
            try:
                if not acct_store.card_password(conn, stmt.account_masked):
                    acct_store.set_card_password(
                        conn, stmt.account_masked, used_password, source="learned"
                    )
            finally:
                conn.close()
        rec.end_file("ok", **common)
        return True

    except (NoTemplateMatch, DecryptError, TemplateError) as exc:
        rec.step("extract", "failed", str(exc))
        rec.end_file("failed", error=str(exc))
        return False
    except Exception as exc:  # never let one bad PDF kill the run
        rec.step("extract", "failed", f"{type(exc).__name__}: {exc}")
        rec.end_file("failed", error=traceback.format_exc(limit=3))
        return False
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def run_import(db_path: Path, pdfs: Iterable[Path], creds: dict, force: bool = False) -> int:
    pdfs = list(pdfs)
    rec = Recorder(db_path, "import", f"{len(pdfs)} file(s)", member_id=creds.get("_member_id"))
    ok = 0
    try:
        for pdf in pdfs:
            ok += bool(ingest_file(rec, Path(pdf), creds, force=force))
        rec.finish("done", f"{ok}/{len(pdfs)} imported")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def month_window(month: str) -> tuple[dt.date, dt.date]:
    """"2026-08" -> (first of that month, first of the next).

    Statements for a cycle land in the mail a few days after it closes, so the
    window is widened to the surrounding weeks rather than the calendar month.
    """
    year, mon = (int(x) for x in month.split("-")[:2])
    start = dt.date(year, mon, 1)
    end = dt.date(year + (mon == 12), (mon % 12) + 1, 1)
    return start - dt.timedelta(days=7), end + dt.timedelta(days=10)


def month_range_window(month_from: str, month_to: str) -> tuple[dt.date, dt.date]:
    since, _ = month_window(month_from)
    _, before = month_window(month_to)
    if since >= before:
        raise ValueError("month_from must not be after month_to")
    return since, before


class CardFilter:
    """Which parsed statements a scan is allowed to keep.

    A scan filters on the card the *parser* found, not on the filename, so the
    decision can only be made after extraction. Cards that are not saved yet are
    tracked separately from cards that are saved but unticked: the first is a
    statement the user has never seen and may well want, the second is one they
    deliberately excluded.
    """

    def __init__(self, allowed: Iterable[str], known: Iterable[str], include_unrecognized: bool):
        self.allowed = set(allowed)
        self.known = set(known)
        self.include_unrecognized = include_unrecognized

    def verdict(self, mask: Optional[str]) -> tuple[bool, str]:
        """(keep?, why) for a statement parsed from ``mask``."""
        shown = mask or "an unreadable card number"
        # Matched the way the importer matches, so a card whose mask changed
        # format between statement eras is still recognised as itself.
        if any(store.same_card(saved, mask) for saved in self.allowed):
            return True, f"{shown} is selected for this scan"
        if any(store.same_card(saved, mask) for saved in self.known):
            return False, f"{shown} is a saved card that was not ticked for this scan"
        if self.include_unrecognized:
            return True, f"{shown} matches no saved card — kept as an unrecognized card"
        return False, (
            f"{shown} matches no saved card. Tick “Unrecognized cards” in the card "
            f"filter to scan statements for cards you have not imported yet."
        )


def _scan_card_rule_groups(
    conn,
    card_ids: Optional[list[int]],
    default_senders: Iterable[str],
    default_subjects: Iterable[str],
    include_unrecognized: bool = False,
) -> tuple[Optional[CardFilter], list[tuple[set[str], set[str]]]]:
    """Resolve the card filter and the sender/subject unions for selected cards.

    An empty ``card_ids`` list means every saved card.  Fully configured cards
    share one strict query. Cards missing either field share a separate fallback
    query, so their defaults cannot broaden the strict query.
    """
    rows = conn.execute(
        "SELECT id, masked_number, sender_ids_json, subject_patterns_json FROM cards"
    ).fetchall()
    wanted = set(card_ids or [])
    chosen = [row for row in rows if not wanted or row["id"] in wanted]

    card_filter = None
    if card_ids or include_unrecognized:
        card_filter = CardFilter(
            allowed={row["masked_number"] for row in chosen},
            known={row["masked_number"] for row in rows},
            include_unrecognized=include_unrecognized,
        )

    default_sender_set = set(default_senders)
    default_subject_set = set(default_subjects)
    strict_senders: set[str] = set()
    strict_subjects: set[str] = set()
    fallback_senders: set[str] = set()
    fallback_subjects: set[str] = set()
    for row in chosen:
        card_senders = set(json.loads(row["sender_ids_json"] or "[]"))
        card_subjects = set(json.loads(row["subject_patterns_json"] or "[]"))
        if card_senders and card_subjects:
            strict_senders.update(card_senders)
            strict_subjects.update(card_subjects)
        else:
            fallback_senders.update(card_senders or default_sender_set)
            fallback_subjects.update(card_subjects or default_subject_set)

    groups: list[tuple[set[str], set[str]]] = []
    if strict_senders and strict_subjects:
        groups.append((strict_senders, strict_subjects))
    if fallback_senders and fallback_subjects:
        groups.append((fallback_senders, fallback_subjects))
    # A card nobody has imported has no saved mail rules to search by, so asking
    # for unrecognized cards has to widen the mailbox query to the issuer
    # defaults; without this the statement is never downloaded to be judged.
    default_group = (default_sender_set, default_subject_set)
    if include_unrecognized and default_group not in groups:
        groups.append(default_group)
    if not groups:
        groups.append(default_group)
    return card_filter, groups


def run_scan(
    db_path: Path,
    dest: Path,
    creds: dict,
    months: int = 1,
    month: Optional[str] = None,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    card_ids: Optional[list[int]] = None,
    connection_ids: Optional[list[int]] = None,
    include_unrecognized_cards: bool = False,
) -> int:
    """Download and parse, but store nothing — builds the review list."""
    from . import mailbox

    if month_from and month_to:
        label = f"months {month_from} → {month_to}"
    else:
        label = f"month {month}" if month else f"last {months} month(s)"
    rec = Recorder(db_path, "scan", label, member_id=creds.get("_member_id"))
    try:
        conn = store.connect(db_path)
        try:
            accts = mailbox.accounts_from_store(conn)
            if connection_ids:
                marks = ",".join("?" for _ in connection_ids)
                selected_addresses = {
                    row["address"] for row in conn.execute(
                        f"SELECT address FROM mailboxes WHERE id IN ({marks})", connection_ids
                    ).fetchall()
                }
                accts = [acct for acct in accts if acct.address in selected_addresses]
            card_filter, scan_rule_groups = _scan_card_rule_groups(
                conn, card_ids, mailbox.STATEMENT_SENDERS, mailbox.SUBJECT_SEARCHES,
                include_unrecognized_cards,
            )
        finally:
            conn.close()
        if not accts:
            rec.begin_file("(mailbox)")
            rec.step("download", "failed", "No mailboxes connected. Add one on Connections.")
            rec.end_file("failed", error="no mailbox credentials")
            rec.finish("failed", "no mailbox credentials")
            return rec.run_id

        if month_from and month_to:
            since, before = month_range_window(month_from, month_to)
        elif month:
            since, before = month_window(month)
        else:
            since, before = dt.date.today() - dt.timedelta(days=31 * months), None

        found: list[Path] = []
        # A sweep spans several mailboxes, each owned by a different member, so
        # attribution is per file rather than per run.
        member_of: dict[Path, Optional[int]] = {}
        from . import accounts as acct_store

        for acct in accts:
            conn = store.connect(db_path)
            try:
                account_found: list[Path] = []
                for scan_senders, scan_subjects in scan_rule_groups:
                    account_found += mailbox.fetch_account(
                        acct, Path(dest), since=since, before=before, verbose=False,
                        include_existing=True,
                        senders=scan_senders,
                        subject_searches=scan_subjects,
                    )
                found += account_found
                for path in account_found:
                    member_of.setdefault(path, acct.member_id)
                acct_store.mark(conn, acct.address, "connected",
                                f"{len(set(account_found))} attachment(s) for {label}", synced=True)
            except Exception as exc:
                acct_store.mark(conn, acct.address, "failed", str(exc)[:200])
                rec.begin_file(f"({acct.address})")
                rec.step("download", "failed", str(exc)[:300])
                rec.end_file("failed", error=str(exc)[:300])
            finally:
                conn.close()

        found = list(dict.fromkeys(found))
        for pdf in found:
            owner = member_of.get(pdf)
            rec.member_id = owner if owner is not None else creds.get("_member_id")
            ingest_file(
                rec, pdf, creds, force=False, downloaded=True, commit=False,
                card_filter=card_filter,
            )
        pending_count = rec.conn.execute(
            "SELECT COUNT(*) FROM ingest_files WHERE run_id = ? AND status = 'pending'",
            (rec.run_id,),
        ).fetchone()[0]
        rec.finish(
            "done",
            f"{pending_count} selected statement(s) found for {label} — awaiting approval "
            f"({len(found)} matching local attachment(s) examined)",
        )
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_scan_local(db_path: Path, pdfs: Iterable[Path], creds: dict) -> int:
    """Same review flow for PDFs already on disk."""
    pdfs = list(pdfs)
    rec = Recorder(
        db_path, "scan", f"{len(pdfs)} local file(s)", member_id=creds.get("_member_id")
    )
    try:
        for pdf in pdfs:
            ingest_file(rec, Path(pdf), creds, force=False, commit=False)
        rec.finish("done", f"{len(pdfs)} statement(s) awaiting approval")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_reevaluate(db_path: Path, file_ids: list[int], creds: dict) -> int:
    """Reparse review rows without importing or duplicating pending entries.

    The previous row remains in history as ``reevaluated`` and the fresh parse
    takes its place in the review list.  If a PDF is missing or reparsing fails,
    its previous pending row is retained so the user never loses the decision.
    """
    conn = store.connect(db_path)
    try:
        where = "WHERE status = 'pending' AND COALESCE(document_type,'credit_card') != 'bank_account'"
        args: list[int] = []
        if file_ids:
            where += f" AND id IN ({','.join('?' * len(file_ids))})"
            args = file_ids
        rows = conn.execute(
            f"SELECT id, path, member_id FROM ingest_files {where} ORDER BY id", args
        ).fetchall()
        sources = [(int(r["id"]), Path(r["path"]), r["member_id"]) for r in rows if r["path"]]
    finally:
        conn.close()

    rec = Recorder(db_path, "reevaluate", f"{len(sources)} pending statement(s)")
    refreshed = 0
    try:
        for source_id, pdf, source_member in sources:
            if not pdf.exists():
                log.warning("pending file #%s no longer exists: %s", source_id, pdf)
                continue
            # A reparse must not relabel the statement; carry the original member.
            rec.member_id = source_member
            ingest_file(rec, pdf, creds, force=False, commit=False)
            replacement_id = rec.file_id
            c2 = store.connect(db_path)
            try:
                replacement = c2.execute(
                    "SELECT status FROM ingest_files WHERE id = ?", (replacement_id,)
                ).fetchone()
                if replacement and replacement["status"] == "pending":
                    c2.execute(
                        "UPDATE ingest_files SET status = 'reevaluated' WHERE id = ?",
                        (source_id,),
                    )
                    c2.commit()
                    refreshed += 1
            finally:
                c2.close()
        rec.finish("done", f"{refreshed}/{len(sources)} pending statement(s) refreshed")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_approve(db_path: Path, file_ids: list[int], creds: dict, force: bool = False) -> int:
    """Import only the statements the user ticked."""
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, path, filename, member_id FROM ingest_files WHERE "
            f"COALESCE(document_type,'credit_card') != 'bank_account' AND id IN "
            f"({','.join('?' * len(file_ids))})",
            file_ids,
        ).fetchall()
        paths = [(r["id"], Path(r["path"]), r["member_id"]) for r in rows if r["path"]]
    finally:
        conn.close()

    rec = Recorder(db_path, "import", f"{len(paths)} approved")
    ok = 0
    try:
        for src_id, pdf, src_member in paths:
            # The member recorded when the file was scanned wins: approval can
            # happen days later, with a different member selected in the UI.
            member_id = src_member if src_member is not None else creds.get("_member_id")
            rec.member_id = member_id
            file_creds = {**creds, "_member_id": member_id}
            if not pdf.exists():
                rec.begin_file(pdf.name)
                rec.step("download", "failed", "file no longer on disk — re-scan")
                rec.end_file("failed", error="missing file")
                continue
            ok += bool(ingest_file(rec, pdf, file_creds, force=force))
            # The reviewed row is now resolved, so it drops out of the pending list.
            c2 = store.connect(db_path)
            try:
                c2.execute("UPDATE ingest_files SET status = 'approved' WHERE id = ?", (src_id,))
                c2.commit()
            finally:
                c2.close()
        rec.finish("done", f"{ok}/{len(paths)} imported")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_fetch(db_path: Path, dest: Path, creds: dict, months: int = 1, force: bool = False) -> int:
    """Download from every configured mailbox, then ingest what arrived."""
    from . import mailbox

    rec = Recorder(db_path, "fetch", f"last {months} month(s)", member_id=creds.get("_member_id"))
    try:
        from . import accounts as acct_store

        conn = store.connect(db_path)
        try:
            accounts = mailbox.accounts_from_store(conn)
        finally:
            conn.close()

        if not accounts:
            rec.begin_file("(mailbox)")
            rec.step("download", "failed",
                     "No mailboxes connected. Add one on the Connections tab.")
            rec.end_file("failed", error="no mailbox credentials")
            rec.finish("failed", "no mailbox credentials")
            return rec.run_id

        # Fetch per account so one bad mailbox cannot hide the others' results.
        new: list[Path] = []
        member_of: dict[Path, Optional[int]] = {}
        since = dt.date.today() - dt.timedelta(days=31 * months)
        for acct in accounts:
            conn = store.connect(db_path)
            try:
                got = mailbox.fetch_account(acct, Path(dest), since=since, verbose=False)
                new += got
                for path in got:
                    member_of.setdefault(path, acct.member_id)
                acct_store.mark(conn, acct.address, "connected",
                                f"{len(got)} new attachment(s)", synced=True)
            except Exception as exc:
                acct_store.mark(conn, acct.address, "failed", str(exc)[:200])
                rec.begin_file(f"({acct.address})")
                rec.step("download", "failed", str(exc)[:300])
                rec.end_file("failed", error=str(exc)[:300])
            finally:
                conn.close()
        if not new:
            rec.finish("done", "no new statements found")
            return rec.run_id

        ok = 0
        for pdf in new:
            owner = member_of.get(pdf)
            member_id = owner if owner is not None else creds.get("_member_id")
            rec.member_id = member_id
            ok += bool(ingest_file(
                rec, pdf, {**creds, "_member_id": member_id}, force=force, downloaded=True,
            ))
        rec.finish("done", f"{ok}/{len(new)} imported")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


# ----------------------------------------------------------------- queries

def pending(conn) -> list[dict]:
    """Scanned statements still awaiting a decision, newest run first."""
    rows = conn.execute(
        """SELECT f.*, r.kind, r.note, m.name AS member_name FROM ingest_files f
           JOIN ingest_runs r ON r.id = f.run_id
           LEFT JOIN members m ON m.id = f.member_id
           WHERE f.status = 'pending'
             AND COALESCE(f.document_type,'credit_card') != 'bank_account'
           ORDER BY f.id DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["checks"] = json.loads(d.pop("checks_json") or "[]")
        d["encrypted"] = bool(d["encrypted"])
        d["is_duplicate"] = d.get("duplicate_of") is not None
        out.append(d)
    return out


def discard(conn, file_ids: list[int]) -> int:
    """Drop scanned rows the user does not want. Downloads stay on disk."""
    if not file_ids:
        return 0
    cur = conn.execute(
        f"UPDATE ingest_files SET status = 'discarded' WHERE status = 'pending'"
        f" AND COALESCE(document_type,'credit_card') != 'bank_account'"
        f" AND id IN ({','.join('?' * len(file_ids))})",
        file_ids,
    )
    conn.commit()
    return cur.rowcount


def runs(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id = r.id) AS files,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id = r.id AND f.status='ok') AS ok,
                  (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id = r.id AND f.status='pending') AS pending
           FROM ingest_runs r WHERE r.kind NOT LIKE 'bank_%'
           ORDER BY r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def run_detail(conn, run_id: int) -> dict:
    run = conn.execute("SELECT * FROM ingest_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return {}
    files = []
    for f in conn.execute(
        "SELECT * FROM ingest_files WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall():
        d = dict(f)
        d["checks"] = json.loads(d.pop("checks_json") or "[]")
        d["encrypted"] = bool(d["encrypted"])
        d["steps"] = [
            dict(s)
            for s in conn.execute(
                "SELECT seq, name, status, detail, ms FROM ingest_steps WHERE file_id = ? ORDER BY seq",
                (f["id"],),
            ).fetchall()
        ]
        files.append(d)
    return {"run": dict(run), "files": files}
