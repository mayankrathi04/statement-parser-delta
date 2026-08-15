"""A queryable copy of the statement database holding only analytical columns.

The curated tools answer the questions they were written for. Anything else —
"merchant within category by month", "the cards I used only once" — needs SQL,
and this module is what makes handing out SQL safe.

The statement database is not safe to expose directly: `card_secrets` holds
statement PDF passwords and `portal_users` holds password hashes, in the same
file as the transactions. Rather than police queries against that file, this
builds a *separate in-memory database* containing only approved tables and
columns. A query cannot reach a secret because no secret was ever copied. This
mirrors what the investment pillar does in `semantic-layer.ts`, for the same
reason.

Money stays in integer paise, exactly as stored. Rounding to rupees per row and
then summing is how a total ends up a few paise wrong; callers divide by 100 at
the end instead.
"""
from __future__ import annotations

import sqlite3

#: `target table -> SELECT that fills it, or a tuple of SELECTs tried in order`.
#:
#: Every column is listed explicitly. A `SELECT *` here would silently start
#: exposing whatever a later migration adds to the source table, which is the
#: failure mode this module exists to prevent.
#:
#: Where a tuple is given, the first SELECT that the source database can answer
#: wins. This is how ownership columns are published without breaking a
#: database that predates the portal migration: the MCP server opens the file
#: read-only, so it cannot migrate it, and a hard failure would take the whole
#: table down rather than one column with it.
SAFE_TABLES: dict[str, str | tuple[str, ...]] = {
    # Ownership: a card belongs to a household member, and members belong to a
    # portal user. `member_id` is nullable — a card imported before the portal
    # existed, or never assigned, reads as NULL and must never be silently
    # attributed to anyone.
    "cards": (
        """
        SELECT c.id, c.issuer, c.product, c.last4, c.display_name,
               c.member_id, m.name AS member_name
        FROM cards c LEFT JOIN members m ON m.id = c.member_id
        """,
        """
        SELECT id, issuer, product, last4, display_name FROM cards
        """,
    ),
    "card_statements": """
        SELECT id, card_id, statement_date, period_start, period_end, currency,
               due_date, previous_dues AS previous_dues_paise,
               payments_credits AS payments_credits_paise,
               purchases_debits AS purchases_debits_paise,
               finance_charges AS finance_charges_paise,
               total_dues AS total_dues_paise, minimum_due AS minimum_due_paise,
               credit_limit AS credit_limit_paise,
               available_credit AS available_credit_paise, confidence
        FROM statements
    """,
    "card_transactions": (
        """
        SELECT t.id, t.statement_id, t.card_id, c.display_name AS card,
               c.member_id, m.name AS member_name,
               t.txn_date, t.txn_time, substr(t.txn_date,1,7) AS month,
               substr(t.txn_date,1,4) AS year, t.description, t.merchant,
               COALESCE(t.category,'Uncategorised') AS category,
               t.amount AS amount_paise, t.direction, t.signed AS signed_paise,
               t.section, t.is_emi, t.spend_effect AS spend_effect_paise,
               t.payment_effect AS payment_effect_paise, t.reward_points,
               t.fcy_currency, t.fcy_amount AS fcy_amount_paise
        FROM transactions t JOIN cards c ON c.id = t.card_id
        LEFT JOIN members m ON m.id = c.member_id
        """,
        """
        SELECT t.id, t.statement_id, t.card_id, c.display_name AS card,
               t.txn_date, t.txn_time, substr(t.txn_date,1,7) AS month,
               substr(t.txn_date,1,4) AS year, t.description, t.merchant,
               COALESCE(t.category,'Uncategorised') AS category,
               t.amount AS amount_paise, t.direction, t.signed AS signed_paise,
               t.section, t.is_emi, t.spend_effect AS spend_effect_paise,
               t.payment_effect AS payment_effect_paise, t.reward_points,
               t.fcy_currency, t.fcy_amount AS fcy_amount_paise
        FROM transactions t JOIN cards c ON c.id = t.card_id
        """,
    ),
    "bank_accounts": (
        """
        SELECT a.id, a.bank_code, a.bank_name, a.last4, a.account_type,
               a.product, a.display_name, a.member_id, m.name AS member_name
        FROM bank_accounts a LEFT JOIN members m ON m.id = a.member_id
        """,
        """
        SELECT id, bank_code, bank_name, last4, account_type, product,
               display_name
        FROM bank_accounts
        """,
    ),
    "bank_statements": """
        SELECT id, account_id, parser_id, period_start, period_end,
               coverage_start, coverage_end, currency,
               opening_balance AS opening_balance_paise,
               closing_balance AS closing_balance_paise, confidence, imported_at
        FROM bank_statements
    """,
    "bank_transactions": (
        """
        SELECT t.id, t.statement_id, t.account_id, a.display_name AS account,
               a.member_id, m.name AS member_name,
               t.txn_date, t.value_date, substr(t.txn_date,1,7) AS month,
               substr(t.txn_date,1,4) AS year, t.description, t.reference,
               t.counterparty,
               COALESCE(t.category_override, t.category, 'Uncategorised') AS category,
               t.amount AS amount_paise, t.direction, t.signed AS signed_paise,
               t.balance AS balance_paise
        FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id
        LEFT JOIN members m ON m.id = a.member_id
        """,
        """
        SELECT t.id, t.statement_id, t.account_id, a.display_name AS account,
               t.txn_date, t.value_date, substr(t.txn_date,1,7) AS month,
               substr(t.txn_date,1,4) AS year, t.description, t.reference,
               t.counterparty,
               COALESCE(t.category_override, t.category, 'Uncategorised') AS category,
               t.amount AS amount_paise, t.direction, t.signed AS signed_paise,
               t.balance AS balance_paise
        FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id
        """,
    ),
    # The household, so a query can group by person without a second round
    # trip. Only the portal user's id and display fields are copied; the
    # password hash beside them in `portal_users` never leaves the source file.
    "portal_users": (
        """
        SELECT id, email, display_name FROM portal_users
        """,
        """
        SELECT 0 AS id, '' AS email, '' AS display_name WHERE 0
        """,
    ),
    "members": (
        """
        SELECT m.id, m.user_id, m.name, m.is_default,
               (SELECT count(*) FROM cards c WHERE c.member_id = m.id) AS cards,
               (SELECT count(*) FROM bank_accounts a WHERE a.member_id = m.id) AS bank_accounts
        FROM members m
        """,
        """
        SELECT 0 AS id, 0 AS user_id, '' AS name, 0 AS is_default,
               0 AS cards, 0 AS bank_accounts WHERE 0
        """,
    ),
    "categories": """
        SELECT id, name, pattern, applies_to, position FROM categories
    """,
}

RULES: tuple[str, ...] = (
    "Money columns end in `_paise` and are exact integers. Sum them, then divide "
    "by 100.0 once at the end.",
    "Card spend is `spend_effect_paise`, not `amount_paise`: it is positive for a "
    "purchase, negative for a refund, and zero for a bill payment. Summing "
    "`amount_paise` counts bill payments as spending and doubles the total.",
    "Bank `signed_paise` is credit-positive; card `signed_paise` is debit-positive. "
    "They are opposite by design — never union the two ledgers without flipping one.",
    "`bank_transactions.balance_paise` is the running balance after that row, so the "
    "closing balance is the balance of the LAST row by (txn_date, id), not a sum.",
    "Use the ready-made `month` and `year` columns rather than strftime.",
    "A card statement's `total_dues_paise` is what the issuer billed; it will not "
    "equal the sum of that cycle's transactions, and the statement is authoritative.",
    "`credit_limit_paise` is null on statements whose parser did not capture one. "
    "Treat null as unknown, never as zero.",
    "Bank and card ledgers overlap: a card bill payment is a debit in "
    "`bank_transactions` AND a `payment_effect_paise` row in `card_transactions`. "
    "Adding both double-counts it.",
    "`member_id` is who a card or bank account belongs to, and is NULL where "
    "nobody was assigned — a card imported before the portal existed, or one "
    "never claimed. Filter on it for a per-person total, but report the NULL "
    "rows rather than dropping them: unassigned spend is still spend.",
    "`member_id` lives on the CARD and the ACCOUNT, never on the transaction, so "
    "a person's history moves with the instrument. Re-assigning a card changes "
    "who every past transaction on it belongs to.",
)

RECIPES: tuple[dict[str, str], ...] = (
    {
        "question": "Card spend by merchant within one category, by month",
        "sql": "SELECT month, merchant, sum(spend_effect_paise)/100.0 AS spend "
               "FROM card_transactions WHERE category = 'Food' AND spend_effect_paise != 0 "
               "GROUP BY month, merchant ORDER BY month, spend DESC",
    },
    {
        "question": "Months where a category rose against its own trailing average",
        "sql": "WITH m AS (SELECT category, month, sum(spend_effect_paise)/100.0 AS spend "
               "FROM card_transactions WHERE spend_effect_paise != 0 GROUP BY category, month) "
               "SELECT category, month, spend, avg(spend) OVER (PARTITION BY category "
               "ORDER BY month ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS trailing_avg "
               "FROM m ORDER BY category, month",
    },
    {
        "question": "Closing balance per bank account",
        "sql": "SELECT account, balance_paise/100.0 AS closing FROM bank_transactions t "
               "WHERE t.id = (SELECT id FROM bank_transactions WHERE account_id = t.account_id "
               "ORDER BY txn_date DESC, id DESC LIMIT 1)",
    },
)


def build_semantic_database(source: sqlite3.Connection) -> sqlite3.Connection:
    """Copy the approved columns into a fresh in-memory database."""
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    for name, select in SAFE_TABLES.items():
        # Each candidate is a progressively less demanding shape of the same
        # table: the first names ownership columns a pre-portal database does
        # not have, the last names only columns that have always existed.
        candidates = (select,) if isinstance(select, str) else select
        columns, rows = ["unavailable"], []
        for candidate in candidates:
            try:
                cursor = source.execute(candidate)
                columns = [description[0] for description in cursor.description]
                rows = cursor.fetchall()
                break
            except sqlite3.OperationalError:
                # A table or column a migration has not created yet is absent,
                # not fatal. If every candidate fails the table is still
                # created, empty, so a query against it returns no rows instead
                # of an error an agent would read as "tool broken".
                continue
        quoted = ",".join('"' + column + '"' for column in columns)
        target.execute(f"CREATE TABLE {name}({quoted})")
        if rows:
            placeholders = ",".join("?" * len(columns))
            target.executemany(
                f"INSERT INTO {name} VALUES({placeholders})",
                [tuple(row) for row in rows],
            )
    target.commit()
    target.execute("PRAGMA query_only = ON")
    return target


def describe(database: sqlite3.Connection) -> dict:
    """Table names, columns and row counts, for an agent's first call."""
    schema = {}
    counts = {}
    for name in SAFE_TABLES:
        schema[name] = [
            row["name"] for row in database.execute(f"PRAGMA table_info({name})")
        ]
        counts[name] = database.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
    return {"tables": schema, "row_counts": counts, "rules": list(RULES),
            "recipes": [dict(recipe) for recipe in RECIPES]}
