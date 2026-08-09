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

    def __init__(self, db_path: Path, kind: str, note: str = ""):
        self.db_path = Path(db_path)
        self.conn = store.connect(self.db_path)
        cur = self.conn.execute(
            "INSERT INTO ingest_runs (kind, status, started_at, note) VALUES (?,?,?,?)",
            (kind, "running", _now(), note),
        )
        self.run_id = int(cur.lastrowid)
        self.conn.commit()
        self.file_id: Optional[int] = None
        self._seq = 0
        self._t0 = 0.0
        log.info("run #%s started: %s (%s)", self.run_id, kind, note or "no note")

    # ---------------------------------------------------------- file scope
    def begin_file(self, filename: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_files (run_id, filename, status, started_at) VALUES (?,?,?,?)",
            (self.run_id, filename, "running", _now()),
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
        conn = store.connect(rec.db_path)
        try:
            _, rows, replaced = store.import_statement(conn, stmt)
        finally:
            conn.close()
        rec.step("store", "ok",
                 f"{'replaced existing' if replaced else 'inserted'} statement, {rows} rows",
                 rec.elapsed_ms)

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
    rec = Recorder(db_path, "import", f"{len(pdfs)} file(s)")
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


def run_scan(
    db_path: Path,
    dest: Path,
    creds: dict,
    months: int = 1,
    month: Optional[str] = None,
) -> int:
    """Download and parse, but store nothing — builds the review list."""
    from . import mailbox

    label = f"month {month}" if month else f"last {months} month(s)"
    rec = Recorder(db_path, "scan", label)
    try:
        conn = store.connect(db_path)
        try:
            accts = mailbox.accounts_from_store(conn)
        finally:
            conn.close()
        if not accts:
            rec.begin_file("(mailbox)")
            rec.step("download", "failed", "No mailboxes connected. Add one on Connections.")
            rec.end_file("failed", error="no mailbox credentials")
            rec.finish("failed", "no mailbox credentials")
            return rec.run_id

        if month:
            since, before = month_window(month)
        else:
            since, before = dt.date.today() - dt.timedelta(days=31 * months), None

        found: list[Path] = []
        from . import accounts as acct_store

        for acct in accts:
            conn = store.connect(db_path)
            try:
                got = mailbox.fetch_account(acct, Path(dest), since=since, before=before,
                                            verbose=False)
                found += got
                acct_store.mark(conn, acct.address, "connected",
                                f"{len(got)} attachment(s) for {label}", synced=True)
            except Exception as exc:
                acct_store.mark(conn, acct.address, "failed", str(exc)[:200])
                rec.begin_file(f"({acct.address})")
                rec.step("download", "failed", str(exc)[:300])
                rec.end_file("failed", error=str(exc)[:300])
            finally:
                conn.close()

        for pdf in found:
            ingest_file(rec, pdf, creds, force=False, downloaded=True, commit=False)
        rec.finish("done", f"{len(found)} statement(s) found for {label} — awaiting approval")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_scan_local(db_path: Path, pdfs: Iterable[Path], creds: dict) -> int:
    """Same review flow for PDFs already on disk."""
    pdfs = list(pdfs)
    rec = Recorder(db_path, "scan", f"{len(pdfs)} local file(s)")
    try:
        for pdf in pdfs:
            ingest_file(rec, Path(pdf), creds, force=False, commit=False)
        rec.finish("done", f"{len(pdfs)} statement(s) awaiting approval")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


def run_approve(db_path: Path, file_ids: list[int], creds: dict, force: bool = False) -> int:
    """Import only the statements the user ticked."""
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, path, filename FROM ingest_files WHERE id IN "
            f"({','.join('?' * len(file_ids))})",
            file_ids,
        ).fetchall()
        paths = [(r["id"], Path(r["path"])) for r in rows if r["path"]]
    finally:
        conn.close()

    rec = Recorder(db_path, "import", f"{len(paths)} approved")
    ok = 0
    try:
        for src_id, pdf in paths:
            if not pdf.exists():
                rec.begin_file(pdf.name)
                rec.step("download", "failed", "file no longer on disk — re-scan")
                rec.end_file("failed", error="missing file")
                continue
            ok += bool(ingest_file(rec, pdf, creds, force=force))
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

    rec = Recorder(db_path, "fetch", f"last {months} month(s)")
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
        since = dt.date.today() - dt.timedelta(days=31 * months)
        for acct in accounts:
            conn = store.connect(db_path)
            try:
                got = mailbox.fetch_account(acct, Path(dest), since=since, verbose=False)
                new += got
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
            ok += bool(ingest_file(rec, pdf, creds, force=force, downloaded=True))
        rec.finish("done", f"{ok}/{len(new)} imported")
    except Exception as exc:
        rec.finish("failed", str(exc))
    return rec.run_id


# ----------------------------------------------------------------- queries

def pending(conn) -> list[dict]:
    """Scanned statements still awaiting a decision, newest run first."""
    rows = conn.execute(
        """SELECT f.*, r.kind, r.note FROM ingest_files f
           JOIN ingest_runs r ON r.id = f.run_id
           WHERE f.status = 'pending' ORDER BY f.id DESC"""
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
           FROM ingest_runs r ORDER BY r.id DESC LIMIT ?""",
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
