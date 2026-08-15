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
import re
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from .emi import EmiEvidence, classify_emi_rows
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
    spend_effect  INTEGER NOT NULL DEFAULT 0,
    payment_effect INTEGER NOT NULL DEFAULT 0,
    reward_points INTEGER,
    fcy_currency  TEXT,
    fcy_amount    INTEGER,
    page          INTEGER,
    raw           TEXT
);

-- Deposit accounts are intentionally separate from cards. Their ledger sign,
-- running balance, value date and reference number have different semantics,
-- and keeping separate foreign keys prevents bank transfers from leaking into
-- card-spend analytics.
CREATE TABLE IF NOT EXISTS bank_accounts (
    id                  INTEGER PRIMARY KEY,
    bank_code           TEXT NOT NULL,
    bank_name           TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL UNIQUE,
    masked_number       TEXT NOT NULL,
    last4               TEXT NOT NULL,
    account_holder      TEXT,
    account_type        TEXT,
    product             TEXT,
    branch              TEXT,
    display_name        TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_statements (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    source_file       TEXT,
    parser_id         TEXT NOT NULL,
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    coverage_start    TEXT,
    coverage_end      TEXT,
    currency          TEXT DEFAULT 'INR',
    opening_balance   INTEGER,
    closing_balance   INTEGER,
    confidence        REAL,
    checks_json       TEXT,
    imported_at       TEXT NOT NULL,
    UNIQUE (account_id, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id            INTEGER PRIMARY KEY,
    statement_id  INTEGER NOT NULL REFERENCES bank_statements(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    txn_date      TEXT NOT NULL,
    value_date    TEXT,
    description   TEXT NOT NULL,
    reference     TEXT,
    counterparty  TEXT,
    category      TEXT,
    category_override TEXT,
    amount        INTEGER NOT NULL,  -- paise, always positive
    direction     TEXT NOT NULL,     -- debit | credit
    signed        INTEGER NOT NULL,  -- paise, credit positive
    balance       INTEGER NOT NULL,  -- paise after this transaction
    page          INTEGER,
    raw           TEXT
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS ix_bank_txn_date ON bank_transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_bank_txn_account ON bank_transactions(account_id);
CREATE INDEX IF NOT EXISTS ix_bank_txn_cat ON bank_transactions(category);
"""

DEFAULT_DB = Path("data/statements.db")
LEGACY_DEFAULT_DB = Path("statements.db")


def to_paise(value: Optional[Decimal]) -> Optional[int]:
    return None if value is None else int((value * 100).to_integral_value())


def to_rupees(paise: Optional[int]) -> float:
    return 0.0 if paise is None else round(paise / 100, 2)


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so they are applied against the live table list on every connect.
_MIGRATIONS = {
    "cards": {
        "sender_ids_json": "TEXT DEFAULT '[]'",
        "subject_patterns_json": "TEXT DEFAULT '[]'",
        "member_id": "INTEGER",
    },
    "bank_accounts": {"member_id": "INTEGER"},
    "ingest_files": {
        "path": "TEXT",
        "statement_date": "TEXT",
        "period_start": "TEXT",
        "period_end": "TEXT",
        "duplicate_of": "INTEGER",
        "document_type": "TEXT DEFAULT 'credit_card'",
        "bank_account_id": "INTEGER",
        # Which member this file was uploaded/fetched for. Recorded at scan time
        # so approving weeks later still attributes it to the intended member
        # rather than whoever happens to be the default.
        "member_id": "INTEGER",
    },
    "transactions": {
        "spend_effect": "INTEGER NOT NULL DEFAULT 0",
        "payment_effect": "INTEGER NOT NULL DEFAULT 0",
        # Manual category, kept separate from the derived one so re-applying the
        # rules can never overwrite a decision the user made by hand.
        "category_override": "TEXT",
        # Exactly what the issuer printed, before any mapping. Kept so the
        # Categories screen can list provider labels, and so re-applying rules can
        # fall back to the issuer instead of losing the information.
        "issuer_category": "TEXT",
    },
    "bank_transactions": {
        # Keep user corrections separate from parser-derived enrichment so a
        # future categorization refresh cannot silently overwrite them.
        "category_override": "TEXT",
    },
}


_PAYMENT_CREDIT = re.compile(
    r"(?:\bBBPS\b.*\bPAYMENT\b|\bBPPY\s+CC\s+PAYMENT\b|"
    r"\bPAYMENT\b.*\b(?:RECEIVED|BBPS)\b|\bCREDIT\s+CARD\s+PAYMENT|"
    r"\b(?:TELE|NETBANKING|NEFT|IMPS)\s+TRANSFER\b|\bIMPS\s+PMT\b)",
    re.I,
)


def _is_card_payment(description: str) -> bool:
    return bool(_PAYMENT_CREDIT.search(description or ""))


def _refresh_emi_flags(conn: sqlite3.Connection, card_id: Optional[int] = None) -> None:
    """Rebuild EMI flags and purchase-based ledger effects from full history."""
    where = "WHERE card_id = ?" if card_id is not None else ""
    args = (card_id,) if card_id is not None else ()
    rows = conn.execute(
        f"""SELECT id, card_id, txn_date, description, amount, direction
            FROM transactions {where} ORDER BY card_id, txn_date, id""",
        args,
    ).fetchall()

    by_card: dict[int, list[EmiEvidence]] = {}
    for row in rows:
        by_card.setdefault(row["card_id"], []).append(
            EmiEvidence(
                key=row["id"],
                date=dt.date.fromisoformat(row["txn_date"]),
                description=row["description"],
                amount=row["amount"],
                direction=row["direction"],
            )
        )

    if card_id is None:
        conn.execute("UPDATE transactions SET is_emi = 0")
    else:
        conn.execute("UPDATE transactions SET is_emi = 0 WHERE card_id = ?", (card_id,))

    classifications = {
        card: classify_emi_rows(evidence) for card, evidence in by_card.items()
    }
    converted = {
        key for classification in classifications.values()
        for key in classification.active_purchases
    }
    conn.executemany(
        "UPDATE transactions SET is_emi = 1 WHERE id = ?",
        [(key,) for key in converted],
    )

    excluded = {
        key for classification in classifications.values()
        for key in classification.excluded_from_spend
    }
    effects = []
    for row in rows:
        payment = (
            row["amount"]
            if row["direction"] == "credit" and _is_card_payment(row["description"])
            else 0
        )
        if row["id"] in excluded:
            spend = 0
            payment = 0
        elif row["direction"] == "debit":
            spend = row["amount"]
        elif payment:
            spend = 0
        else:
            spend = -row["amount"]
        effects.append((spend, payment, row["id"]))
    conn.executemany(
        "UPDATE transactions SET spend_effect = ?, payment_effect = ? WHERE id = ?",
        effects,
    )


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

    emi_version = conn.execute(
        "SELECT value FROM app_metadata WHERE key = 'emi_classification_version'"
    ).fetchone()
    if not emi_version or emi_version["value"] != "5":
        _refresh_emi_flags(conn)
        conn.execute(
            """INSERT INTO app_metadata (key, value) VALUES ('emi_classification_version', '5')
               ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
        )
    conn.commit()


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    # Keep the default database out of the project root.  Existing installs are
    # migrated once, including SQLite's WAL sidecars when they are present.
    if path == DEFAULT_DB and not path.exists() and LEGACY_DEFAULT_DB.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_DEFAULT_DB), str(path))
        for suffix in ("-wal", "-shm"):
            legacy_sidecar = Path(f"{LEGACY_DEFAULT_DB}{suffix}")
            if legacy_sidecar.exists():
                shutil.move(str(legacy_sidecar), f"{path}{suffix}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Identity tables live beside the financial tables so one backup remains a
    # complete, portable portal. Imports are local to avoid module cycles.
    from . import accounts, categories, portal
    accounts.ensure_schema(conn)
    portal.ensure_schema(conn)
    categories.ensure_schema(conn)
    return conn


#: Characters issuers use to hide the middle digits of a card number.
_MASK_CHARS = "X*•·#"


def mask_positions(mask: Optional[str]) -> str:
    """One character per printed card-number position, ``*`` where hidden.

    Separators are noise — "5394 94** **** 4321" and "539494XXXXXX4321" are the
    same printed number — so only digits and masking marks survive.
    """
    return "".join(
        ch if ch.isdigit() else "*"
        for ch in (mask or "").upper()
        if ch.isdigit() or ch in _MASK_CHARS
    )


def same_card(left: Optional[str], right: Optional[str]) -> bool:
    """Do two masked numbers denote one card?

    Issuers change *which* digits they print between statement eras: Axis moved
    from "53346700****8765" to "533467******8765" mid-2021. Comparing the strings
    files one physical card under two, splitting its history. So the numbers are
    compared position by position, and only where both actually show a digit.

    Deliberately strict about the last four, which every issuer prints: without
    that, two masks whose visible windows do not overlap would vacuously agree.
    """
    if (left or "") == (right or ""):
        return bool(left)
    a, b = mask_positions(left), mask_positions(right)
    if not a or len(a) != len(b) or a[-4:] != b[-4:] or "*" in a[-4:]:
        return False
    return all(x == y for x, y in zip(a, b) if x != "*" and y != "*")


def find_card(conn: sqlite3.Connection, masked: str) -> Optional[sqlite3.Row]:
    """The saved card this masked number belongs to, whatever era printed it."""
    rows = conn.execute("SELECT * FROM cards").fetchall()
    return next((row for row in rows if same_card(row["masked_number"], masked)), None)


def find_statement(conn: sqlite3.Connection, stmt: Statement) -> Optional[dict]:
    """Has this exact statement already been imported?

    Matched on the same key the importer uses, so the answer shown in the review
    list is precisely what will happen on import: a hit means replace, not add.
    """
    card = find_card(conn, stmt.account_masked or f"{stmt.issuer}-unknown")
    if not card:
        return None
    row = conn.execute(
        """SELECT s.id, s.source_file, s.imported_at, c.display_name AS card
           FROM statements s JOIN cards c ON c.id = s.card_id
           WHERE s.card_id = ? AND s.period_start IS ? AND s.period_end IS ?""",
        (card["id"], _d(stmt.period_start), _d(stmt.period_end)),
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


def upsert_card(conn: sqlite3.Connection, stmt: Statement, member_id: Optional[int] = None) -> int:
    masked = stmt.account_masked or f"{stmt.issuer}-unknown"
    row = find_card(conn, masked)
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cards (issuer, product, masked_number, last4, display_name, member_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (stmt.issuer, stmt.product, masked, masked[-4:], _card_display(stmt), member_id),
    )
    return int(cur.lastrowid)


def import_statement(
    conn: sqlite3.Connection, stmt: Statement, member_id: Optional[int] = None
) -> tuple[int, int, bool]:
    """Insert or replace one statement. Returns (statement_id, rows, replaced)."""
    card_id = upsert_card(conn, stmt, member_id)
    # Keyed on the billing cycle — see _migrate for why not the statement date.
    key = (card_id, _d(stmt.period_start), _d(stmt.period_end))

    existing = conn.execute(
        "SELECT id FROM statements WHERE card_id IS ? AND period_start IS ? AND period_end IS ?",
        key,
    ).fetchone()
    replaced = existing is not None
    overrides: dict[tuple, str] = {}
    if replaced:
        # Re-import replaces the rows, so carry manual categories across on fields
        # that stay stable when parser or rule changes alter the derived ones.
        overrides = {
            (row["txn_date"], row["description"], row["amount"], row["direction"]):
                row["category_override"]
            for row in conn.execute(
                """SELECT txn_date, description, amount, direction, category_override
                   FROM transactions
                   WHERE statement_id=? AND category_override IS NOT NULL""",
                (existing["id"],),
            ).fetchall()
        }
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

    from . import categories as category_rules

    rules = category_rules.rules_for(
        conn, category_rules.resolve(conn, member_id), "cards"
    )
    conn.executemany(
        """INSERT INTO transactions
           (statement_id, card_id, txn_date, txn_time, description, merchant, category,
            issuer_category, amount, direction, signed, section, is_emi, reward_points,
            fcy_currency, fcy_amount, page, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                stmt_id, card_id, t.date.isoformat(),
                t.time.strftime("%H:%M") if t.time else None,
                t.description, merchant_name(t.description),
                category_rules.categorize_card(t.description, t.category, rules),
                t.category,
                to_paise(t.amount), t.type.value, to_paise(t.signed), t.section,
                1 if t.is_emi else 0, t.reward_points, t.fcy_currency,
                to_paise(t.fcy_amount), t.page, t.raw,
            )
            for t in stmt.transactions
        ],
    )
    if overrides:
        conn.executemany(
            "UPDATE transactions SET category_override=? WHERE id=?",
            [
                (override, row["id"])
                for row in conn.execute(
                    "SELECT id, txn_date, description, amount, direction FROM transactions"
                    " WHERE statement_id=?",
                    (stmt_id,),
                ).fetchall()
                if (override := overrides.get((
                    row["txn_date"], row["description"], row["amount"], row["direction"],
                ))) is not None
            ],
        )
    # Re-evaluate the complete card history: a conversion or cancellation may
    # arrive in a later statement than the original purchase.
    _refresh_emi_flags(conn, card_id)
    conn.commit()
    return stmt_id, len(stmt.transactions), replaced


MAX_CATEGORY_LENGTH = 80


def update_transaction_categories(
    conn: sqlite3.Connection,
    transaction_ids: Iterable[int],
    category: Optional[str],
    card_ids: Iterable[int] = (),
) -> list[dict]:
    """Set or clear the manual category on card rows.

    ``card_ids`` restricts the update to cards the caller may see, so a request
    cannot reach rows outside the signed-in user's members.
    """
    ids = sorted({int(value) for value in transaction_ids})
    if not ids:
        return []
    override = " ".join((category or "").split()).strip() or None
    if override and len(override) > MAX_CATEGORY_LENGTH:
        raise ValueError(f"category must be at most {MAX_CATEGORY_LENGTH} characters")

    allowed = list(card_ids)
    scope = f" AND card_id IN ({','.join('?' * len(allowed))})" if allowed else ""
    rows = conn.execute(
        f"SELECT id, category FROM transactions WHERE id IN ({','.join('?' * len(ids))}){scope}",
        [*ids, *allowed],
    ).fetchall()
    if not rows:
        return []
    found = [int(row["id"]) for row in rows]
    conn.execute(
        f"UPDATE transactions SET category_override=? WHERE id IN ({','.join('?' * len(found))})",
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


def _d(value) -> Optional[str]:
    return value.isoformat() if isinstance(value, dt.date) else None


# ------------------------------------------------------------------ queries

def cards(conn: sqlite3.Connection, member_ids: Iterable[int] = ()) -> list[dict]:
    ids = list(member_ids)
    where = f"WHERE c.member_id IN ({','.join('?' * len(ids))})" if ids else ""
    rows = conn.execute(
        """SELECT c.id, c.issuer, c.product, c.masked_number, c.last4, c.display_name,
                  c.sender_ids_json, c.subject_patterns_json, c.member_id,
                  m.name AS member_name,
                  COUNT(t.id) AS txn_count,
                  MIN(t.txn_date) AS first_txn,
                  MAX(t.txn_date) AS last_txn
           FROM cards c LEFT JOIN members m ON m.id=c.member_id
           LEFT JOIN transactions t ON t.card_id = c.id
           """ + where + " GROUP BY c.id ORDER BY c.display_name",
        ids,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["sender_ids"] = json.loads(item.pop("sender_ids_json") or "[]")
        item["subject_patterns"] = json.loads(item.pop("subject_patterns_json") or "[]")
        out.append(item)
    return out


def set_card_mail_rules(
    conn: sqlite3.Connection, card_id: int, sender_ids: list[str], subject_patterns: list[str]
) -> bool:
    cur = conn.execute(
        "UPDATE cards SET sender_ids_json = ?, subject_patterns_json = ? WHERE id = ?",
        (json.dumps(sender_ids), json.dumps(subject_patterns), card_id),
    )
    conn.commit()
    return cur.rowcount > 0


def date_bounds(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(txn_date) a, MAX(txn_date) b FROM transactions").fetchone()
    return {"min": row["a"], "max": row["b"]}


#: The one category a row counts under: the manual override when there is one,
#: else what the parser derived, else 'Other'. Kept identical to what the
#: listing and grouping queries select, so filtering by a label always matches
#: the label the user is looking at.
CATEGORY_SQL = "COALESCE(NULLIF(TRIM(t.category_override),''), t.category, 'Other')"


def _filters(
    card_ids, date_from, date_to, extra_sql="", params=None, date_column="t.txn_date",
    categories=None,
):
    """Build the shared WHERE clause.

    ``categories`` of ``None`` means the caller is not filtering by category at
    all. An empty sequence is different, and deliberate: the user unticked every
    category, so nothing should match.
    """
    where, args = ["1=1"], list(params or [])
    if card_ids:
        where.append(f"t.card_id IN ({','.join('?' * len(card_ids))})")
        args += list(card_ids)
    if date_from:
        where.append(f"{date_column} >= ?")
        args.append(date_from)
    if date_to:
        where.append(f"{date_column} <= ?")
        args.append(date_to)
    if categories is not None:
        names = list(categories)
        where.append(
            f"{CATEGORY_SQL} IN ({','.join('?' * len(names))})" if names else "0=1"
        )
        args += names
    if extra_sql:
        where.append(extra_sql)
    return " AND ".join(where), args


def category_facets(conn: sqlite3.Connection, card_ids: Iterable[int] = ()) -> list[dict]:
    """Every category present on the given cards, with its row count.

    The category filter needs a list that does not shrink as it is applied, so
    this is scoped by card only — never by the filter it feeds.
    """
    clause, args = _filters(list(card_ids), None, None)
    rows = conn.execute(
        f"""SELECT {CATEGORY_SQL} AS name, COUNT(*) AS n
            FROM transactions t WHERE {clause}
            GROUP BY name ORDER BY name COLLATE NOCASE""",
        args,
    ).fetchall()
    return [{"name": row["name"], "n": row["n"]} for row in rows]


def transactions(
    conn: sqlite3.Connection,
    card_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 5000,
    categories: Optional[Iterable[str]] = None,
) -> list[dict]:
    clause, args = _filters(list(card_ids), date_from, date_to, categories=categories)
    rows = conn.execute(
        f"""SELECT t.id, t.txn_date, t.txn_time, t.description, t.merchant,
                   t.category AS derived_category, t.category_override,
                   COALESCE(NULLIF(TRIM(t.category_override),''), t.category, 'Other') AS category,
                   t.amount, t.direction, t.signed, t.is_emi, t.reward_points,
                   t.fcy_currency, t.fcy_amount, c.display_name AS card, t.card_id,
                   c.member_id, m.name AS member_name,
                   s.statement_date, s.period_start AS statement_period_start,
                   s.period_end AS statement_period_end,
                   substr(COALESCE(s.period_end, s.statement_date),1,7) AS statement_month
            FROM transactions t JOIN cards c ON c.id = t.card_id
            LEFT JOIN members m ON m.id=c.member_id
            JOIN statements s ON s.id = t.statement_id
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
        d["derived_category"] = d["derived_category"] or "Other"
        d["category_is_override"] = d["category_override"] is not None
        out.append(d)
    return out


def analytics(
    conn: sqlite3.Connection,
    card_ids: Iterable[int] = (),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Every aggregate the dashboard needs, in exact integer paise."""
    ids = list(card_ids)
    names = None if categories is None else list(categories)
    clause, args = _filters(ids, date_from, date_to, categories=names)
    purchase_clause, purchase_args = _filters(
        ids, date_from, date_to, extra_sql="t.spend_effect > 0", categories=names
    )
    refund_clause, refund_args = _filters(
        ids, date_from, date_to, extra_sql="t.spend_effect < 0",
        date_column="COALESCE(s.period_end,t.txn_date)", categories=names,
    )
    spend_clause = f"(({purchase_clause}) OR ({refund_clause}))"
    spend_args = purchase_args + refund_args
    payment_clause, payment_args = _filters(
        ids, date_from, date_to, extra_sql="t.payment_effect != 0",
        date_column="COALESCE(s.period_end,t.txn_date)", categories=names,
    )

    spend_totals = conn.execute(
        f"""SELECT
              COALESCE(SUM(spend_effect),0) AS spend,
              COALESCE(SUM(CASE WHEN spend_effect > 0 THEN 1 ELSE 0 END),0) AS purchase_count,
              COALESCE(MAX(spend_effect),0) AS largest
            FROM transactions t JOIN statements s ON s.id=t.statement_id
            WHERE {spend_clause}""",
        spend_args,
    ).fetchone()
    payment_totals = conn.execute(
        f"""SELECT COALESCE(SUM(t.payment_effect),0) AS payments
            FROM transactions t JOIN statements s ON s.id=t.statement_id
            WHERE {payment_clause}""",
        payment_args,
    ).fetchone()
    txn_count = conn.execute(
        f"SELECT COUNT(*) FROM transactions t WHERE {clause}", args
    ).fetchone()[0]

    spend, purchase_count = spend_totals["spend"], spend_totals["purchase_count"]

    monthly = conn.execute(
        f"""SELECT month, SUM(spend) AS spend, SUM(payments) AS payments FROM (
              SELECT substr(t.txn_date,1,7) AS month,
                     SUM(t.spend_effect) AS spend, 0 AS payments
              FROM transactions t WHERE {purchase_clause} GROUP BY month
              UNION ALL
              SELECT substr(COALESCE(s.period_end,t.txn_date),1,7) AS month,
                     SUM(t.spend_effect) AS spend, 0 AS payments
              FROM transactions t JOIN statements s ON s.id=t.statement_id
              WHERE {refund_clause} GROUP BY month
              UNION ALL
              SELECT substr(COALESCE(s.period_end,t.txn_date),1,7) AS month,
                     0 AS spend, SUM(t.payment_effect) AS payments
              FROM transactions t JOIN statements s ON s.id=t.statement_id
              WHERE {payment_clause} GROUP BY month
            )
            GROUP BY month ORDER BY month""",
        purchase_args + refund_args + payment_args,
    ).fetchall()

    by_category = conn.execute(
        f"""SELECT COALESCE(NULLIF(TRIM(t.category_override),''), t.category, 'Other') AS category,
                   SUM(spend_effect) AS total, COUNT(*) AS n
            FROM transactions t JOIN statements s ON s.id=t.statement_id
            WHERE {spend_clause}
            GROUP BY category ORDER BY total DESC""",
        spend_args,
    ).fetchall()

    by_card = conn.execute(
        f"""SELECT c.id AS card_id, c.display_name AS card,
                   SUM(t.spend_effect) AS total, COUNT(*) AS n
            FROM transactions t JOIN cards c ON c.id = t.card_id
            JOIN statements s ON s.id=t.statement_id
            WHERE {spend_clause}
            GROUP BY c.id ORDER BY total DESC""",
        spend_args,
    ).fetchall()

    by_member = conn.execute(
        f"""SELECT m.id AS member_id, COALESCE(m.name,'Unassigned') AS member,
                   SUM(CASE WHEN t.direction='debit' THEN t.amount ELSE 0 END) AS debits,
                   SUM(CASE WHEN t.direction='credit' THEN t.amount ELSE 0 END) AS credits,
                   COUNT(*) AS n
            FROM transactions t JOIN cards c ON c.id=t.card_id
            LEFT JOIN members m ON m.id=c.member_id
            WHERE {clause} GROUP BY c.member_id ORDER BY debits DESC""",
        args,
    ).fetchall()

    merchants = conn.execute(
        f"""SELECT merchant, SUM(spend_effect) AS total, COUNT(*) AS n
            FROM transactions t JOIN statements s ON s.id=t.statement_id
            WHERE {spend_clause}
            GROUP BY merchant ORDER BY total DESC LIMIT 12""",
        spend_args,
    ).fetchall()

    # Reward points, converted purchases and foreign-currency legs are reported
    # separately. EMI principal bookkeeping has zero spend_effect, while the
    # original converted purchase remains counted exactly once.
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
            "payments": to_rupees(payment_totals["payments"]),
            "net": to_rupees(spend - payment_totals["payments"]),
            "txn_count": txn_count,
            "avg_txn": to_rupees(spend // purchase_count) if purchase_count else 0.0,
            "largest": to_rupees(spend_totals["largest"]),
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
        "by_member": [
            {
                "member_id": r["member_id"], "member": r["member"],
                "debits": to_rupees(r["debits"]), "credits": to_rupees(r["credits"]),
                "n": r["n"],
            }
            for r in by_member
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
