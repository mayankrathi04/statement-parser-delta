"""Persistence and analytics for deposit-account statements."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter
from typing import Any, Iterable, Optional

from . import store
from .banks import BankStatement
from .enrich import categorize

_ENRICHMENT_VERSION = "4"
MAX_CATEGORY_LENGTH = 80


def display_name(stmt: BankStatement) -> str:
    kind = "Account"
    account_type = (stmt.account_type or "").lower()
    if "saving" in account_type:
        kind = "Savings"
    elif "current" in account_type:
        kind = "Current"
    elif stmt.product:
        kind = stmt.product.title()
    return f"{stmt.bank_name} {kind} ••{stmt.last4}"


def _counterparty(description: str) -> str:
    text = " ".join((description or "").split()).strip(" -")
    upper = text.upper()
    parts = [part.strip() for part in text.split("-") if part.strip()]
    if upper.startswith("UPI-") and len(parts) > 1:
        return parts[1][:80]
    if upper.startswith(("IMPS-", "REV-IMPS-")):
        offset = 3 if upper.startswith("REV-IMPS-") else 2
        if len(parts) > offset:
            return parts[offset][:80]
    if upper.startswith("NEFT") and len(parts) > 2:
        return parts[2][:80]
    if upper.startswith("ACH") and len(parts) > 1:
        return parts[1][:80]
    if upper.startswith("POS "):
        match = re.search(r"X{4,}\d{2,}\s+(.+)", text, re.I)
        if match:
            return match.group(1)[:80]
    return (parts[0] if parts else text)[:80]


def _category(description: str, rules=None) -> str:
    """User rules first, then the built-in bank heuristics.

    The rule list is shared with the card ledger, so one category means the same
    thing on both sides of the app.
    """
    text = description or ""
    if rules:
        matched = rules.match(text)
        if matched:
            return matched
    if re.search(r"^\s*ACH\s+C\s*-", text, re.I):
        return "Dividends"
    if re.search(r"\bincome\s+tax\b|\btax\b|\bgst\b", text, re.I):
        return "Tax"
    if re.search(r"\bIB\s*BILLPAY\b|\bCHEQ\b|\bCRED\b", text, re.I):
        return "Bills & Utilities"
    if re.search(r"salary|payroll", text, re.I):
        return "Salary & Income"
    if re.search(r"zerodha|broking|mutual fund|\bipo\b|\bfd\b|fixed deposit", text, re.I):
        return "Investments"
    if re.search(r"\bemi\b|loan|finance", text, re.I):
        return "Loans & EMI"
    if re.search(r"\batm\b|cash withdrawal", text, re.I):
        return "Cash"
    if re.search(r"\bimps\b|\bneft\b|transfer", text, re.I):
        return "Transfers"
    derived = categorize(text)
    if derived != "Other":
        return derived
    if re.search(r"\bupi\b", text, re.I):
        return "Transfers"
    return "Other"


def _ensure_enrichment(conn: sqlite3.Connection) -> None:
    """Reapply derived bank fields when categorization rules change."""
    current = conn.execute(
        "SELECT value FROM app_metadata WHERE key='bank_enrichment_version'"
    ).fetchone()
    if current and current["value"] == _ENRICHMENT_VERSION:
        return
    rows = conn.execute("SELECT id, description FROM bank_transactions").fetchall()
    conn.executemany(
        "UPDATE bank_transactions SET counterparty=?, category=? WHERE id=?",
        [(_counterparty(row["description"]), _category(row["description"]), row["id"])
         for row in rows],
    )
    conn.execute(
        """INSERT INTO app_metadata (key,value) VALUES ('bank_enrichment_version',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (_ENRICHMENT_VERSION,),
    )
    conn.commit()


def find_statement(conn: sqlite3.Connection, stmt: BankStatement) -> Optional[dict]:
    row = conn.execute(
        """SELECT s.id, s.source_file, s.imported_at, a.display_name AS account
           FROM bank_statements s JOIN bank_accounts a ON a.id = s.account_id
           WHERE a.account_fingerprint = ? AND s.period_start = ? AND s.period_end = ?""",
        (stmt.account_fingerprint, stmt.period_start.isoformat(), stmt.period_end.isoformat()),
    ).fetchone()
    return dict(row) if row else None


def unseen_transactions(
    conn: sqlite3.Connection,
    account_id: int,
    transactions: list,
    exclude_statement_id: Optional[int] = None,
) -> list[bool]:
    """Which of these rows the account does not already hold.

    Statements overlap. A yearly download and the monthly e-statements for the
    months inside it describe the same transactions, and storing both counted
    every shared day twice — the only thing that ever stopped it was an exact
    match on the statement period.

    Matched on date, amount, direction and the running balance after the row,
    and *counted* rather than merely looked up: three identical-looking rows on
    one day are three transactions, so a statement repeating two of them adds
    one. A plain "does one exist" test would silently drop the third.

    Deliberately not matched on narration or reference. HDFC's mailed statement
    prints no reference column at all and words its narration differently from
    the downloaded one, so the same transaction reads differently in each and
    every overlapping row would look new. The running balance is what the two
    layouts agree on, and it is also what separates two payments of the same
    amount on the same day.

    ``exclude_statement_id`` drops the rows of a statement that is about to be
    replaced: re-importing a period rewrites those rows instead of adding to
    them, so they are not "already held".
    """
    if not transactions:
        return []
    dates = [row.date.isoformat() for row in transactions]
    # A date range, not a list of dates: an account holds years of rows and only
    # the days this statement touches can collide.
    query = """SELECT txn_date, amount, direction, balance FROM bank_transactions
               WHERE account_id = ? AND txn_date BETWEEN ? AND ?"""
    args: list[Any] = [account_id, min(dates), max(dates)]
    if exclude_statement_id is not None:
        query += " AND statement_id != ?"
        args.append(exclude_statement_id)
    held = Counter(
        (row["txn_date"], row["amount"], row["direction"], row["balance"])
        for row in conn.execute(query, args).fetchall()
    )
    unseen = []
    for row in transactions:
        key = (
            row.date.isoformat(), store.to_paise(row.amount),
            row.type.value, store.to_paise(row.balance),
        )
        if held[key]:
            held[key] -= 1
            unseen.append(False)
        else:
            unseen.append(True)
    return unseen


def import_preview(conn: sqlite3.Connection, stmt: BankStatement) -> dict:
    """How much of this statement the ledger already holds, storing nothing.

    Read before the import so the review screen can say what approving would
    actually do — add forty transactions, add twelve, or add none at all.
    """
    total = len(stmt.transactions)
    account = conn.execute(
        "SELECT id FROM bank_accounts WHERE account_fingerprint = ?",
        (stmt.account_fingerprint,),
    ).fetchone()
    if not account:
        return {"total": total, "known": 0, "new": total}
    replacing = find_statement(conn, stmt)
    new = sum(unseen_transactions(
        conn, int(account["id"]), stmt.transactions,
        exclude_statement_id=(replacing or {}).get("id"),
    ))
    return {"total": total, "known": total - new, "new": new}


def upsert_account(
    conn: sqlite3.Connection, stmt: BankStatement, member_id: Optional[int] = None
) -> int:
    row = conn.execute(
        "SELECT id FROM bank_accounts WHERE account_fingerprint = ?",
        (stmt.account_fingerprint,),
    ).fetchone()
    values = (
        stmt.bank_code, stmt.bank_name, stmt.masked_number, stmt.last4,
        stmt.account_holder, stmt.account_type, stmt.product, stmt.branch,
        display_name(stmt),
    )
    if row:
        conn.execute(
            """UPDATE bank_accounts SET bank_code=?, bank_name=?, masked_number=?, last4=?,
                      account_holder=?, account_type=?, product=?, branch=?, display_name=?
               WHERE id=?""",
            (*values, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        """INSERT INTO bank_accounts
           (bank_code, bank_name, account_fingerprint, masked_number, last4,
            account_holder, account_type, product, branch, display_name, created_at, member_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            stmt.bank_code, stmt.bank_name, stmt.account_fingerprint, stmt.masked_number,
            stmt.last4, stmt.account_holder, stmt.account_type, stmt.product, stmt.branch,
            display_name(stmt), dt.datetime.now().isoformat(timespec="seconds"), member_id,
        ),
    )
    return int(cur.lastrowid)


def import_statement(
    conn: sqlite3.Connection, stmt: BankStatement, member_id: Optional[int] = None
) -> tuple[int, int, bool, int, int]:
    """Insert/replace a bank statement.

    Returns (statement, rows stored, replaced, account, rows already held). Rows
    the account already holds are skipped rather than stored a second time — see
    :func:`unseen_transactions` — so statements that overlap union instead of
    double-counting the days they share.
    """
    _ensure_enrichment(conn)
    from . import categories as category_rules

    account_id = upsert_account(conn, stmt, member_id)
    rules = category_rules.rules_for(conn, category_rules.resolve(conn, member_id), "bank")
    key = (account_id, stmt.period_start.isoformat(), stmt.period_end.isoformat())
    existing = conn.execute(
        """SELECT id FROM bank_statements
           WHERE account_id = ? AND period_start = ? AND period_end = ?""",
        key,
    ).fetchone()
    replaced = existing is not None
    overrides: dict[tuple[Any, ...], str] = {}
    if existing:
        # Re-import replaces the statement rows. Carry explicit user choices
        # across that replacement using bank-ledger fields that are stable when
        # parser/enrichment rules change.
        old_rows = conn.execute(
            """SELECT txn_date, value_date, COALESCE(reference,'') reference,
                      amount, direction, balance, category_override
               FROM bank_transactions
               WHERE statement_id=? AND category_override IS NOT NULL""",
            (existing["id"],),
        ).fetchall()
        overrides = {
            (
                row["txn_date"], row["value_date"], row["reference"],
                row["amount"], row["direction"], row["balance"],
            ): row["category_override"]
            for row in old_rows
        }
        conn.execute("DELETE FROM bank_statements WHERE id = ?", (existing["id"],))

    # After the replacement delete above, so a re-import of the same period sees
    # its own previous rows as gone rather than as already held.
    incoming = [
        row for row, unseen in
        zip(stmt.transactions, unseen_transactions(conn, account_id, stmt.transactions))
        if unseen
    ]
    already_held = len(stmt.transactions) - len(incoming)

    # The period the statement covers, as printed — not as stored. A statement
    # whose rows were all held already still covered those dates.
    coverage_start = min((t.date for t in stmt.transactions), default=None)
    coverage_end = max((t.date for t in stmt.transactions), default=None)
    cur = conn.execute(
        """INSERT INTO bank_statements
           (account_id, source_file, parser_id, period_start, period_end,
            coverage_start, coverage_end, currency, opening_balance, closing_balance,
            confidence, checks_json, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id, stmt.source_file, stmt.parser_id,
            stmt.period_start.isoformat(), stmt.period_end.isoformat(),
            coverage_start.isoformat() if coverage_start else None,
            coverage_end.isoformat() if coverage_end else None,
            stmt.currency, store.to_paise(stmt.opening_balance), store.to_paise(stmt.closing_balance),
            stmt.confidence, json.dumps([check.model_dump() for check in stmt.checks]),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    statement_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT INTO bank_transactions
           (statement_id, account_id, txn_date, value_date, description, reference,
            counterparty, category, amount, direction, signed, balance, page, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                statement_id, account_id, row.date.isoformat(),
                row.value_date.isoformat() if row.value_date else None,
                row.description, row.reference, _counterparty(row.description),
                _category(row.description, rules), store.to_paise(row.amount), row.type.value,
                store.to_paise(row.signed), store.to_paise(row.balance), row.page, row.raw,
            )
            for row in incoming
        ],
    )
    if overrides:
        new_rows = conn.execute(
            """SELECT id, txn_date, value_date, COALESCE(reference,'') reference,
                      amount, direction, balance
               FROM bank_transactions WHERE statement_id=?""",
            (statement_id,),
        ).fetchall()
        conn.executemany(
            "UPDATE bank_transactions SET category_override=? WHERE id=?",
            [
                (override, row["id"])
                for row in new_rows
                if (override := overrides.get((
                    row["txn_date"], row["value_date"], row["reference"],
                    row["amount"], row["direction"], row["balance"],
                ))) is not None
            ],
        )
    conn.commit()
    return statement_id, len(incoming), replaced, account_id, already_held


def update_transaction_category(
    conn: sqlite3.Connection, transaction_id: int, category: Optional[str]
) -> dict:
    """Set or clear a manual bank-category override for one ledger row."""
    row = conn.execute(
        "SELECT id, category FROM bank_transactions WHERE id=?", (transaction_id,)
    ).fetchone()
    if not row:
        raise KeyError(f"bank transaction {transaction_id} was not found")
    override = " ".join((category or "").split()).strip() or None
    if override and len(override) > MAX_CATEGORY_LENGTH:
        raise ValueError(f"category must be at most {MAX_CATEGORY_LENGTH} characters")
    conn.execute(
        "UPDATE bank_transactions SET category_override=? WHERE id=?",
        (override, transaction_id),
    )
    conn.commit()
    return {
        "id": transaction_id,
        "category": override or row["category"] or "Other",
        "derived_category": row["category"] or "Other",
        "category_override": override,
        "category_is_override": override is not None,
    }


def update_transaction_categories(
    conn: sqlite3.Connection,
    transaction_ids: Iterable[int],
    category: Optional[str],
    account_ids: Iterable[int] = (),
) -> list[dict]:
    """Set or clear the manual override on many rows at once.

    ``account_ids`` restricts the update to accounts the caller may see, so a
    request cannot reach rows outside the signed-in user's members.
    """
    ids = sorted({int(value) for value in transaction_ids})
    if not ids:
        return []
    override = " ".join((category or "").split()).strip() or None
    if override and len(override) > MAX_CATEGORY_LENGTH:
        raise ValueError(f"category must be at most {MAX_CATEGORY_LENGTH} characters")

    allowed = list(account_ids)
    scope = f" AND account_id IN ({','.join('?' * len(allowed))})" if allowed else ""
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, category FROM bank_transactions WHERE id IN ({marks}){scope}",
        [*ids, *allowed],
    ).fetchall()
    if not rows:
        return []
    found = [int(row["id"]) for row in rows]
    conn.execute(
        f"UPDATE bank_transactions SET category_override=? WHERE id IN ({','.join('?' * len(found))})",
        [override, *found],
    )
    conn.commit()
    return [
        {
            "id": int(row["id"]),
            "category": override or row["category"] or "Other",
            "derived_category": row["category"] or "Other",
            "category_override": override,
            "category_is_override": override is not None,
        }
        for row in rows
    ]


#: The one category a row counts under — see the card ledger's twin constant.
CATEGORY_SQL = "COALESCE(NULLIF(TRIM(t.category_override),''), t.category, 'Other')"


def _where(
    account_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause.

    ``categories`` of ``None`` means no category filter. An empty sequence is
    different, and deliberate: the user unticked every category, so nothing
    should match.
    """
    ids = list(account_ids)
    clauses = ["1=1"]
    args: list[Any] = []
    if ids:
        clauses.append(f"t.account_id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    if date_from:
        clauses.append("t.txn_date >= ?")
        args.append(date_from)
    if date_to:
        clauses.append("t.txn_date <= ?")
        args.append(date_to)
    if categories is not None:
        names = list(categories)
        clauses.append(
            f"{CATEGORY_SQL} IN ({','.join('?' * len(names))})" if names else "0=1"
        )
        args.extend(names)
    return " AND ".join(clauses), args


def category_facets(conn: sqlite3.Connection, account_ids: Iterable[int] = ()) -> list[dict]:
    """Every category present on the given accounts, with its row count.

    The category filter needs a list that does not shrink as it is applied, so
    this is scoped by account only — never by the filter it feeds.
    """
    _ensure_enrichment(conn)
    clause, args = _where(account_ids)
    rows = conn.execute(
        f"""SELECT {CATEGORY_SQL} AS name, COUNT(*) AS n
            FROM bank_transactions t WHERE {clause}
            GROUP BY name ORDER BY name COLLATE NOCASE""",
        args,
    ).fetchall()
    return [{"name": row["name"], "n": row["n"]} for row in rows]


def date_bounds(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(txn_date) a, MAX(txn_date) b FROM bank_transactions").fetchone()
    return {"min": row["a"], "max": row["b"]}


def statements(conn: sqlite3.Connection) -> list[dict]:
    _ensure_enrichment(conn)
    rows = conn.execute(
        """SELECT s.*, a.display_name AS account FROM bank_statements s
           JOIN bank_accounts a ON a.id = s.account_id
           ORDER BY s.period_end DESC"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("opening_balance", "closing_balance"):
            item[key] = store.to_rupees(item[key])
        item["checks"] = json.loads(item.pop("checks_json") or "[]")
        result.append(item)
    return result


def accounts(conn: sqlite3.Connection, member_ids: Iterable[int] = ()) -> list[dict]:
    _ensure_enrichment(conn)
    ids = list(member_ids)
    where = f"WHERE a.member_id IN ({','.join('?' * len(ids))})" if ids else ""
    rows = conn.execute(
        """SELECT a.*, m.name AS member_name,
                  COUNT(DISTINCT s.id) AS statements,
                  COUNT(t.id) AS txn_count,
                  MIN(t.txn_date) AS first_txn,
                  MAX(t.txn_date) AS last_txn
           FROM bank_accounts a LEFT JOIN members m ON m.id=a.member_id
           LEFT JOIN bank_statements s ON s.account_id = a.id
           LEFT JOIN bank_transactions t ON t.statement_id = s.id
           """ + where + " GROUP BY a.id ORDER BY a.display_name",
        ids,
    ).fetchall()
    history_rows = conn.execute(
        """SELECT s.*,
                  COUNT(t.id) AS txns,
                  COALESCE(SUM(CASE WHEN t.direction='debit' THEN t.amount ELSE 0 END),0) withdrawals,
                  COALESCE(SUM(CASE WHEN t.direction='credit' THEN t.amount ELSE 0 END),0) deposits
           FROM bank_statements s LEFT JOIN bank_transactions t ON t.statement_id = s.id
           GROUP BY s.id ORDER BY s.account_id, s.period_end DESC"""
    ).fetchall()
    history: dict[int, list[dict]] = {}
    for row in history_rows:
        item = dict(row)
        item["opening_balance"] = store.to_rupees(item["opening_balance"])
        item["closing_balance"] = store.to_rupees(item["closing_balance"])
        item["withdrawals"] = store.to_rupees(item["withdrawals"])
        item["deposits"] = store.to_rupees(item["deposits"])
        item["checks"] = json.loads(item.pop("checks_json") or "[]")
        history.setdefault(item.pop("account_id"), []).append(item)
    result = []
    for row in rows:
        item = dict(row)
        item.pop("account_fingerprint", None)
        item["sender_ids"] = json.loads(item.pop("sender_ids_json", None) or "[]")
        item["subject_patterns"] = json.loads(item.pop("subject_patterns_json", None) or "[]")
        item["history"] = history.get(item["id"], [])
        result.append(item)
    return result


def transactions(
    conn: sqlite3.Connection,
    account_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 5000,
    categories: Optional[Iterable[str]] = None,
) -> list[dict]:
    _ensure_enrichment(conn)
    clause, args = _where(account_ids, date_from, date_to, categories)
    rows = conn.execute(
        f"""SELECT t.id, t.txn_date, t.value_date, t.description, t.reference,
                   t.counterparty, t.category AS derived_category,
                   t.category_override,
                   COALESCE(NULLIF(TRIM(t.category_override),''), t.category, 'Other') AS category,
                   t.amount, t.direction, t.signed,
                   t.balance, t.page, t.account_id, a.display_name AS account,
                   a.member_id, m.name AS member_name,
                   s.period_start AS statement_period_start,
                   s.period_end AS statement_period_end
            FROM bank_transactions t JOIN bank_accounts a ON a.id=t.account_id
            LEFT JOIN members m ON m.id=a.member_id
            JOIN bank_statements s ON s.id=t.statement_id
            WHERE {clause} ORDER BY t.txn_date DESC, t.id DESC LIMIT ?""",
        args + [limit],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["category_is_override"] = item["category_override"] is not None
        for key in ("amount", "signed", "balance"):
            item[key] = store.to_rupees(item[key])
        result.append(item)
    return result


def analytics(
    conn: sqlite3.Connection,
    account_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
) -> dict:
    _ensure_enrichment(conn)
    clause, args = _where(account_ids, date_from, date_to, categories)
    # Balances are a property of the account, not of a category: a running
    # balance read from a subset of the rows is meaningless. The opening and
    # closing figures therefore ignore the category filter.
    ledger_clause, ledger_args = _where(account_ids, date_from, date_to)
    totals = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END),0) withdrawals,
              COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) deposits,
              COALESCE(SUM(CASE WHEN direction='debit' THEN 1 ELSE 0 END),0) debit_count,
              COUNT(*) txn_count,
              COALESCE(MAX(CASE WHEN direction='debit' THEN amount ELSE 0 END),0) largest
            FROM bank_transactions t WHERE {clause}""",
        args,
    ).fetchone()
    monthly = conn.execute(
        f"""SELECT substr(txn_date,1,7) month,
                   SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) withdrawals,
                   SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) deposits
            FROM bank_transactions t WHERE {clause} GROUP BY month ORDER BY month""",
        args,
    ).fetchall()
    categories = conn.execute(
        f"""SELECT COALESCE(NULLIF(TRIM(category_override),''), category, 'Other') label,
                   SUM(amount) total, COUNT(*) n
            FROM bank_transactions t WHERE {clause} AND direction='debit'
            GROUP BY label ORDER BY total DESC""",
        args,
    ).fetchall()
    deposit_categories = conn.execute(
        f"""SELECT COALESCE(NULLIF(TRIM(category_override),''), category, 'Other') label,
                   SUM(amount) total, COUNT(*) n
            FROM bank_transactions t WHERE {clause} AND direction='credit'
            GROUP BY label ORDER BY total DESC""",
        args,
    ).fetchall()
    # Net is deposits minus withdrawals per category, so a category can land on
    # either side of zero. Ordered by magnitude rather than value: the biggest
    # drains matter as much as the biggest earners, and a plain DESC would bury
    # them at the bottom of the card.
    net_categories = conn.execute(
        f"""SELECT COALESCE(NULLIF(TRIM(category_override),''), category, 'Other') label,
                   SUM(signed) total, COUNT(*) n
            FROM bank_transactions t WHERE {clause}
            GROUP BY label ORDER BY ABS(SUM(signed)) DESC""",
        args,
    ).fetchall()
    by_account = conn.execute(
        f"""SELECT t.account_id, a.display_name label, SUM(t.amount) total, COUNT(*) n
            FROM bank_transactions t JOIN bank_accounts a ON a.id=t.account_id
            WHERE {clause} AND t.direction='debit'
            GROUP BY t.account_id ORDER BY total DESC""",
        args,
    ).fetchall()
    by_member = conn.execute(
        f"""SELECT m.id AS member_id, COALESCE(m.name,'Unassigned') AS member,
                   SUM(CASE WHEN t.direction='debit' THEN t.amount ELSE 0 END) withdrawals,
                   SUM(CASE WHEN t.direction='credit' THEN t.amount ELSE 0 END) deposits,
                   COUNT(*) n
            FROM bank_transactions t JOIN bank_accounts a ON a.id=t.account_id
            LEFT JOIN members m ON m.id=a.member_id
            WHERE {clause} GROUP BY a.member_id ORDER BY withdrawals DESC""",
        args,
    ).fetchall()
    counterparties = conn.execute(
        f"""SELECT counterparty label, SUM(amount) total, COUNT(*) n
            FROM bank_transactions t WHERE {clause} AND direction='debit'
            GROUP BY counterparty ORDER BY total DESC LIMIT 12""",
        args,
    ).fetchall()
    ledger = conn.execute(
        f"""SELECT account_id, amount, direction, balance
            FROM bank_transactions t WHERE {ledger_clause} ORDER BY txn_date, id""",
        ledger_args,
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
    withdrawals = totals["withdrawals"]
    deposits = totals["deposits"]
    debit_count = totals["debit_count"]
    return {
        "totals": {
            "withdrawals": store.to_rupees(withdrawals),
            "deposits": store.to_rupees(deposits),
            "net": store.to_rupees(deposits - withdrawals),
            "txn_count": totals["txn_count"],
            "avg_debit": store.to_rupees(withdrawals // debit_count) if debit_count else 0.0,
            "largest_debit": store.to_rupees(totals["largest"]),
            "opening_balance": store.to_rupees(opening),
            "closing_balance": store.to_rupees(closing),
        },
        "monthly": [
            {"month": row["month"], "withdrawals": store.to_rupees(row["withdrawals"]),
             "deposits": store.to_rupees(row["deposits"])}
            for row in monthly
        ],
        "by_category": [
            {"label": row["label"], "value": store.to_rupees(row["total"]), "n": row["n"]}
            for row in categories
        ],
        "deposits_by_category": [
            {"label": row["label"], "value": store.to_rupees(row["total"]), "n": row["n"]}
            for row in deposit_categories
        ],
        "net_by_category": [
            {"label": row["label"], "value": store.to_rupees(row["total"]), "n": row["n"]}
            for row in net_categories
        ],
        "by_account": [
            {"account_id": row["account_id"], "label": row["label"],
             "value": store.to_rupees(row["total"]), "n": row["n"]}
            for row in by_account
        ],
        "by_member": [
            {
                "member_id": row["member_id"], "member": row["member"],
                "withdrawals": store.to_rupees(row["withdrawals"]),
                "deposits": store.to_rupees(row["deposits"]), "n": row["n"],
            }
            for row in by_member
        ],
        "top_counterparties": [
            {"label": row["label"] or "Unknown", "value": store.to_rupees(row["total"]),
             "n": row["n"]}
            for row in counterparties
        ],
    }
