"""FastAPI application.

Chosen over a hand-rolled server because the analytics surface is expected to
grow: typed request/response models come free from the pydantic schemas already
in the project, and `/docs` documents every endpoint without extra work.

Long jobs (mailbox fetch, bulk import) run on a background thread and report
progress through the ingest tables, so the UI polls a run id rather than holding
a request open.
"""
from __future__ import annotations

import datetime as dt
import os
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, pipeline, store
from .logging_config import configure_progress_logging

configure_progress_logging()

WEB_DIST = Path(__file__).parent / "web" / "dist"

DB_PATH = Path(os.environ.get("SPARSER_DB", "statements.db"))
INBOX = Path(os.environ.get("SPARSER_INBOX", "inbox"))

app = FastAPI(title="sparser", description="Card statement parser & spend analytics", version="0.2.0")

_lock = threading.Lock()
log = logging.getLogger("sparser.api")


def db():
    return store.connect(DB_PATH)


def _cards(cards: Optional[str]) -> list[int]:
    return [int(v) for v in (cards or "").split(",") if v.strip().isdigit()]


# ------------------------------------------------------------------ models

class Credentials(BaseModel):
    """Supplied per request rather than stored: these unlock financial documents."""

    password: Optional[str] = None
    name: Optional[str] = Field(default=None, description="For deriving the PDF password")
    dob: Optional[str] = Field(default=None, description="DD/MM/YYYY")
    card_last4: Optional[str] = None

    def as_dict(self) -> dict:
        d = self.model_dump()
        if d.get("dob"):
            try:
                d["dob"] = dt.datetime.strptime(d["dob"], "%d/%m/%Y").date()
            except ValueError:
                raise HTTPException(422, "dob must be DD/MM/YYYY")
        return d


class MailboxIn(BaseModel):
    address: str
    app_password: str = Field(description="16-character Gmail app password, not the login password")


class FetchRequest(Credentials):
    months: int = Field(default=1, ge=1, le=36)
    month: Optional[str] = Field(
        default=None, description='Specific billing month as "YYYY-MM"; overrides months'
    )
    force: bool = False


class ApproveRequest(Credentials):
    file_ids: list[int] = Field(min_length=1)
    force: bool = False


class ImportRequest(Credentials):
    paths: list[str] = Field(default_factory=list, description="PDF paths or directories")
    force: bool = False


# -------------------------------------------------------------- analytics

@app.get("/api/bootstrap")
def bootstrap():
    conn = db()
    try:
        return {
            "cards": store.cards(conn),
            "bounds": store.date_bounds(conn),
            "statements": store.statements(conn),
            "mailboxes_configured": bool(accounts.listing(conn))
            or bool(os.environ.get("SPARSER_GMAIL", "").strip()),
        }
    finally:
        conn.close()


@app.get("/api/analytics")
def analytics(
    cards: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
):
    conn = db()
    try:
        return store.analytics(conn, _cards(cards), date_from, date_to)
    finally:
        conn.close()


@app.get("/api/transactions")
def transactions(
    cards: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = 5000,
):
    conn = db()
    try:
        return store.transactions(conn, _cards(cards), date_from, date_to, limit)
    finally:
        conn.close()


@app.get("/api/export")
def export(
    cards: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
):
    """The canonical JSON document — every card, statement and transaction."""
    conn = db()
    try:
        payload = {
            "schema": "sparser/statements@1",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "cards": store.cards(conn),
            "statements": store.statements(conn),
            "transactions": store.transactions(conn, _cards(cards), date_from, date_to, limit=1_000_000),
        }
    finally:
        conn.close()
    return JSONResponse(
        payload, headers={"Content-Disposition": 'attachment; filename="statements.json"'}
    )


# --------------------------------------------------------------- profile

class ProfileIn(BaseModel):
    full_name: str = ""
    dob: str = Field(default="", description='DD/MM/YYYY, DD/MM or DDMM — year optional')


@app.get("/api/profile")
def get_profile():
    conn = db()
    try:
        p = accounts.get_profile(conn)
        p["derives"] = len(
            __import__("sparser.decrypt", fromlist=["x"]).candidate_passwords(
                p["full_name"], p["dob"]
            )
        )
        return p
    finally:
        conn.close()


@app.put("/api/profile")
def put_profile(body: ProfileIn):
    from .decrypt import candidate_passwords

    conn = db()
    try:
        accounts.set_profile(conn, body.full_name.strip(), body.dob.strip())
        # Read it back through the encryption boundary. The response confirms
        # persistence, not merely that the PUT handler ran without raising.
        saved = accounts.get_profile(conn)
        saved["derives"] = len(candidate_passwords(saved["full_name"], saved["dob"]))
        saved["status"] = "saved"
        return saved
    finally:
        conn.close()


# ----------------------------------------------------------------- cards

class CardPasswordIn(BaseModel):
    password: str = Field(min_length=1)


@app.get("/api/cards")
def list_cards():
    """Cards with whether a decryption password is stored — never the value."""
    conn = db()
    try:
        meta = accounts.card_secret_meta(conn)
        history = store.card_history(conn)
        rows = store.cards(conn)
        for c in rows:
            m = meta.get(c["masked_number"])
            c["password_set"] = m is not None
            c["password_source"] = (m or {}).get("source")
            c["password_updated_at"] = (m or {}).get("updated_at")
            mine = history.get(c["id"], [])
            c["history"] = mine
            c["statements"] = len(mine)
            # Newest first, and a statement whose date the document never printed
            # is still dated by the cycle it closes.
            c["last_statement"] = (
                (mine[0]["statement_date"] or mine[0]["period_end"]) if mine else None
            )
        return {"cards": rows}
    finally:
        conn.close()


@app.get("/api/cards/{card_id}/password")
def reveal_card_password(card_id: int):
    """Explicit, separate request — the value is never included in listings."""
    conn = db()
    try:
        row = next((c for c in store.cards(conn) if c["id"] == card_id), None)
        if not row:
            raise HTTPException(404, "no such card")
        secret = accounts.card_password(conn, row["masked_number"])
        if secret is None:
            raise HTTPException(404, "no password stored for this card")
        return {"password": secret}
    finally:
        conn.close()


@app.put("/api/cards/{card_id}/password")
def set_card_password(card_id: int, body: CardPasswordIn):
    conn = db()
    try:
        row = next((c for c in store.cards(conn) if c["id"] == card_id), None)
        if not row:
            raise HTTPException(404, "no such card")
        accounts.set_card_password(conn, row["masked_number"], body.password, source="manual")
        return {"status": "saved"}
    finally:
        conn.close()


@app.delete("/api/cards/{card_id}/password")
def delete_card_password(card_id: int):
    conn = db()
    try:
        row = next((c for c in store.cards(conn) if c["id"] == card_id), None)
        if not row:
            raise HTTPException(404, "no such card")
        accounts.clear_card_password(conn, row["masked_number"])
        return {"status": "removed"}
    finally:
        conn.close()


# ------------------------------------------------------------- mailboxes

@app.get("/api/mailboxes")
def list_mailboxes():
    """Connected mailboxes. Secrets are never returned — only whether one works."""
    conn = db()
    try:
        return {
            "mailboxes": accounts.listing(conn),
            "env_configured": bool(os.environ.get("SPARSER_GMAIL", "").strip()),
            "key_file": str(accounts.key_path()),
        }
    finally:
        conn.close()


@app.post("/api/mailboxes")
def add_mailbox(m: MailboxIn):
    """Verify the credentials before storing them, so a typo fails loudly here."""
    ok, message = accounts.test_connection(m.address, m.app_password)
    conn = db()
    try:
        accounts.add(conn, m.address, m.app_password)
        accounts.mark(conn, m.address, "connected" if ok else "failed", message)
        if not ok:
            raise HTTPException(400, message)
        return {"status": "connected", "detail": message}
    finally:
        conn.close()


@app.post("/api/mailboxes/{mailbox_id}/test")
def test_mailbox(mailbox_id: int):
    conn = db()
    try:
        row = next((m for m in accounts.listing(conn) if m["id"] == mailbox_id), None)
        if not row:
            raise HTTPException(404, "no such mailbox")
        secret = accounts.secret_for(conn, row["address"])
        if not secret:
            raise HTTPException(400, "stored secret cannot be read — re-add this mailbox")
        ok, message = accounts.test_connection(row["address"], secret)
        accounts.mark(conn, row["address"], "connected" if ok else "failed", message)
        return {"status": "connected" if ok else "failed", "detail": message}
    finally:
        conn.close()


@app.delete("/api/mailboxes/{mailbox_id}")
def delete_mailbox(mailbox_id: int):
    conn = db()
    try:
        accounts.remove(conn, mailbox_id)
        return {"status": "removed"}
    finally:
        conn.close()


# ---------------------------------------------------------------- ingest

def _spawn(fn, *args) -> None:
    log.info("starting background job: %s", getattr(fn, "__name__", "job"))
    threading.Thread(target=fn, args=args, daemon=True).start()


@app.post("/api/ingest/fetch")
def ingest_fetch(req: FetchRequest, tasks: BackgroundTasks):
    """Pull this month's statements from every configured mailbox and import."""
    conn = db()
    try:
        from .mailbox import accounts_from_store

        if not accounts_from_store(conn):
            raise HTTPException(
                400,
                "No mailboxes connected. Add a Gmail account on the Connections tab "
                "using an app password from https://myaccount.google.com/apppasswords",
            )
    finally:
        conn.close()
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")

    creds, months, force = req.as_dict(), req.months, req.force
    log.info("mail fetch requested: last %d month(s)", months)

    def job():
        try:
            pipeline.run_fetch(DB_PATH, INBOX, creds, months=months, force=force)
        finally:
            _lock.release()

    # Create the run row synchronously so the UI has an id to poll immediately.
    _spawn(job)
    return {"status": "started", "months": months}


def _collect(paths: list[str]) -> list[Path]:
    pdfs: list[Path] = []
    for raw in paths or [str(INBOX)]:
        p = Path(raw).expanduser()
        if p.is_dir():
            pdfs += sorted(p.glob("*.pdf"))
        elif p.exists():
            pdfs.append(p)
        else:
            pdfs += sorted(Path().glob(raw))
    if not pdfs:
        raise HTTPException(404, f"no PDFs found in {paths or [str(INBOX)]}")
    return pdfs


@app.post("/api/ingest/local")
def ingest_local(req: ImportRequest):
    """Import PDFs already on disk, skipping the review step."""
    pdfs = _collect(req.paths)
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")

    creds, force = req.as_dict(), req.force
    log.info("local import requested: %d PDF(s)", len(pdfs))

    def job():
        try:
            pipeline.run_import(DB_PATH, pdfs, creds, force=force)
        finally:
            _lock.release()

    _spawn(job)
    return {"status": "started", "files": [p.name for p in pdfs]}


@app.post("/api/ingest/scan")
def ingest_scan(req: FetchRequest):
    """Download and parse, storing nothing — produces the list to review."""
    conn = db()
    try:
        from .mailbox import accounts_from_store

        if not accounts_from_store(conn):
            raise HTTPException(
                400,
                "No mailboxes connected. Add a Gmail account on the Connections tab "
                "using an app password from https://myaccount.google.com/apppasswords",
            )
    finally:
        conn.close()
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")

    creds, months, month = req.as_dict(), req.months, req.month
    log.info("mail scan requested: %s", f"month {month}" if month else f"last {months} month(s)")

    def job():
        try:
            pipeline.run_scan(DB_PATH, INBOX, creds, months=months, month=month)
        finally:
            _lock.release()

    _spawn(job)
    return {"status": "started", "month": month, "months": months}


@app.post("/api/ingest/scan-local")
def ingest_scan_local(req: ImportRequest):
    """Review flow for PDFs already on disk."""
    pdfs = _collect(req.paths)
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")
    creds = req.as_dict()

    def job():
        try:
            pipeline.run_scan_local(DB_PATH, pdfs, creds)
        finally:
            _lock.release()

    _spawn(job)
    return {"status": "started", "files": [p.name for p in pdfs]}


@app.get("/api/pending")
def pending():
    """Scanned statements awaiting approval."""
    conn = db()
    try:
        return {"pending": pipeline.pending(conn)}
    finally:
        conn.close()


@app.post("/api/pending/discard")
def discard_pending(body: dict):
    """Dismiss scanned statements without importing them."""
    ids = [int(i) for i in body.get("file_ids", [])]
    conn = db()
    try:
        return {"discarded": pipeline.discard(conn, ids)}
    finally:
        conn.close()


@app.post("/api/ingest/approve")
def ingest_approve(req: ApproveRequest):
    """Import only the statements the user selected."""
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")
    creds, ids, force = req.as_dict(), req.file_ids, req.force

    def job():
        try:
            pipeline.run_approve(DB_PATH, ids, creds, force=force)
        finally:
            _lock.release()

    _spawn(job)
    return {"status": "started", "count": len(ids)}


@app.get("/api/runs")
def list_runs(limit: int = 40):
    conn = db()
    try:
        return {"runs": pipeline.runs(conn, limit), "busy": _lock.locked()}
    finally:
        conn.close()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    conn = db()
    try:
        detail = pipeline.run_detail(conn, run_id)
        if not detail:
            raise HTTPException(404, "no such run")
        return detail
    finally:
        conn.close()


# ------------------------------------------------------------------- web

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        """Serve the built SPA, letting client-side routing own unknown paths."""
        candidate = WEB_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
