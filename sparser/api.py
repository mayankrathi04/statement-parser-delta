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
import re
import shutil
import tempfile
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import accounts, bank_pipeline, bank_store, categories, pipeline, portal, store
from .env import load_local_env
from .logging_config import configure_progress_logging

load_local_env()
configure_progress_logging()

WEB_DIST = Path(__file__).parent / "web" / "dist"

DB_PATH = Path(os.environ.get("SPARSER_DB", "data/statements.db"))
INBOX = Path(os.environ.get("SPARSER_INBOX", "inbox"))

app = FastAPI(title="sparser", description="Card and bank statement analytics", version="0.3.0")

_lock = threading.Lock()
_bank_lock = threading.Lock()
log = logging.getLogger("sparser.api")
_current_user: ContextVar[Optional[dict]] = ContextVar("sparser_user", default=None)


def db():
    conn = store.connect(DB_PATH)
    username = os.environ.get("DEFAULT_USERNAME", "").strip()
    password = os.environ.get("DEFAULT_PASSWORD", "")
    if username and password and not portal.has_users(conn):
        portal.register(conn, username, password, username)
    return conn


def _user() -> dict:
    current = _current_user.get()
    if not current:
        raise HTTPException(401, "sign in to continue")
    return current


def _member_ids(value: Optional[str]) -> list[int]:
    return [int(v) for v in (value or "").split(",") if v.strip().isdigit()]


def _scope(conn, requested=()) -> list[int]:
    try:
        return portal.allowed_member_ids(conn, int(_user()["id"]), requested)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


def _default_member(conn, requested: Optional[int] = None) -> int:
    if requested is not None:
        try:
            return portal.require_member(conn, int(_user()["id"]), requested)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
    rows = portal.members(conn, int(_user()["id"]))
    return int(next((row for row in rows if row["is_default"]), rows[0])["id"])


def _selected_ids(allowed: set[int], requested: list[int]) -> list[int]:
    """Return a non-empty SQL scope; ``-1`` deliberately matches no row."""
    selected = sorted(allowed & set(requested)) if requested else sorted(allowed)
    return selected or [-1]


#: The only endpoints reachable without a session. Note that `/api/auth/me` is
#: NOT among them: it reports who the caller is, so it needs the user context this
#: middleware sets. Excluding the whole `/api/auth/` prefix made it answer 401 for
#: everyone, which signed users out on every page reload.
_PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/register",
})


@app.middleware("http")
async def portal_auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in _PUBLIC_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or request.query_params.get("access_token", "")
    conn = db()
    try:
        current = portal.user_for_token(conn, token) if token else None
    finally:
        conn.close()
    if not current:
        return JSONResponse({"detail": "sign in to continue"}, status_code=401)
    marker = _current_user.set(current)
    try:
        return await call_next(request)
    finally:
        _current_user.reset(marker)


def _cards(cards: Optional[str]) -> list[int]:
    return [int(v) for v in (cards or "").split(",") if v.strip().isdigit()]


def _account_ids(accounts: Optional[str]) -> list[int]:
    return [int(v) for v in (accounts or "").split(",") if v.strip().isdigit()]


def _categories(values: Optional[list[str]]) -> Optional[list[str]]:
    """The requested category filter, or ``None`` when the caller sent none.

    Repeated ``?categories=`` values rather than one comma-joined string,
    because category names may contain commas. An empty list is meaningful and
    distinct from ``None``: the user unticked every category, so nothing should
    match. A query string cannot carry an empty repeated parameter, so the
    client sends a single blank value to say so, which strips down to ``[]``.
    """
    if values is None:
        return None
    return [name.strip() for name in values if name.strip()]


# ------------------------------------------------------------------ models

class Credentials(BaseModel):
    """Supplied per request rather than stored: these unlock financial documents."""

    password: Optional[str] = None
    name: Optional[str] = Field(default=None, description="For deriving the PDF password")
    dob: Optional[str] = Field(default=None, description="DD/MM/YYYY")
    card_last4: Optional[str] = None
    member_id: Optional[int] = None

    def as_dict(self) -> dict:
        d = self.model_dump()
        member_id = d.pop("member_id", None)
        if member_id is not None:
            d["_member_id"] = member_id
        if d.get("dob"):
            try:
                d["dob"] = dt.datetime.strptime(d["dob"], "%d/%m/%Y").date()
            except ValueError:
                raise HTTPException(422, "dob must be DD/MM/YYYY")
        return d


class MailboxIn(BaseModel):
    address: str
    app_password: str = Field(description="16-character Gmail app password, not the login password")
    member_id: Optional[int] = None


class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: str


class LoginIn(BaseModel):
    username: str
    password: str


class MemberIn(BaseModel):
    name: str


class MemberUpdateIn(BaseModel):
    name: Optional[str] = None
    is_default: bool = False


class MemberAssignmentIn(BaseModel):
    member_id: int


def _owned_credentials(req: Credentials) -> dict:
    conn = db()
    try:
        values = req.as_dict()
        values["_member_id"] = _default_member(conn, req.member_id)
        return values
    finally:
        conn.close()


class FetchRequest(Credentials):
    months: int = Field(default=1, ge=1, le=36)
    month: Optional[str] = Field(
        default=None, description='Specific billing month as "YYYY-MM"; overrides months'
    )
    month_from: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    month_to: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    card_ids: list[int] = Field(default_factory=list)
    #: Keep statements whose card number matches none of the saved cards. Without
    #: it a card that has never been imported can never be scanned, because the
    #: card list a scan filters on is only written by an import.
    include_unrecognized_cards: bool = False
    connection_ids: list[int] = Field(default_factory=list)
    force: bool = False


class ApproveRequest(Credentials):
    file_ids: list[int] = Field(min_length=1)
    force: bool = False


class ReevaluateRequest(Credentials):
    file_ids: list[int] = Field(default_factory=list)


class ImportRequest(Credentials):
    paths: list[str] = Field(default_factory=list, description="PDF paths or directories")
    force: bool = False


class BankCategoryIn(BaseModel):
    category: Optional[str] = Field(
        default=None,
        max_length=bank_store.MAX_CATEGORY_LENGTH,
        description="Manual category label; null or blank restores automatic categorization",
    )


class BankBulkCategoryIn(BankCategoryIn):
    #: Capped so one request cannot rewrite the whole ledger by accident; the UI
    #: sends the current search result, which is well inside this.
    ids: list[int] = Field(min_length=1, max_length=5000)


class CategoryIn(BaseModel):
    name: str
    pattern: Optional[str] = None
    applies_to: str = "both"
    #: The major this sub is filed under. Optional: a sub can be created now and
    #: placed later, which is what the unmapped tray is for.
    major_id: Optional[int] = None


class CategoryUpdateIn(BaseModel):
    name: Optional[str] = None
    pattern: Optional[str] = None
    applies_to: Optional[str] = None
    #: Explicit, because a null `pattern` means "leave it alone" on a partial update.
    clear_pattern: bool = False
    major_id: Optional[int] = None
    #: Same reason as `clear_pattern`: this is how a sub is un-filed.
    clear_major: bool = False


class MajorCategoryIn(BaseModel):
    name: str


class CategoryLinkIn(BaseModel):
    #: Null files the sub back into the unmapped tray.
    major_id: Optional[int] = None


class CategoryOrderIn(BaseModel):
    ids: list[int] = Field(min_length=1)


# -------------------------------------------------------------- portal

@app.get("/api/auth/status")
def auth_status():
    conn = db()
    try:
        return {"registration_required": not portal.has_users(conn)}
    finally:
        conn.close()


@app.post("/api/auth/register")
def register(body: RegisterIn):
    conn = db()
    try:
        try:
            created = portal.register(conn, body.username, body.password, body.display_name)
            token, signed_in = portal.login(conn, body.username, body.password)
            return {"token": token, "user": signed_in or created}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/auth/login")
def login(body: LoginIn):
    conn = db()
    try:
        try:
            token, current = portal.login(conn, body.username, body.password)
            return {"token": token, "user": current}
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    """Invalidate the presented session so the token cannot be reused."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    conn = db()
    try:
        return {"status": "signed out" if token and portal.logout(conn, token) else "no session"}
    finally:
        conn.close()


@app.get("/api/auth/me")
def auth_me():
    conn = db()
    try:
        return portal.user(conn, int(_user()["id"]))
    finally:
        conn.close()


@app.get("/api/members")
def list_members():
    """Members with what is filed under each, so the Members tab can show the cost
    of removing one before it is attempted."""
    conn = db()
    try:
        rows = portal.members(conn, int(_user()["id"]))
        for row in rows:
            usage = portal.member_usage(conn, int(row["id"]))
            row["cards"] = usage["card"]
            row["bank_accounts"] = usage["bank account"]
            row["mailboxes"] = usage["mailbox"]
        return {"members": rows}
    finally:
        conn.close()


@app.post("/api/members")
def create_member(body: MemberIn):
    conn = db()
    try:
        try:
            return portal.add_member(conn, int(_user()["id"]), body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/members/{member_id}")
def update_member(member_id: int, body: MemberUpdateIn):
    conn = db()
    try:
        user_id = int(_user()["id"])
        try:
            member = portal.members(conn, user_id)
            if not any(int(row["id"]) == member_id for row in member):
                raise PermissionError("member does not belong to this user")
            updated = None
            if body.name is not None:
                updated = portal.rename_member(conn, user_id, member_id, body.name)
            if body.is_default:
                updated = portal.set_default_member(conn, user_id, member_id)
            return updated or next(row for row in member if int(row["id"]) == member_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.delete("/api/members/{member_id}")
def remove_member(member_id: int):
    conn = db()
    try:
        try:
            portal.delete_member(conn, int(_user()["id"]), member_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"status": "removed"}
    finally:
        conn.close()


# -------------------------------------------------------------- analytics

@app.get("/api/bootstrap")
def bootstrap(members: Optional[str] = None):
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        visible_cards = store.cards(conn, scope)
        card_ids = [row["id"] for row in visible_cards]
        sql_ids = card_ids or [-1]
        bounds = conn.execute(
            f"SELECT MIN(txn_date) min, MAX(txn_date) max FROM transactions WHERE card_id IN ({','.join('?' * len(sql_ids))})",
            sql_ids,
        ).fetchone()
        return {
            "cards": visible_cards,
            "bounds": dict(bounds),
            # `sql_ids`, never the bare list: with no visible cards an empty
            # scope would widen to every card rather than to none.
            "categories": store.category_facets(conn, sql_ids),
            "statements": [row for row in store.statements(conn) if row["card_id"] in card_ids],
            "mailboxes_configured": bool(accounts.listing(conn, scope))
            or bool(os.environ.get("SPARSER_GMAIL", "").strip()),
        }
    finally:
        conn.close()


@app.get("/api/analytics")
def analytics(
    cards: Optional[str] = None,
    members: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    categories: Optional[list[str]] = Query(None),
):
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        allowed = {row["id"] for row in store.cards(conn, scope)}
        requested = _cards(cards)
        ids = _selected_ids(allowed, requested)
        return store.analytics(conn, ids, date_from, date_to, _categories(categories))
    finally:
        conn.close()


@app.get("/api/transactions")
def transactions(
    cards: Optional[str] = None,
    members: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = 5000,
    categories: Optional[list[str]] = Query(None),
):
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        allowed = {row["id"] for row in store.cards(conn, scope)}
        requested = _cards(cards)
        ids = _selected_ids(allowed, requested)
        return store.transactions(conn, ids, date_from, date_to, limit, _categories(categories))
    finally:
        conn.close()


@app.get("/api/export")
def export(
    cards: Optional[str] = None,
    members: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    categories: Optional[list[str]] = Query(None),
):
    """The canonical JSON document — every card, statement and transaction."""
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        visible_cards = store.cards(conn, scope)
        allowed = {row["id"] for row in visible_cards}
        requested = _cards(cards)
        ids = _selected_ids(allowed, requested)
        payload = {
            "schema": "sparser/statements@1",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "cards": visible_cards,
            "statements": [row for row in store.statements(conn) if row["card_id"] in ids],
            "transactions": store.transactions(
                conn, ids, date_from, date_to, limit=1_000_000,
                categories=_categories(categories),
            ),
        }
    finally:
        conn.close()
    return JSONResponse(
        payload, headers={"Content-Disposition": 'attachment; filename="statements.json"'}
    )


# --------------------------------------------------------- bank analytics

@app.get("/api/bank/bootstrap")
def bank_bootstrap(members: Optional[str] = None):
    conn = db()
    try:
        visible = bank_store.accounts(conn, _scope(conn, _member_ids(members)))
        ids = [row["id"] for row in visible] or [-1]
        bounds = conn.execute(
            f"SELECT MIN(txn_date) min, MAX(txn_date) max FROM bank_transactions WHERE account_id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchone()
        return {
            "accounts": visible,
            "bounds": dict(bounds),
            # `ids` is already `… or [-1]`: with no visible accounts the scope
            # must narrow to none, not widen to every account.
            "categories": bank_store.category_facets(conn, ids),
        }
    finally:
        conn.close()


@app.get("/api/bank/analytics")
def bank_analytics(
    accounts: Optional[str] = None,
    members: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    categories: Optional[list[str]] = Query(None),
):
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        allowed = {row["id"] for row in bank_store.accounts(conn, scope)}
        requested = _account_ids(accounts)
        ids = _selected_ids(allowed, requested)
        return bank_store.analytics(conn, ids, date_from, date_to, _categories(categories))
    finally:
        conn.close()


@app.get("/api/bank/transactions")
def bank_transactions(
    accounts: Optional[str] = None,
    members: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = 5000,
    categories: Optional[list[str]] = Query(None),
):
    conn = db()
    try:
        scope = _scope(conn, _member_ids(members))
        allowed = {row["id"] for row in bank_store.accounts(conn, scope)}
        requested = _account_ids(accounts)
        ids = _selected_ids(allowed, requested)
        return bank_store.transactions(
            conn, ids, date_from, date_to, limit, _categories(categories)
        )
    finally:
        conn.close()


@app.put("/api/bank/transactions/{transaction_id}/category")
def put_bank_transaction_category(transaction_id: int, body: BankCategoryIn):
    """Override one bank transaction category, or clear it to use parser logic."""
    conn = db()
    try:
        scope = _scope(conn)
        owned_accounts = {row["id"] for row in bank_store.accounts(conn, scope)}
        txn = conn.execute(
            "SELECT account_id FROM bank_transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if not txn or txn["account_id"] not in owned_accounts:
            raise HTTPException(404, "bank transaction not found")
        try:
            return bank_store.update_transaction_category(conn, transaction_id, body.category)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/bank/transactions/category")
def put_bank_transaction_categories(body: BankBulkCategoryIn):
    """Apply one category to many rows — the search-and-select flow on Bank Analysis."""
    conn = db()
    try:
        scope = _scope(conn)
        owned_accounts = [row["id"] for row in bank_store.accounts(conn, scope)]
        if not owned_accounts:
            raise HTTPException(404, "no bank accounts found")
        try:
            updated = bank_store.update_transaction_categories(
                conn, body.ids, body.category, owned_accounts
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not updated:
            raise HTTPException(404, "none of those bank transactions were found")
        return {"updated": len(updated), "rows": updated}
    finally:
        conn.close()


# ------------------------------------------------------------------ categories

@app.get("/api/categories")
def list_categories():
    """The one category list both ledgers share, plus the issuers' own read-only labels."""
    conn = db()
    try:
        user_id = int(_user()["id"])
        categories.seed(conn, user_id)
        rows = categories.listing(conn, user_id)
        for row in rows:
            row["usage"] = categories.in_use(conn, user_id, row["name"])
        # Every list carries the counts, not just the flat one. `majors` and
        # `unmapped` are separate reads of the same rows, so decorating only the
        # first hands the screen the same category with and without `usage`
        # depending on where it is shown — and a reader of the second shape sees
        # a blank page, not a missing number.
        counts = {int(row["id"]): row["usage"] for row in rows}
        majors = categories.majors(conn, user_id)
        for major in majors:
            for child in major["children"]:
                child["usage"] = counts.get(int(child["id"]), {"cards": 0, "bank": 0})
        return {
            "categories": rows,
            "majors": majors,
            # Named separately from `categories` so a screen can lead with what
            # is still unplaced instead of hiding it in a list of forty.
            "unmapped": [row for row in rows if row["major_id"] is None],
            "provider_categories": categories.provider_categories(conn, user_id),
        }
    finally:
        conn.close()


@app.post("/api/categories")
def create_category(body: CategoryIn):
    conn = db()
    try:
        try:
            return categories.add(
                conn, int(_user()["id"]), body.name, body.pattern, body.applies_to,
                body.major_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "major category not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/categories/order")
def order_categories(body: CategoryOrderIn):
    """Declared before the ``{category_id}`` route: FastAPI matches in order, and
    "order" would otherwise be parsed as an id."""
    conn = db()
    try:
        try:
            return {"categories": categories.reorder(conn, int(_user()["id"]), body.ids)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/categories/{category_id}")
def edit_category(category_id: int, body: CategoryUpdateIn):
    conn = db()
    try:
        try:
            return categories.update(
                conn, int(_user()["id"]), category_id,
                body.name, body.pattern, body.applies_to, body.clear_pattern,
                body.major_id, body.clear_major,
            )
        except KeyError as exc:
            # KeyError stringifies with quotes; the message is in args[0].
            raise HTTPException(404, str(exc.args[0]) if exc.args else "category not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/categories/{category_id}/major")
def link_category(category_id: int, body: CategoryLinkIn):
    """File one sub-category under a major, or clear it back to unmapped.

    Separate from the general edit so the mapping screen cannot touch a rule
    while moving a category between headings.
    """
    conn = db()
    try:
        try:
            return categories.link(conn, int(_user()["id"]), category_id, body.major_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0]) if exc.args else "category not found") from exc
    finally:
        conn.close()


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int):
    conn = db()
    try:
        try:
            return categories.remove(conn, int(_user()["id"]), category_id)
        except KeyError as exc:
            raise HTTPException(404, "category not found") from exc
    finally:
        conn.close()


# ------------------------------------------------------------ major categories

@app.post("/api/major-categories")
def create_major_category(body: MajorCategoryIn):
    conn = db()
    try:
        try:
            return categories.major_add(conn, int(_user()["id"]), body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/major-categories/order")
def order_major_categories(body: CategoryOrderIn):
    """Before the ``{major_id}`` route: FastAPI matches in order, and "order"
    would otherwise be parsed as an id."""
    conn = db()
    try:
        try:
            return {"majors": categories.major_reorder(conn, int(_user()["id"]), body.ids)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.put("/api/major-categories/{major_id}")
def edit_major_category(major_id: int, body: MajorCategoryIn):
    conn = db()
    try:
        try:
            return categories.major_update(conn, int(_user()["id"]), major_id, body.name)
        except KeyError as exc:
            raise HTTPException(404, "major category not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.delete("/api/major-categories/{major_id}")
def delete_major_category(major_id: int):
    """`unmapped` counts the subs this delete just sent back to the tray."""
    conn = db()
    try:
        try:
            return categories.major_remove(conn, int(_user()["id"]), major_id)
        except KeyError as exc:
            raise HTTPException(404, "major category not found") from exc
    finally:
        conn.close()


@app.post("/api/categories/reapply")
def reapply_categories():
    """Recompute derived categories on both ledgers. Manual overrides are kept."""
    conn = db()
    try:
        return categories.reapply(conn, int(_user()["id"]))
    finally:
        conn.close()


@app.put("/api/transactions/category")
def put_card_transaction_categories(body: BankBulkCategoryIn):
    """Bulk category override for card rows — the same flow as the bank ledger."""
    conn = db()
    try:
        scope = _scope(conn)
        owned = [row["id"] for row in store.cards(conn, scope)]
        if not owned:
            raise HTTPException(404, "no cards found")
        try:
            updated = store.update_transaction_categories(conn, body.ids, body.category, owned)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not updated:
            raise HTTPException(404, "none of those transactions were found")
        return {"updated": len(updated), "rows": updated}
    finally:
        conn.close()


@app.get("/api/bank/export")
def bank_export(
    accounts: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    categories: Optional[list[str]] = Query(None),
):
    conn = db()
    try:
        scope = _scope(conn)
        visible_accounts = bank_store.accounts(conn, scope)
        allowed = {row["id"] for row in visible_accounts}
        requested = _account_ids(accounts)
        ids = _selected_ids(allowed, requested)
        payload = {
            "schema": "sparser/bank-statements@1",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "accounts": visible_accounts,
            "statements": [row for row in bank_store.statements(conn)
                           if row["account_id"] in ids],
            "transactions": bank_store.transactions(
                conn, ids, date_from, date_to, limit=1_000_000,
                categories=_categories(categories),
            ),
        }
    finally:
        conn.close()
    return JSONResponse(
        payload, headers={"Content-Disposition": 'attachment; filename="bank-statements.json"'}
    )


# --------------------------------------------------------------- profile

class ProfileIn(BaseModel):
    full_name: str = ""
    dob: str = Field(default="", description='DD/MM/YYYY, DD/MM or DDMM — year optional')


@app.get("/api/profile")
def get_profile(member_id: Optional[int] = None):
    conn = db()
    try:
        p = accounts.get_profile(conn, _default_member(conn, member_id))
        p["derives"] = len(
            __import__("sparser.decrypt", fromlist=["x"]).candidate_passwords(
                p["full_name"], p["dob"]
            )
        )
        return p
    finally:
        conn.close()


@app.put("/api/profile")
def put_profile(body: ProfileIn, member_id: Optional[int] = None):
    from .decrypt import candidate_passwords

    conn = db()
    try:
        selected = _default_member(conn, member_id)
        accounts.set_profile(conn, body.full_name.strip(), body.dob.strip(), selected)
        # Read it back through the encryption boundary. The response confirms
        # persistence, not merely that the PUT handler ran without raising.
        saved = accounts.get_profile(conn, selected)
        saved["derives"] = len(candidate_passwords(saved["full_name"], saved["dob"]))
        saved["status"] = "saved"
        return saved
    finally:
        conn.close()


# ----------------------------------------------------------------- cards

class CardPasswordIn(BaseModel):
    password: str = Field(min_length=1)


class CardMailRulesIn(BaseModel):
    sender_ids: list[str] = Field(default_factory=list)
    subject_patterns: list[str] = Field(default_factory=list)


@app.get("/api/cards")
def list_cards(members: Optional[str] = None):
    """Cards with whether a decryption password is stored — never the value."""
    conn = db()
    try:
        meta = accounts.card_secret_meta(conn)
        history = store.card_history(conn)
        rows = store.cards(conn, _scope(conn, _member_ids(members)))
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
        row = next((c for c in store.cards(conn, _scope(conn)) if c["id"] == card_id), None)
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
        row = next((c for c in store.cards(conn, _scope(conn)) if c["id"] == card_id), None)
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
        row = next((c for c in store.cards(conn, _scope(conn)) if c["id"] == card_id), None)
        if not row:
            raise HTTPException(404, "no such card")
        accounts.clear_card_password(conn, row["masked_number"])
        return {"status": "removed"}
    finally:
        conn.close()


@app.put("/api/cards/{card_id}/mail-rules")
def set_card_mail_rules(card_id: int, body: CardMailRulesIn):
    def cleaned(values: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip().lower() for v in values if v.strip()))

    senders = cleaned(body.sender_ids)
    subjects = cleaned(body.subject_patterns)
    if len(senders) > 30 or len(subjects) > 30:
        raise HTTPException(422, "a card supports at most 30 sender and 30 subject rules")
    conn = db()
    try:
        if card_id not in {row["id"] for row in store.cards(conn, _scope(conn))}:
            raise HTTPException(404, "no such card")
        if not store.set_card_mail_rules(conn, card_id, senders, subjects):
            raise HTTPException(404, "no such card")
        return {"status": "saved", "sender_ids": senders, "subject_patterns": subjects}
    finally:
        conn.close()


@app.put("/api/cards/{card_id}/member")
def assign_card_member(card_id: int, body: MemberAssignmentIn):
    conn = db()
    try:
        member_id = _default_member(conn, body.member_id)
        visible = {row["id"] for row in store.cards(conn, _scope(conn))}
        if card_id not in visible:
            raise HTTPException(404, "no such card")
        conn.execute("UPDATE cards SET member_id=? WHERE id=?", (member_id, card_id))
        conn.commit()
        return {"status": "saved", "member_id": member_id}
    finally:
        conn.close()


@app.put("/api/bank/accounts/{account_id}/member")
def assign_bank_account_member(account_id: int, body: MemberAssignmentIn):
    conn = db()
    try:
        member_id = _default_member(conn, body.member_id)
        visible = {row["id"] for row in bank_store.accounts(conn, _scope(conn))}
        if account_id not in visible:
            raise HTTPException(404, "no such bank account")
        conn.execute("UPDATE bank_accounts SET member_id=? WHERE id=?", (member_id, account_id))
        conn.commit()
        return {"status": "saved", "member_id": member_id}
    finally:
        conn.close()


# ------------------------------------------------------------- mailboxes

@app.get("/api/mailboxes")
def list_mailboxes():
    """Connected mailboxes. Secrets are never returned — only whether one works."""
    conn = db()
    try:
        return {
            "mailboxes": accounts.listing(conn, _scope(conn)),
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
        accounts.add(conn, m.address, m.app_password, member_id=_default_member(conn, m.member_id))
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
        row = next((m for m in accounts.listing(conn, _scope(conn)) if m["id"] == mailbox_id), None)
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
        visible = {row["id"] for row in accounts.listing(conn, _scope(conn))}
        if mailbox_id not in visible:
            raise HTTPException(404, "no such mailbox")
        accounts.remove(conn, mailbox_id)
        return {"status": "removed"}
    finally:
        conn.close()


@app.put("/api/mailboxes/{mailbox_id}/member")
def assign_mailbox_member(mailbox_id: int, body: MemberAssignmentIn):
    conn = db()
    try:
        visible = {row["id"] for row in accounts.listing(conn, _scope(conn))}
        if mailbox_id not in visible:
            raise HTTPException(404, "no such mailbox")
        member_id = _default_member(conn, body.member_id)
        conn.execute("UPDATE mailboxes SET member_id=? WHERE id=?", (member_id, mailbox_id))
        conn.commit()
        return {"status": "saved", "member_id": member_id}
    finally:
        conn.close()


# ---------------------------------------------------------------- ingest

def _spawn(fn, *args) -> None:
    log.info("starting background job: %s", getattr(fn, "__name__", "job"))
    threading.Thread(target=fn, args=args, daemon=True).start()


@app.post("/api/bank/ingest/upload")
async def bank_ingest_upload(
    files: list[UploadFile] = File(...),
    password: Optional[str] = Form(default=None),
    member_id: Optional[int] = Form(default=None),
):
    """Persist uploaded PDFs, then parse them on the normal background worker."""
    if not files or len(files) > 20:
        raise HTTPException(422, "upload between 1 and 20 PDF files")
    for upload in files:
        if Path(upload.filename or "").suffix.lower() != ".pdf":
            raise HTTPException(422, f"{upload.filename or 'file'} is not a PDF")
    if not _bank_lock.acquire(blocking=False):
        raise HTTPException(409, "a bank ingest run is already in progress")

    upload_dir = INBOX / "bank-uploads" / uuid.uuid4().hex
    saved: list[Path] = []
    try:
        upload_dir.mkdir(parents=True, exist_ok=False)
        for index, upload in enumerate(files, 1):
            original = Path(upload.filename or f"statement-{index}.pdf").name
            safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", original).strip(" .")
            if not safe.lower().endswith(".pdf"):
                safe += ".pdf"
            target = upload_dir / safe
            if target.exists():
                target = upload_dir / f"{target.stem}-{index}{target.suffix}"
            size = 0
            first = b""
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    if not first:
                        first = chunk[:5]
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise HTTPException(413, f"{original} exceeds the 25 MB upload limit")
                    handle.write(chunk)
            if first != b"%PDF-":
                raise HTTPException(422, f"{original} does not contain a valid PDF header")
            saved.append(target)
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        _bank_lock.release()
        raise
    finally:
        for upload in files:
            await upload.close()

    conn = db()
    try:
        selected_member = _default_member(conn, member_id)
    finally:
        conn.close()

    def job():
        try:
            bank_pipeline.run_scan(
                DB_PATH, saved, {"password": password, "_member_id": selected_member}
            )
        finally:
            _bank_lock.release()

    _spawn(job)
    return {"status": "started", "files": [path.name for path in saved]}


@app.get("/api/bank/pending")
def bank_pending():
    conn = db()
    try:
        return {"pending": bank_pipeline.pending(conn)}
    finally:
        conn.close()


@app.post("/api/bank/pending/discard")
def bank_discard_pending(body: dict):
    ids = [int(value) for value in body.get("file_ids", [])]
    conn = db()
    try:
        return {"discarded": bank_pipeline.discard(conn, ids)}
    finally:
        conn.close()


@app.post("/api/bank/pending/reevaluate")
def bank_reevaluate_pending(req: ReevaluateRequest):
    if not _bank_lock.acquire(blocking=False):
        raise HTTPException(409, "a bank ingest run is already in progress")
    credentials, ids = _owned_credentials(req), req.file_ids

    def job():
        try:
            bank_pipeline.run_reevaluate(DB_PATH, ids, credentials)
        finally:
            _bank_lock.release()

    _spawn(job)
    return {"status": "started", "count": len(ids) if ids else None}


@app.post("/api/bank/ingest/approve")
def bank_ingest_approve(req: ApproveRequest):
    if not _bank_lock.acquire(blocking=False):
        raise HTTPException(409, "a bank ingest run is already in progress")
    credentials, ids = _owned_credentials(req), req.file_ids

    def job():
        try:
            bank_pipeline.run_approve(DB_PATH, ids, credentials)
        finally:
            _bank_lock.release()

    _spawn(job)
    return {"status": "started", "count": len(ids)}


@app.get("/api/bank/runs")
def bank_runs(limit: int = 40):
    conn = db()
    try:
        return {"runs": bank_pipeline.runs(conn, limit), "busy": _bank_lock.locked()}
    finally:
        conn.close()


@app.get("/api/bank/runs/{run_id}")
def bank_run_detail(run_id: int):
    conn = db()
    try:
        # Must match the predicate bank_pipeline.runs() lists by. Guarding on a
        # single kind ('bank_import') 404'd every run the history actually offers —
        # scans, approvals and re-evaluations — leaving the detail pane empty.
        found = conn.execute(
            "SELECT 1 FROM ingest_runs WHERE id=? AND kind LIKE 'bank_%'", (run_id,)
        ).fetchone()
        if not found:
            raise HTTPException(404, "no such bank ingest run")
        return pipeline.run_detail(conn, run_id)
    finally:
        conn.close()


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

    creds, months, force = _owned_credentials(req), req.months, req.force
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

    creds, force = _owned_credentials(req), req.force
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
    if bool(req.month_from) != bool(req.month_to):
        raise HTTPException(422, "month_from and month_to must be supplied together")
    if req.month_from and req.month_to:
        try:
            pipeline.month_range_window(req.month_from, req.month_to)
        except (ValueError, TypeError):
            raise HTTPException(422, "month_from must be a valid month not after month_to")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")

    creds, months, month = _owned_credentials(req), req.months, req.month
    log.info("mail scan requested: %s", f"month {month}" if month else f"last {months} month(s)")

    def job():
        try:
            pipeline.run_scan(
                DB_PATH, INBOX, creds, months=months, month=month,
                month_from=req.month_from, month_to=req.month_to, card_ids=req.card_ids,
                connection_ids=req.connection_ids,
                include_unrecognized_cards=req.include_unrecognized_cards,
            )
        finally:
            _lock.release()

    _spawn(job)
    return {
        "status": "started", "month": month, "months": months,
        "month_from": req.month_from, "month_to": req.month_to,
        "card_ids": req.card_ids,
        "include_unrecognized_cards": req.include_unrecognized_cards,
        "connection_ids": req.connection_ids,
    }


@app.post("/api/ingest/scan-local")
def ingest_scan_local(req: ImportRequest):
    """Review flow for PDFs already on disk."""
    pdfs = _collect(req.paths)
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")
    creds = _owned_credentials(req)

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


@app.get("/api/pending/{file_id}/pdf")
def pending_pdf(file_id: int):
    """Open the PDF attached to a pending review row in the browser.

    The database supplies the path—callers cannot request an arbitrary local
    file. Encrypted statements are unlocked into a short-lived temporary copy
    using the same saved card passwords/profile conventions as ingestion.
    """
    conn = db()
    try:
        row = conn.execute(
            """SELECT filename, path FROM ingest_files
               WHERE id = ? AND status = 'pending'
                 AND COALESCE(document_type,'credit_card') != 'bank_account'""",
            (file_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such pending statement")
        source = Path(row["path"])
        if not source.is_file():
            raise HTTPException(404, "statement PDF is no longer on disk")

        from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted

        if not is_encrypted(source):
            return FileResponse(
                source,
                media_type="application/pdf",
                filename=row["filename"],
                content_disposition_type="inline",
            )

        known = accounts.all_card_passwords(conn)
        profile = accounts.get_profile(conn)
        passwords = list(dict.fromkeys(
            list(known.values())
            + candidate_passwords(profile.get("full_name"), profile.get("dob"))
        ))
    finally:
        conn.close()

    handle = tempfile.NamedTemporaryFile(prefix="sparser-view-", suffix=".pdf", delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        decrypt_to(source, temporary, passwords)
    except DecryptError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(422, "saved passwords could not unlock this PDF") from exc

    return FileResponse(
        temporary,
        media_type="application/pdf",
        filename=row["filename"],
        content_disposition_type="inline",
        background=BackgroundTask(temporary.unlink, missing_ok=True),
    )


@app.post("/api/pending/discard")
def discard_pending(body: dict):
    """Dismiss scanned statements without importing them."""
    ids = [int(i) for i in body.get("file_ids", [])]
    conn = db()
    try:
        return {"discarded": pipeline.discard(conn, ids)}
    finally:
        conn.close()


@app.post("/api/pending/reevaluate")
def reevaluate_pending(req: ReevaluateRequest):
    """Reparse pending PDFs with the currently installed analyzer versions."""
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")
    creds, ids = _owned_credentials(req), req.file_ids

    def job():
        try:
            pipeline.run_reevaluate(DB_PATH, ids, creds)
        finally:
            _lock.release()

    _spawn(job)
    return {"status": "started", "count": len(ids) if ids else None}


@app.post("/api/ingest/approve")
def ingest_approve(req: ApproveRequest):
    """Import only the statements the user selected."""
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest run is already in progress")
    creds, ids, force = _owned_credentials(req), req.file_ids, req.force

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
