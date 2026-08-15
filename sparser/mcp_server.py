"""Curated MCP server for open-ended card and bank-statement analytics.

The server deliberately exposes analytical operations instead of raw SQL. MCP
clients cannot read stored passwords or traverse local files. The sole mutation
is an explicit, transaction-scoped bank category override.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from . import bank_store, semantic as semantic_layer, store
from .env import load_local_env
from .store import to_rupees

load_local_env()
DB_PATH = Path(os.environ.get("SPARSER_DB", "data/statements.db")).expanduser().resolve()
#: Writes are opt-in. The default surface is analysis, and the Command Center
#: never needs the one mutating tool, so it should not be reachable by default.
WRITES_ENABLED = os.environ.get("SPARSER_MCP_ALLOW_WRITES", "").strip() in {"1", "true", "yes"}

mcp = FastMCP(
    "Statement Analytics",
    instructions=(
        "Analysis of imported credit-card and bank-account statements. Start with list_cards "
        "or list_bank_accounts, narrow by ISO dates and IDs, and cite counts and totals. "
        "Call list_users_members first when a question names a person: ownership lives on the "
        "card and the account as member_id, never on the transaction, and is null where an "
        "instrument was never assigned. "
        "Only set_bank_transaction_category may write, and only when the user explicitly "
        "asks to correct one transaction's category. "
        "Amounts are INR unless a foreign currency is explicitly present."
    ),
    json_response=True,
)


def _connect() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"statement database not found: {DB_PATH}")
    uri = f"file:{quote(str(DB_PATH))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _first(conn: sqlite3.Connection, *statements: str) -> sqlite3.Cursor:
    """Run the first statement this database can answer.

    The connection is read-only, so a database that predates the household
    ownership migration cannot be brought up to date here. Rather than fail, the
    caller passes a second statement naming only long-standing columns, with the
    ownership fields selected as NULL — an unattributed card, which is the
    truth for that database.
    """
    last: sqlite3.OperationalError | None = None
    for statement in statements:
        try:
            return conn.execute(statement)
        except sqlite3.OperationalError as error:
            last = error
    raise last if last else ValueError("no statement supplied")


def _connect_write() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"statement database not found: {DB_PATH}")
    return store.connect(DB_PATH)


def _date(value: Optional[str], name: str) -> Optional[str]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def _where(
    card_ids: Optional[list[int]],
    date_from: Optional[str],
    date_to: Optional[str],
    *,
    direction: Optional[str] = None,
) -> tuple[str, list[object]]:
    terms = ["1=1"]
    args: list[object] = []
    if card_ids:
        terms.append(f"t.card_id IN ({','.join('?' * len(card_ids))})")
        args.extend(card_ids)
    if start := _date(date_from, "date_from"):
        terms.append("t.txn_date >= ?")
        args.append(start)
    if end := _date(date_to, "date_to"):
        terms.append("t.txn_date <= ?")
        args.append(end)
    if direction:
        if direction not in {"debit", "credit"}:
            raise ValueError("direction must be debit or credit")
        terms.append("t.direction = ?")
        args.append(direction)
    return " AND ".join(terms), args


def _bank_where(
    account_ids: Optional[list[int]],
    date_from: Optional[str],
    date_to: Optional[str],
    *,
    direction: Optional[str] = None,
) -> tuple[str, list[object]]:
    terms = ["1=1"]
    args: list[object] = []
    if account_ids:
        terms.append(f"t.account_id IN ({','.join('?' * len(account_ids))})")
        args.extend(account_ids)
    if start := _date(date_from, "date_from"):
        terms.append("t.txn_date >= ?")
        args.append(start)
    if end := _date(date_to, "date_to"):
        terms.append("t.txn_date <= ?")
        args.append(end)
    if direction:
        if direction not in {"debit", "credit"}:
            raise ValueError("direction must be debit or credit")
        terms.append("t.direction = ?")
        args.append(direction)
    return " AND ".join(terms), args


_BANK_CATEGORY = "COALESCE(NULLIF(TRIM(t.category_override),''),t.category,'Other')"


def _money_row(row: sqlite3.Row, fields: tuple[str, ...]) -> dict:
    item = dict(row)
    for field in fields:
        item[field] = to_rupees(item[field])
    return item


@mcp.tool()
def list_users_members() -> dict:
    """Portal users and the household members beneath them.

    Call this first when a question names a person. Cards and bank accounts
    carry a `member_id`, not a name, and a member id is only meaningful inside
    its portal user — two users can both have a member called `Personal`.

    `unassigned_cards` and `unassigned_bank_accounts` count instruments with no
    member at all. They belong to nobody, so they are in no per-person total,
    and a household figure that ignores them is short by exactly that much.
    """
    with _connect() as conn:
        try:
            users = conn.execute(
                "SELECT id, email, display_name FROM portal_users ORDER BY id"
            ).fetchall()
            members = conn.execute(
                """SELECT m.id, m.user_id, m.name, m.is_default,
                          (SELECT COUNT(*) FROM cards c WHERE c.member_id = m.id) AS cards,
                          (SELECT COUNT(*) FROM bank_accounts a WHERE a.member_id = m.id)
                              AS bank_accounts
                   FROM members m ORDER BY m.user_id, m.is_default DESC, m.name"""
            ).fetchall()
            orphan_cards = conn.execute(
                "SELECT COUNT(*) AS n FROM cards WHERE member_id IS NULL"
            ).fetchone()["n"]
            orphan_accounts = conn.execute(
                "SELECT COUNT(*) AS n FROM bank_accounts WHERE member_id IS NULL"
            ).fetchone()["n"]
        except sqlite3.OperationalError:
            # A database that predates the portal migration has no identity
            # tables. That is "nobody registered yet", not a broken tool.
            return {
                "database": str(DB_PATH),
                "users": [],
                "unassigned_cards": None,
                "unassigned_bank_accounts": None,
                "note": (
                    "This statement database predates household ownership, so every card "
                    "and bank account is unattributed. Register a portal user to assign them."
                ),
            }
    by_user: defaultdict[int, list[dict]] = defaultdict(list)
    for member in members:
        item = dict(member)
        by_user[item.pop("user_id")].append(item)
    return {
        "database": str(DB_PATH),
        "users": [{**dict(user), "members": by_user[user["id"]]} for user in users],
        "unassigned_cards": orphan_cards,
        "unassigned_bank_accounts": orphan_accounts,
    }


@mcp.tool()
def list_cards() -> dict:
    """List available cards, IDs, owning member, date coverage, and transaction counts."""
    with _connect() as conn:
        rows = _first(
            conn,
            """SELECT c.id, c.display_name, c.issuer, c.product, c.last4,
                      c.member_id, m.name AS member_name,
                      COUNT(t.id) AS transactions, MIN(t.txn_date) AS first_date,
                      MAX(t.txn_date) AS last_date
               FROM cards c LEFT JOIN transactions t ON t.card_id = c.id
               LEFT JOIN members m ON m.id = c.member_id
               GROUP BY c.id ORDER BY c.display_name""",
            """SELECT c.id, c.display_name, c.issuer, c.product, c.last4,
                      NULL AS member_id, NULL AS member_name,
                      COUNT(t.id) AS transactions, MIN(t.txn_date) AS first_date,
                      MAX(t.txn_date) AS last_date
               FROM cards c LEFT JOIN transactions t ON t.card_id = c.id
               GROUP BY c.id ORDER BY c.display_name""",
        ).fetchall()
    return {"database": str(DB_PATH), "cards": [dict(r) for r in rows]}


@mcp.tool()
def list_bank_accounts() -> dict:
    """List bank accounts with IDs, owning member, coverage, and imported statement periods."""
    with _connect() as conn:
        rows = _first(
            conn,
            """SELECT a.id, a.display_name, a.bank_code, a.bank_name, a.last4,
                      a.account_type, a.product, a.member_id, m.name AS member_name,
                      COUNT(DISTINCT s.id) statements,
                      COUNT(t.id) transactions, MIN(t.txn_date) first_date,
                      MAX(t.txn_date) last_date
               FROM bank_accounts a
               LEFT JOIN bank_statements s ON s.account_id=a.id
               LEFT JOIN bank_transactions t ON t.statement_id=s.id
               LEFT JOIN members m ON m.id=a.member_id
               GROUP BY a.id ORDER BY a.display_name""",
            """SELECT a.id, a.display_name, a.bank_code, a.bank_name, a.last4,
                      a.account_type, a.product, NULL AS member_id, NULL AS member_name,
                      COUNT(DISTINCT s.id) statements,
                      COUNT(t.id) transactions, MIN(t.txn_date) first_date,
                      MAX(t.txn_date) last_date
               FROM bank_accounts a
               LEFT JOIN bank_statements s ON s.account_id=a.id
               LEFT JOIN bank_transactions t ON t.statement_id=s.id
               GROUP BY a.id ORDER BY a.display_name""",
        ).fetchall()
        periods = conn.execute(
            """SELECT id, account_id, period_start, period_end, coverage_start,
                      coverage_end, parser_id, confidence, imported_at
               FROM bank_statements ORDER BY account_id, period_end"""
        ).fetchall()
    by_account: defaultdict[int, list[dict]] = defaultdict(list)
    for period in periods:
        item = dict(period)
        by_account[item.pop("account_id")].append(item)
    accounts = []
    for row in rows:
        item = dict(row)
        item["statement_periods"] = by_account[item["id"]]
        accounts.append(item)
    return {"database": str(DB_PATH), "accounts": accounts}


@mcp.tool()
def get_bank_overview(
    account_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Get exact bank withdrawals, deposits, net cash flow, balances, and monthly totals."""
    clause, args = _bank_where(account_ids, date_from, date_to)
    with _connect() as conn:
        totals = conn.execute(
            f"""SELECT COUNT(*) transactions,
                       COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END),0) withdrawals,
                       COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) deposits,
                       COALESCE(MAX(CASE WHEN direction='debit' THEN amount ELSE 0 END),0) largest_withdrawal
                FROM bank_transactions t WHERE {clause}""",
            args,
        ).fetchone()
        monthly = conn.execute(
            f"""SELECT substr(txn_date,1,7) month,
                       COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END),0) withdrawals,
                       COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) deposits,
                       COUNT(*) transactions
                FROM bank_transactions t WHERE {clause}
                GROUP BY month ORDER BY month""",
            args,
        ).fetchall()
        ledger = conn.execute(
            f"""SELECT account_id, amount, direction, balance
                FROM bank_transactions t WHERE {clause} ORDER BY txn_date, id""",
            args,
        ).fetchall()
    first: dict[int, sqlite3.Row] = {}
    last: dict[int, sqlite3.Row] = {}
    for row in ledger:
        first.setdefault(row["account_id"], row)
        last[row["account_id"]] = row
    opening = sum(
        row["balance"] + row["amount"] if row["direction"] == "debit"
        else row["balance"] - row["amount"]
        for row in first.values()
    )
    closing = sum(row["balance"] for row in last.values())
    result = _money_row(totals, ("withdrawals", "deposits", "largest_withdrawal"))
    result["net_cash_flow"] = round(result["deposits"] - result["withdrawals"], 2)
    result["opening_balance"] = to_rupees(opening)
    result["closing_balance"] = to_rupees(closing)
    return {
        "filters": {"account_ids": account_ids or [], "date_from": date_from, "date_to": date_to},
        "totals": result,
        "monthly": [_money_row(row, ("withdrawals", "deposits")) for row in monthly],
    }


@mcp.tool()
def search_bank_transactions(
    query: Optional[str] = None,
    account_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    direction: Optional[Literal["debit", "credit"]] = None,
    category: Optional[str] = None,
    minimum_amount: Optional[float] = None,
    maximum_amount: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Search bank rows by narration, reference, counterparty, category, dates, and amount."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    clause, args = _bank_where(account_ids, date_from, date_to, direction=direction)
    terms = [clause]
    if query:
        terms.append("(t.description LIKE ? OR t.reference LIKE ? OR t.counterparty LIKE ?)")
        needle = f"%{query}%"
        args.extend([needle, needle, needle])
    if category:
        terms.append(f"{_BANK_CATEGORY} = ? COLLATE NOCASE")
        args.append(category)
    if minimum_amount is not None:
        terms.append("t.amount >= ?")
        args.append(round(minimum_amount * 100))
    if maximum_amount is not None:
        terms.append("t.amount <= ?")
        args.append(round(maximum_amount * 100))
    where = " AND ".join(terms)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM bank_transactions t WHERE {where}", args
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT t.id, t.txn_date, t.value_date, t.description, t.reference,
                       t.counterparty, {_BANK_CATEGORY} category, t.category derived_category,
                       t.category_override, t.amount, t.direction, t.balance,
                       a.display_name account
                FROM bank_transactions t JOIN bank_accounts a ON a.id=t.account_id
                WHERE {where} ORDER BY t.txn_date DESC, t.id DESC LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = _money_row(row, ("amount", "balance"))
        item["category_is_override"] = item["category_override"] is not None
        items.append(item)
    return {"total_matches": total, "limit": limit, "offset": offset, "transactions": items}


@mcp.tool()
def bank_cashflow_breakdown(
    group_by: Literal["month", "category", "counterparty", "account"],
    direction: Literal["debit", "credit"],
    account_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Group withdrawals or deposits by month, effective category, counterparty, or account."""
    expressions = {
        "month": "substr(t.txn_date,1,7)",
        "category": _BANK_CATEGORY,
        "counterparty": "COALESCE(t.counterparty,t.description,'Unknown')",
        "account": "a.display_name",
    }
    clause, args = _bank_where(account_ids, date_from, date_to, direction=direction)
    limit = max(1, min(limit, 200))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT {expressions[group_by]} label, SUM(t.amount) amount,
                       COUNT(*) transactions
                FROM bank_transactions t JOIN bank_accounts a ON a.id=t.account_id
                WHERE {clause} GROUP BY label ORDER BY amount DESC LIMIT ?""",
            args + [limit],
        ).fetchall()
        total = conn.execute(
            f"SELECT COALESCE(SUM(t.amount),0) FROM bank_transactions t WHERE {clause}", args
        ).fetchone()[0]
    total_rupees = to_rupees(total)
    groups = []
    for row in rows:
        item = _money_row(row, ("amount",))
        item["share_percent"] = round(item["amount"] * 100 / total_rupees, 2) if total_rupees else 0
        groups.append(item)
    return {"group_by": group_by, "direction": direction, "total": total_rupees, "groups": groups}


@mcp.tool()
def bank_statement_health(account_ids: Optional[list[int]] = None) -> dict:
    """Summarize bank-parser confidence and failed checks for imported statement periods."""
    terms, args = ["1=1"], []
    if account_ids:
        terms.append(f"s.account_id IN ({','.join('?' * len(account_ids))})")
        args.extend(account_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.period_start, s.period_end, s.coverage_start, s.coverage_end,
                       s.confidence, s.parser_id, s.checks_json, a.display_name account
                FROM bank_statements s JOIN bank_accounts a ON a.id=s.account_id
                WHERE {' AND '.join(terms)} ORDER BY s.period_start""",
            args,
        ).fetchall()
    failed_checks: defaultdict[str, int] = defaultdict(int)
    statements = []
    for row in rows:
        item = dict(row)
        checks = json.loads(item.pop("checks_json") or "[]")
        failed = [check["name"] for check in checks if not check.get("passed")]
        for name in failed:
            failed_checks[name] += 1
        item["failed_checks"] = failed
        statements.append(item)
    return {
        "statements": len(statements),
        "fully_reconciled": sum(item["confidence"] == 1 for item in statements),
        "failed_check_counts": dict(sorted(failed_checks.items())),
        "details": statements,
    }


@mcp.tool()
def bank_pipeline_status(limit: int = 20) -> dict:
    """Inspect recent bank import runs and statements currently awaiting approval."""
    limit = max(1, min(limit, 100))
    with _connect() as conn:
        runs = conn.execute(
            """SELECT r.id, r.kind, r.status, r.started_at, r.finished_at, r.note,
                      (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id) files,
                      (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id AND f.status='ok') ok,
                      (SELECT COUNT(*) FROM ingest_files f WHERE f.run_id=r.id AND f.status='pending') pending
               FROM ingest_runs r WHERE r.kind LIKE 'bank_%'
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        pending = conn.execute(
            """SELECT f.id, f.filename, f.issuer bank, f.card account, f.template_id parser_id,
                      f.encrypted, f.txn_count, f.confidence, f.statement_date,
                      f.period_start, f.period_end, f.duplicate_of, f.checks_json, f.error
               FROM ingest_files f
               WHERE f.status='pending' AND f.document_type='bank_account'
               ORDER BY f.id DESC"""
        ).fetchall()
    review = []
    for row in pending:
        item = dict(row)
        item["encrypted"] = bool(item["encrypted"])
        item["is_duplicate"] = item.pop("duplicate_of") is not None
        item["checks"] = json.loads(item.pop("checks_json") or "[]")
        review.append(item)
    return {"runs": [dict(row) for row in runs], "pending_review": review}


@mcp.tool()
def set_bank_transaction_category(transaction_id: int, category: Optional[str] = None) -> dict:
    """Explicitly set one bank row's category; blank/null restores automatic categorization.

    This is the only tool here that writes. It is refused unless the server was
    started with writes enabled, because this MCP is reachable by any local
    client and an analysis surface should not silently carry an edit path — a
    master analyser that only ever reads has no reason to hold it.
    """
    if not WRITES_ENABLED:
        raise PermissionError(
            "This server is running read-only. Set SPARSER_MCP_ALLOW_WRITES=1 to "
            "enable category overrides."
        )
    with _connect_write() as conn:
        return bank_store.update_transaction_category(conn, transaction_id, category)


@mcp.tool()
def get_overview(
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Get exact spend, payments, net movement, counts, and monthly totals."""
    clause, args = _where(card_ids, date_from, date_to)
    with _connect() as conn:
        totals = conn.execute(
            f"""SELECT COUNT(*) AS transactions,
                       COALESCE(SUM(spend_effect),0) AS spend,
                       COALESCE(SUM(payment_effect),0) AS payments,
                       COALESCE(MAX(spend_effect),0) AS largest
                FROM transactions t WHERE {clause}""",
            args,
        ).fetchone()
        monthly = conn.execute(
            f"""SELECT substr(txn_date,1,7) AS month,
                       COALESCE(SUM(spend_effect),0) AS spend,
                       COALESCE(SUM(payment_effect),0) AS payments,
                       COUNT(*) AS transactions
                FROM transactions t WHERE {clause}
                GROUP BY month ORDER BY month""",
            args,
        ).fetchall()
    result = _money_row(totals, ("spend", "payments", "largest"))
    result["net_movement"] = round(result["spend"] - result["payments"], 2)
    return {
        "filters": {"card_ids": card_ids or [], "date_from": date_from, "date_to": date_to},
        "totals": result,
        "monthly": [_money_row(r, ("spend", "payments")) for r in monthly],
    }


@mcp.tool()
def search_transactions(
    query: Optional[str] = None,
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    direction: Optional[Literal["debit", "credit"]] = None,
    ledger: Optional[Literal["emi", "rewards", "foreign_currency"]] = None,
    category: Optional[str] = None,
    minimum_amount: Optional[float] = None,
    maximum_amount: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Search transactions by text, card, dates, direction, category, and INR amount."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    clause, args = _where(card_ids, date_from, date_to, direction=direction)
    terms = [clause]
    if query:
        terms.append("(t.description LIKE ? OR t.merchant LIKE ?)")
        needle = f"%{query}%"
        args += [needle, needle]
    if category:
        terms.append("t.category = ? COLLATE NOCASE")
        args.append(category)
    if ledger == "emi":
        terms.append("t.is_emi = 1")
    elif ledger == "rewards":
        terms.append("t.reward_points IS NOT NULL")
    elif ledger == "foreign_currency":
        terms.append("t.fcy_currency IS NOT NULL")
    if minimum_amount is not None:
        terms.append("t.amount >= ?")
        args.append(round(minimum_amount * 100))
    if maximum_amount is not None:
        terms.append("t.amount <= ?")
        args.append(round(maximum_amount * 100))
    where = " AND ".join(terms)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT t.id, t.txn_date, t.txn_time, t.description, t.merchant, t.category,
                       t.amount, t.direction, t.is_emi, t.reward_points, t.fcy_currency,
                       t.fcy_amount, c.display_name AS card
                FROM transactions t JOIN cards c ON c.id=t.card_id
                WHERE {where} ORDER BY t.txn_date DESC, t.id DESC LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = _money_row(row, ("amount", "fcy_amount"))
        item["is_emi"] = bool(item["is_emi"])
        items.append(item)
    return {"total_matches": total, "limit": limit, "offset": offset, "transactions": items}


@mcp.tool()
def special_ledger(
    ledger: Literal["rewards", "emi", "foreign_currency"],
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Aggregate reward points, EMI rows, or foreign-currency transactions by month."""
    clause, args = _where(card_ids, date_from, date_to)
    conditions = {
        "rewards": "t.reward_points IS NOT NULL",
        "emi": "t.is_emi = 1",
        "foreign_currency": "t.fcy_currency IS NOT NULL",
    }
    clause += f" AND {conditions[ledger]}"
    with _connect() as conn:
        if ledger == "rewards":
            rows = conn.execute(
                f"""SELECT substr(txn_date,1,7) month, SUM(reward_points) points,
                           COUNT(*) transactions
                    FROM transactions t WHERE {clause} GROUP BY month ORDER BY month""",
                args,
            ).fetchall()
            return {
                "ledger": ledger,
                "total_points": sum(r["points"] for r in rows),
                "transactions": sum(r["transactions"] for r in rows),
                "monthly": [dict(r) for r in rows],
            }
        if ledger == "emi":
            rows = conn.execute(
                f"""SELECT substr(txn_date,1,7) month, SUM(amount) amount,
                           COUNT(*) transactions
                    FROM transactions t WHERE {clause} GROUP BY month ORDER BY month""",
                args,
            ).fetchall()
            return {
                "ledger": ledger,
                "total_inr": to_rupees(sum(r["amount"] for r in rows)),
                "transactions": sum(r["transactions"] for r in rows),
                "monthly": [_money_row(r, ("amount",)) for r in rows],
            }
        rows = conn.execute(
            f"""SELECT substr(txn_date,1,7) month, fcy_currency currency,
                       SUM(fcy_amount) original_amount, SUM(amount) billed_inr,
                       COUNT(*) transactions
                FROM transactions t WHERE {clause}
                GROUP BY month, currency ORDER BY month, currency""",
            args,
        ).fetchall()
    return {
        "ledger": ledger,
        "total_billed_inr": to_rupees(sum(r["billed_inr"] for r in rows)),
        "transactions": sum(r["transactions"] for r in rows),
        "monthly_by_currency": [
            _money_row(r, ("original_amount", "billed_inr")) for r in rows
        ],
    }


@mcp.tool()
def spending_breakdown(
    group_by: Literal["month", "category", "merchant", "card"],
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Group purchase-based net spending by month, category, merchant, or card."""
    expressions = {
        "month": "substr(t.txn_date,1,7)",
        "category": "COALESCE(t.category,'Uncategorised')",
        "merchant": "COALESCE(t.merchant,t.description)",
        "card": "c.display_name",
    }
    expression = expressions[group_by]
    clause, args = _where(card_ids, date_from, date_to)
    clause += " AND t.spend_effect != 0"
    limit = max(1, min(limit, 200))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT {expression} AS label, SUM(t.spend_effect) AS amount,
                       COUNT(*) AS transactions
                FROM transactions t JOIN cards c ON c.id=t.card_id
                WHERE {clause} GROUP BY label ORDER BY amount DESC LIMIT ?""",
            args + [limit],
        ).fetchall()
        total = conn.execute(
            f"SELECT COALESCE(SUM(t.spend_effect),0) FROM transactions t WHERE {clause}", args
        ).fetchone()[0]
    total_rupees = to_rupees(total)
    groups = []
    for row in rows:
        item = _money_row(row, ("amount",))
        item["share_percent"] = round(item["amount"] * 100 / total_rupees, 2) if total_rupees else 0
        groups.append(item)
    return {"group_by": group_by, "total_spend": total_rupees, "groups": groups}


@mcp.tool()
def compare_periods(
    first_from: str,
    first_to: str,
    second_from: str,
    second_to: str,
    card_ids: Optional[list[int]] = None,
) -> dict:
    """Compare spend and category mix between two explicit date periods."""
    def period(start: str, end: str) -> dict:
        clause, args = _where(card_ids, start, end)
        clause += " AND t.spend_effect != 0"
        with _connect() as conn:
            total = conn.execute(
                f"""SELECT COALESCE(SUM(spend_effect),0), COUNT(*)
                    FROM transactions t WHERE {clause}""", args
            ).fetchone()
            categories = conn.execute(
                f"""SELECT COALESCE(category,'Uncategorised') label,
                           SUM(spend_effect) amount
                    FROM transactions t WHERE {clause} GROUP BY label ORDER BY amount DESC""",
                args,
            ).fetchall()
        return {
            "from": _date(start, "period_from"), "to": _date(end, "period_to"),
            "spend": to_rupees(total[0]), "transactions": total[1],
            "categories": {r["label"]: to_rupees(r["amount"]) for r in categories},
        }

    first, second = period(first_from, first_to), period(second_from, second_to)
    labels = sorted(set(first["categories"]) | set(second["categories"]))
    category_changes = [
        {
            "category": label,
            "first": first["categories"].get(label, 0),
            "second": second["categories"].get(label, 0),
            "change": round(second["categories"].get(label, 0) - first["categories"].get(label, 0), 2),
        }
        for label in labels
    ]
    category_changes.sort(key=lambda r: abs(r["change"]), reverse=True)
    return {
        "first": first, "second": second,
        "spend_change": round(second["spend"] - first["spend"], 2),
        "spend_change_percent": (
            round((second["spend"] - first["spend"]) * 100 / first["spend"], 2)
            if first["spend"] else None
        ),
        "category_changes": category_changes,
    }


@mcp.tool()
def find_recurring_merchants(
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    minimum_months: int = 3,
    limit: int = 50,
) -> dict:
    """Find debit merchants appearing across multiple distinct months."""
    minimum_months = max(2, minimum_months)
    limit = max(1, min(limit, 200))
    clause, args = _where(card_ids, date_from, date_to)
    clause += " AND t.spend_effect > 0"
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(t.merchant,t.description) AS merchant,
                       COUNT(*) AS transactions, COUNT(DISTINCT substr(txn_date,1,7)) AS months,
                       SUM(spend_effect) AS total, MIN(spend_effect) AS minimum,
                       MAX(spend_effect) AS maximum,
                       MIN(txn_date) AS first_date, MAX(txn_date) AS last_date
                FROM transactions t WHERE {clause}
                GROUP BY merchant HAVING months >= ?
                ORDER BY months DESC, transactions DESC LIMIT ?""",
            args + [minimum_months, limit],
        ).fetchall()
    return {
        "minimum_months": minimum_months,
        "merchants": [_money_row(r, ("total", "minimum", "maximum")) for r in rows],
    }


@mcp.tool()
def find_unusual_transactions(
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 30,
) -> dict:
    """Rank unusually large debits using robust median absolute deviation."""
    clause, args = _where(card_ids, date_from, date_to)
    clause += " AND t.spend_effect > 0"
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.txn_date, t.description, t.merchant, t.category,
                       t.spend_effect AS amount,
                       c.display_name card FROM transactions t JOIN cards c ON c.id=t.card_id
                WHERE {clause}""",
            args,
        ).fetchall()
    if not rows:
        return {"method": "median absolute deviation", "transactions": []}
    amounts = [r["amount"] for r in rows]
    median = statistics.median(amounts)
    mad = statistics.median(abs(value - median) for value in amounts)
    scored = []
    for row in rows:
        score = 0.6745 * (row["amount"] - median) / mad if mad else 0.0
        if score >= 3.5 or (mad == 0 and row["amount"] > median):
            item = _money_row(row, ("amount",))
            item["robust_z_score"] = round(score, 2) if math.isfinite(score) else None
            scored.append(item)
    scored.sort(key=lambda item: (item["robust_z_score"] or 0, item["amount"]), reverse=True)
    return {
        "method": "median absolute deviation",
        "median_amount": to_rupees(round(median)),
        "mad_amount": to_rupees(round(mad)),
        "threshold_score": 3.5,
        "transactions": scored[: max(1, min(limit, 100))],
    }


@mcp.tool()
def statements_schema() -> dict:
    """Tables, columns, row counts and correctness rules for `statements_query`.

    Call this before the first query. The rules are not style advice — each one
    describes a mistake that returns a plausible wrong number rather than an
    error.
    """
    with _connect() as conn:
        semantic = semantic_layer.build_semantic_database(conn)
    try:
        return semantic_layer.describe(semantic)
    finally:
        semantic.close()


@mcp.tool()
def statements_query(sql: str, max_rows: int = 500) -> dict:
    """Run one read-only SELECT over the safe card and bank tables.

    This is the escape hatch for anything the fixed tools do not group the way a
    question needs — two-dimensional breakdowns, window functions over months,
    self-joins. Call `statements_schema` first for columns and rules.

    The query runs against an in-memory copy containing only analytical
    columns, so stored statement passwords and portal credentials are not
    reachable from here regardless of what the SQL asks for.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise ValueError("sql must not be empty")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("only a single SELECT (or WITH ... SELECT) is allowed")
    if ";" in statement:
        raise ValueError("only one statement may be run per call")
    max_rows = max(1, min(max_rows, 5000))
    with _connect() as conn:
        semantic = semantic_layer.build_semantic_database(conn)
    try:
        semantic.set_progress_handler(_deadline(dt.datetime.now(), 10.0), 10_000)
        cursor = semantic.execute(statement)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [description[0] for description in cursor.description or []]
    except sqlite3.OperationalError as error:
        raise ValueError(f"query failed: {error}") from error
    finally:
        semantic.set_progress_handler(None, 0)
        semantic.close()
    truncated = len(rows) > max_rows
    payload = {
        "columns": columns,
        "row_count": min(len(rows), max_rows),
        "rows": [dict(row) for row in rows[:max_rows]],
    }
    if truncated:
        payload["warning"] = (
            "The row cap cut this result short, so it is not the whole answer. "
            "Aggregate in SQL or narrow the date range."
        )
    return payload


def _deadline(started: dt.datetime, seconds: float):
    """Abort a runaway query instead of hanging the client."""
    def handler() -> int:
        return 1 if (dt.datetime.now() - started).total_seconds() > seconds else 0

    return handler


@mcp.tool()
def card_statement_ledger(
    card_ids: Optional[list[int]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Billed statement figures per card cycle: dues, minimum due, due date, credit
    limit, available credit and finance charges.

    These come off the statement header, not from summing transactions, so they
    are the card's own account of what was owed. Use this for credit
    utilisation, interest leakage and due-date discipline; the transaction
    tools cannot answer any of those because none of these figures is a
    transaction row.
    """
    terms, args = ["1=1"], []
    if card_ids:
        terms.append(f"s.card_id IN ({','.join('?' * len(card_ids))})")
        args.extend(card_ids)
    if date_from:
        terms.append("COALESCE(s.period_end,s.statement_date) >= ?")
        args.append(_date(date_from, "date_from"))
    if date_to:
        terms.append("COALESCE(s.period_end,s.statement_date) <= ?")
        args.append(_date(date_to, "date_to"))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.card_id, c.display_name card, s.statement_date,
                       s.period_start, s.period_end, s.currency, s.due_date,
                       s.previous_dues, s.payments_credits, s.purchases_debits,
                       s.finance_charges, s.total_dues, s.minimum_due,
                       s.credit_limit, s.available_credit, s.confidence
                FROM statements s JOIN cards c ON c.id=s.card_id
                WHERE {' AND '.join(terms)}
                ORDER BY s.card_id, COALESCE(s.period_end,s.statement_date)""",
            args,
        ).fetchall()
    money_fields = (
        "previous_dues", "payments_credits", "purchases_debits", "finance_charges",
        "total_dues", "minimum_due", "credit_limit", "available_credit",
    )
    cycles = []
    total_finance_charges = 0.0
    for row in rows:
        item = dict(row)
        for field in money_fields:
            item[field] = to_rupees(item[field]) if item[field] is not None else None
        limit, dues = item["credit_limit"], item["total_dues"]
        # Utilisation is only meaningful where the parser actually captured a
        # limit; reporting 0% for a statement that never printed one would read
        # as disciplined rather than unknown.
        item["utilisation_percent"] = (
            round(dues * 100 / limit, 2) if limit and dues is not None else None
        )
        item["paid_in_full"] = (
            None if item["total_dues"] is None or item["payments_credits"] is None
            else item["payments_credits"] >= item["total_dues"]
        )
        if item["finance_charges"]:
            total_finance_charges += item["finance_charges"]
        cycles.append(item)
    limits = [item["credit_limit"] for item in cycles if item["credit_limit"]]
    utilisations = [
        item["utilisation_percent"] for item in cycles
        if item["utilisation_percent"] is not None
    ]
    return {
        "filters": {"card_ids": card_ids or [], "date_from": date_from, "date_to": date_to},
        "cycles": len(cycles),
        "total_finance_charges": round(total_finance_charges, 2),
        "cycles_with_finance_charges": sum(1 for i in cycles if i["finance_charges"]),
        "cycles_missing_credit_limit": sum(1 for i in cycles if not i["credit_limit"]),
        "peak_credit_limit": max(limits) if limits else None,
        "peak_utilisation_percent": max(utilisations) if utilisations else None,
        "statements": cycles,
    }


@mcp.tool()
def statement_health(card_ids: Optional[list[int]] = None) -> dict:
    """Summarize parser confidence, analyzer versions, and failed validation checks."""
    terms, args = ["1=1"], []
    if card_ids:
        terms.append(f"s.card_id IN ({','.join('?' * len(card_ids))})")
        args.extend(card_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.statement_date, s.period_start, s.period_end, s.confidence,
                       s.template_id, s.checks_json, c.display_name card
                FROM statements s JOIN cards c ON c.id=s.card_id
                WHERE {' AND '.join(terms)} ORDER BY s.statement_date""",
            args,
        ).fetchall()
    failed_checks: defaultdict[str, int] = defaultdict(int)
    statements = []
    for row in rows:
        item = dict(row)
        checks = json.loads(item.pop("checks_json") or "[]")
        failed = [check["name"] for check in checks if not check.get("passed")]
        for name in failed:
            failed_checks[name] += 1
        item["failed_checks"] = failed
        statements.append(item)
    return {
        "statements": len(statements),
        "fully_reconciled": sum(s["confidence"] == 1 for s in statements),
        "failed_check_counts": dict(sorted(failed_checks.items())),
        "details": statements,
    }


@mcp.resource("statement-parser://schema")
def schema_resource() -> str:
    """Explain the analytical data model and units."""
    return (
        "Cards and bank accounts use separate statement and transaction tables. Dates are ISO "
        "YYYY-MM-DD. Tool outputs express amounts in INR rupees; SQLite stores integer paise. "
        "Bank withdrawals and deposits remain separate; balance is the post-transaction balance. "
        "A bank category_override, when present, is the effective category while category remains "
        "the parser-derived value. "
        "spend uses purchase-based spend_effect: purchases and fees are positive, genuine "
        "refunds are negative, and EMI principal/bookkeeping is zero. payment_effect contains "
        "only actual card payments. direction remains the raw statement-side debit/credit. "
        "Reward points and original foreign-currency amounts are separate units. "
        "Statements with confidence=1 passed every arithmetic reconciliation check."
    )


@mcp.prompt()
def analyze_statement_history(question: str) -> str:
    """Create a disciplined workflow for answering a financial-history question."""
    return (
        f"Answer this question using Statement Analytics MCP tools: {question}\n\n"
        "First identify whether the question concerns cards or bank accounts, then inspect the "
        "matching IDs/date coverage. Choose the narrowest relevant tools and filters. Separate "
        "card spend from payments and bank withdrawals from deposits. Keep INR, foreign currency, "
        "and points distinct. Never change a bank category unless the user explicitly requests it. "
        "Quantify claims with totals/counts/date ranges, identify limitations, and do not infer "
        "intent or fraud from a merchant name alone."
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
