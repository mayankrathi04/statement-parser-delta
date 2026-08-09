"""SQLite store — the canonical home for every card's history.

Money is stored as **integer paise**, never float. SQLite has no decimal type, and
a REAL column silently turns ₹1,234.05 + ₹2,345.10 into 3579.1499999999996; summed
across a few thousand rows that drifts off the reconciliations the parser works so
hard to prove. Conversion happens at the boundary, so every aggregate is exact
integer arithmetic.

Re-importing is idempotent. A statement is identified by (card, statement date,
period start), so re-running the importer over the same folder — or re-fetching a
corrected copy of a statement — replaces that statement's rows rather than
doubling them.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from .enrich import categorize, merchant_name
from .schema import Statement, TxnType

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY,
    issuer        TEXT NOT NULL,
    product       TEXT,
    masked_number TEXT NOT NULL UNIQUE,
    last4         TEXT,
    display_name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    id               INTEGER PRIMARY KEY,
    card_id          INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    source_file      TEXT,
    template_id      TEXT,
    statement_date   TEXT,
    period_start     TEXT,
    period_end       TEXT,
    currency         TEXT DEFAULT 'INR',
    previous_dues    INTEGER,
    payments_credits INTEGER,
    purchases_debits INTEGER,
    finance_charges  INTEGER,
    total_dues       INTEGER,
    minimum_due      INTEGER,
    due_date         TEXT,
    credit_limit     INTEGER,
    available_credit INTEGER,
    confidence       REAL,
    checks_json      TEXT,
    imported_at      TEXT,
    UNIQUE (card_id, statement_date, period_start)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    statement_id  INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    txn_date      TEXT NOT NULL,
    txn_time      TEXT,
    description   TEXT NOT NULL,
    merchant      TEXT,
    category      TEXT,
    amount        INTEGER NOT NULL,   -- paise, always positive
    direction     TEXT NOT NULL,      -- debit | credit
    signed        INTEGER NOT NULL,   -- paise, debit positive
    section       TEXT,
    is_emi        INTEGER DEFAULT 0,
    reward_points INTEGER,
    fcy_currency  TEXT,
    fcy_amount    INTEGER,
    page          INTEGER,
    raw           TEXT
);

-- Ingest history: every PDF's journey through the pipeline is kept, so the
-- dashboard can show what happened to a statement long after the run finished.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,            -- fetch | import
    status      TEXT NOT NULL,            -- running | done | failed
    started_at  TEXT, finished_at TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS ingest_files (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL,           -- running | ok | failed | skipped
    issuer TEXT, product TEXT, card TEXT, template_id TEXT,
    encrypted    INTEGER DEFAULT 0,
    txn_count    INTEGER,
    confidence   REAL,
    checks_json  TEXT,
    error        TEXT,
    started_at   TEXT, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_steps (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES ingest_files(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    name     TEXT NOT NULL,
    status   TEXT NOT NULL,               -- running | ok | failed | skipped
    detail   TEXT,
    ms       INTEGER
);

CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_card ON transactions(card_id);
CREATE INDEX IF NOT EXISTS ix_txn_cat  ON transactions(category);
"""

DEFAULT_DB = Path("statements.db")


def to_paise(value: Optional[Decimal]) -> Optional[int]:
    return None if value is None else int((value * 100).to_integral_value())


def to_rupees(paise: Optional[int]) -> float:
    return 0.0 if paise is None else round(paise / 100, 2)


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so they are applied against the live table list on every connect.
_MIGRATIONS = {
    "ingest_files": {
        "path": "TEXT",
        "statement_date": "TEXT",
        "period_start": "TEXT",
        "period_end": "TEXT",
        "duplicate_of": "INTEGER",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # Identity moved from (card, statement date, period start) to the billing
    # cycle. The old key included a field the parser can start extracting later,
    # and when it did, the same statement no longer matched itself and was stored
    # twice. The cycle is what a statement actually *is*, so it is stable against
    # parser improvements. Collapse any duplicates the old key let through,
    # keeping the most recent import of each cycle.
    conn.execute(
        """DELETE FROM statements WHERE id NOT IN (
             SELECT MAX(id) FROM statements
             GROUP BY card_id, COALESCE(period_start,''), COALESCE(period_end,'')
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_statement_cycle
           ON statements (card_id, period_start, period_end)"""
    )
    conn.commit()


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def find_statement(conn: sqlite3.Connection, stmt: Statement) -> Optional[dict]:
    """Has this exact statement already been imported?

    Matched on the same key the importer uses, so the answer shown in the review
    list is precisely what will happen on import: a hit means replace, not add.
    """
    masked = stmt.account_masked or f"{stmt.issuer}-unknown"
    row = conn.execute(
        """SELECT s.id, s.source_file, s.imported_at, c.display_name AS card
           FROM statements s JOIN cards c ON c.id = s.card_id
           WHERE c.masked_number = ? AND s.period_start IS ? AND s.period_end IS ?""",
        (masked, _d(stmt.period_start), _d(stmt.period_end)),
    ).fetchone()
    return dict(row) if row else None


def _card_display(stmt: Statement) -> str:
    last4 = (stmt.account_masked or "")[-4:]
    product = (stmt.product or "").strip()
    # Issuers repeat their own name inside the product string ("Tata Neu Infinity
    # HDFC Bank"); printing it twice makes the filter list unreadable.
    if product.lower().endswith(stmt.issuer.lower()):
        product = product[: -len(stmt.issuer)].strip()
    label = f"{stmt.issuer} {product}".strip()
    return f"{label} ••{last4}" if last4 else label


def upsert_card(conn: sqlite3.Connection, stmt: Statement) -> int:
    masked = stmt.account_masked or f"{stmt.issuer}-unknown"
    row = conn.execute("SELECT id FROM cards WHERE masked_number = ?", (masked,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cards (issuer, product, masked_number, last4, display_name)"
        " VALUES (?, ?, ?, ?, ?)",
        (stmt.issuer, stmt.product, masked, masked[-4:], _card_display(stmt)),
    )
    return int(cur.lastrowid)


def import_statement(conn: sqlite3.Connection, stmt: Statement) -> tuple[int, int, bool]:
    """Insert or replace one statement. Returns (statement_id, rows, replaced)."""
    card_id = upsert_card(conn, stmt)
    # Keyed on the billing cycle — see _migrate for why not the statement date.
    key = (card_id, _d(stmt.period_start), _d(stmt.period_end))

    existing = conn.execute(
        "SELECT id FROM statements WHERE card_id IS ? AND period_start IS ? AND period_end IS ?",
        key,
    ).fetchone()
    replaced = existing is not None
    if replaced:
        conn.execute("DELETE FROM statements WHERE id = ?", (existing["id"],))

    s = stmt.summary
    cur = conn.execute(
        """INSERT INTO statements
           (card_id, source_file, template_id, statement_date, period_start, period_end,
            currency, previous_dues, payments_credits, purchases_debits, finance_charges,
            total_dues, minimum_due, due_date, credit_limit, available_credit,
            confidence, checks_json, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            card_id, stmt.source_file, stmt.template_id, _d(stmt.statement_date),
            _d(stmt.period_start), _d(stmt.period_end), stmt.currency,
            to_paise(s.previous_dues), to_paise(s.payments_credits),
            to_paise(s.purchases_debits), to_paise(s.finance_charges),
            to_paise(s.total_dues), to_paise(s.minimum_due), _d(s.due_date),
            to_paise(s.credit_limit), to_paise(s.available_credit),
            stmt.confidence, json.dumps([c.model_dump() for c in stmt.checks]),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    stmt_id = int(cur.lastrowid)

    conn.executemany(
        """INSERT INTO transactions
           (statement_id, card_id, txn_date, txn_time, description, merchant, category,
            amount, direction, signed, section, is_emi, reward_points,
            fcy_currency, fcy_amount, page, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                stmt_id, card_id, t.date.isoformat(),
                t.time.strftime("%H:%M") if t.time else None,
                t.description, merchant_name(t.description),
                categorize(t.description, t.category),
                to_paise(t.amount), t.type.value, to_paise(t.signed), t.section,
                1 if t.is_emi else 0, t.reward_points, t.fcy_currency,
                to_paise(t.fcy_amount), t.page, t.raw,
            )
            for t in stmt.transactions
        ],
    )
    conn.commit()
    return stmt_id, len(stmt.transactions), replaced


def _d(value) -> Optional[str]:
    return value.isoformat() if isinstance(value, dt.date) else None


# ------------------------------------------------------------------ queries

def cards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT c.id, c.issuer, c.product, c.masked_number, c.last4, c.display_name,
                  COUNT(t.id) AS txn_count,
                  MIN(t.txn_date) AS first_txn,
                  MAX(t.txn_date) AS last_txn
           FROM cards c LEFT JOIN transactions t ON t.card_id = c.id
           GROUP BY c.id ORDER BY c.display_name"""
    ).fetchall()
    return [dict(r) for r in rows]


def date_bounds(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(txn_date) a, MAX(txn_date) b FROM transactions").fetchone()
    return {"min": row["a"], "max": row["b"]}


def _filters(card_ids, date_from, date_to, extra_sql="", params=None):
    where, args = ["1=1"], list(params or [])
    if card_ids:
        where.append(f"t.card_id IN ({','.join('?' * len(card_ids))})")
        args += list(card_ids)
    if date_from:
        where.append("t.txn_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("t.txn_date <= ?")
        args.append(date_to)
    if extra_sql:
        where.append(extra_sql)
    return " AND ".join(where), args


def transactions(
    conn: sqlite3.Connection,
    card_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 5000,
) -> list[dict]:
    clause, args = _filters(list(card_ids), date_from, date_to)
    rows = conn.execute(
        f"""SELECT t.id, t.txn_date, t.txn_time, t.description, t.merchant, t.category,
                   t.amount, t.direction, t.signed, t.is_emi, t.reward_points,
                   t.fcy_currency, t.fcy_amount, c.display_name AS card, t.card_id
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE {clause}
            ORDER BY t.txn_date DESC, t.id DESC LIMIT ?""",
        args + [limit],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = to_rupees(d["amount"])
        d["signed"] = to_rupees(d["signed"])
        d["fcy_amount"] = to_rupees(d["fcy_amount"]) if d["fcy_amount"] else None
        d["is_emi"] = bool(d["is_emi"])
        out.append(d)
    return out


def analytics(
    conn: sqlite3.Connection,
    card_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """Every aggregate the dashboard needs, in exact integer paise."""
    ids = list(card_ids)
    clause, args = _filters(ids, date_from, date_to)

    totals = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0) AS spend,
              COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS payments,
              COUNT(*) AS txn_count,
              COALESCE(SUM(CASE WHEN direction='debit'  THEN 1 END),0) AS debit_count,
              COALESCE(MAX(CASE WHEN direction='debit'  THEN amount END),0) AS largest
            FROM transactions t WHERE {clause}""",
        args,
    ).fetchone()

    spend, debit_count = totals["spend"], totals["debit_count"]

    monthly = conn.execute(
        f"""SELECT substr(t.txn_date,1,7) AS month,
                   COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0) AS spend,
                   COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS payments
            FROM transactions t WHERE {clause}
            GROUP BY month ORDER BY month""",
        args,
    ).fetchall()

    by_category = conn.execute(
        f"""SELECT category, SUM(amount) AS total, COUNT(*) AS n
            FROM transactions t WHERE {clause} AND direction='debit'
            GROUP BY category ORDER BY total DESC""",
        args,
    ).fetchall()

    by_card = conn.execute(
        f"""SELECT c.id AS card_id, c.display_name AS card, SUM(t.amount) AS total, COUNT(*) AS n
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE {clause} AND t.direction='debit'
            GROUP BY c.id ORDER BY total DESC""",
        args,
    ).fetchall()

    merchants = conn.execute(
        f"""SELECT merchant, SUM(amount) AS total, COUNT(*) AS n
            FROM transactions t WHERE {clause} AND direction='debit'
            GROUP BY merchant ORDER BY total DESC LIMIT 12""",
        args,
    ).fetchall()

    # Reward points, EMIs and foreign-currency legs are parsed per row but mean
    # nothing inside a spend total — they are separate ledgers and are reported
    # as such rather than folded into the rupee figures.
    rewards_by_card = conn.execute(
        f"""SELECT c.id AS card_id, c.display_name AS card,
                   COALESCE(SUM(t.reward_points),0) AS pts, COUNT(t.reward_points) AS n
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE {clause} AND t.reward_points IS NOT NULL
            GROUP BY c.id ORDER BY pts DESC""",
        args,
    ).fetchall()
    rewards_by_month = conn.execute(
        f"""SELECT substr(t.txn_date,1,7) AS month, COALESCE(SUM(t.reward_points),0) AS pts
            FROM transactions t WHERE {clause} AND t.reward_points IS NOT NULL
            GROUP BY month ORDER BY month""",
        args,
    ).fetchall()

    emi_rows = conn.execute(
        f"""SELECT t.txn_date, t.description, t.merchant, t.amount, c.display_name AS card, t.card_id
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE {clause} AND t.is_emi = 1 ORDER BY t.txn_date DESC""",
        args,
    ).fetchall()

    fcy_rows = conn.execute(
        f"""SELECT t.txn_date, t.description, t.merchant, t.fcy_currency, t.fcy_amount,
                   t.amount, c.display_name AS card, t.card_id
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE {clause} AND t.fcy_currency IS NOT NULL ORDER BY t.txn_date DESC""",
        args,
    ).fetchall()

    return {
        "rewards": {
            "total_points": sum(r["pts"] for r in rewards_by_card),
            "earning_txns": sum(r["n"] for r in rewards_by_card),
            "by_card": [
                {"card_id": r["card_id"], "label": r["card"], "points": r["pts"], "n": r["n"]}
                for r in rewards_by_card
            ],
            "by_month": [{"month": r["month"], "points": r["pts"]} for r in rewards_by_month],
        },
        "emi": {
            "count": len(emi_rows),
            "total": to_rupees(sum(r["amount"] for r in emi_rows)),
            "rows": [
                {
                    "txn_date": r["txn_date"], "description": r["description"],
                    "merchant": r["merchant"], "amount": to_rupees(r["amount"]),
                    "card": r["card"], "card_id": r["card_id"],
                }
                for r in emi_rows
            ],
        },
        "fcy": {
            "count": len(fcy_rows),
            "total_inr": to_rupees(sum(r["amount"] for r in fcy_rows)),
            "rows": [
                {
                    "txn_date": r["txn_date"], "description": r["description"],
                    "merchant": r["merchant"], "currency": r["fcy_currency"],
                    "fcy_amount": to_rupees(r["fcy_amount"]), "amount": to_rupees(r["amount"]),
                    "card": r["card"], "card_id": r["card_id"],
                }
                for r in fcy_rows
            ],
        },
        "totals": {
            "spend": to_rupees(spend),
            "payments": to_rupees(totals["payments"]),
            "net": to_rupees(spend - totals["payments"]),
            "txn_count": totals["txn_count"],
            "avg_txn": to_rupees(spend // debit_count) if debit_count else 0.0,
            "largest": to_rupees(totals["largest"]),
            "months": len(monthly),
            "avg_month": to_rupees(spend // len(monthly)) if monthly else 0.0,
        },
        "monthly": [
            {"month": r["month"], "spend": to_rupees(r["spend"]), "payments": to_rupees(r["payments"])}
            for r in monthly
        ],
        "by_category": [
            {"label": r["category"], "value": to_rupees(r["total"]), "n": r["n"]} for r in by_category
        ],
        "by_card": [
            {"card_id": r["card_id"], "label": r["card"], "value": to_rupees(r["total"]), "n": r["n"]}
            for r in by_card
        ],
        "top_merchants": [
            {"label": r["merchant"], "value": to_rupees(r["total"]), "n": r["n"]} for r in merchants
        ],
    }


def card_history(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Per card, the months already parsed and stored, newest first.

    Months with no statement are simply absent — a gap in this list is a month
    that was never imported, which is exactly the thing worth seeing.
    """
    rows = conn.execute(
        """SELECT s.card_id, s.id, s.source_file, s.statement_date, s.period_start,
                  s.period_end, s.total_dues, s.confidence, s.imported_at,
                  (SELECT COUNT(*) FROM transactions t WHERE t.statement_id = s.id) AS txns,
                  (SELECT COALESCE(SUM(t.amount),0) FROM transactions t
                     WHERE t.statement_id = s.id AND t.direction='debit') AS spend
           FROM statements s
           ORDER BY s.card_id, COALESCE(s.statement_date, s.period_end) DESC"""
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        anchor = d["statement_date"] or d["period_end"] or ""
        d["month"] = anchor[:7]
        d["total_dues"] = to_rupees(d["total_dues"])
        d["spend"] = to_rupees(d["spend"])
        out.setdefault(d.pop("card_id"), []).append(d)
    return out


def statements(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, c.display_name AS card FROM statements s
           JOIN cards c ON c.id = s.card_id
           ORDER BY s.statement_date DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("previous_dues", "payments_credits", "purchases_debits", "finance_charges",
                  "total_dues", "minimum_due", "credit_limit", "available_credit"):
            d[k] = to_rupees(d[k])
        d["checks"] = json.loads(d.pop("checks_json") or "[]")
        out.append(d)
    return out
