"""Read-only MCP server for open-ended statement analytics.

The server deliberately exposes curated analytical operations instead of raw
SQL.  MCP clients can inspect financial history but cannot mutate the database,
read stored passwords, or traverse local files.
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

from .store import to_rupees

DB_PATH = Path(os.environ.get("SPARSER_DB", "statements.db")).expanduser().resolve()

mcp = FastMCP(
    "Statement Analytics",
    instructions=(
        "Read-only analysis of imported credit-card statements. Start with list_cards or "
        "get_overview, narrow by ISO dates/card IDs, and cite returned counts and totals. "
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


def _money_row(row: sqlite3.Row, fields: tuple[str, ...]) -> dict:
    item = dict(row)
    for field in fields:
        item[field] = to_rupees(item[field])
    return item


@mcp.tool()
def list_cards() -> dict:
    """List available cards, IDs, date coverage, and transaction counts."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.display_name, c.issuer, c.product, c.last4,
                      COUNT(t.id) AS transactions, MIN(t.txn_date) AS first_date,
                      MAX(t.txn_date) AS last_date
               FROM cards c LEFT JOIN transactions t ON t.card_id = c.id
               GROUP BY c.id ORDER BY c.display_name"""
        ).fetchall()
    return {"database": str(DB_PATH), "cards": [dict(r) for r in rows]}


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
                       COALESCE(SUM(CASE WHEN direction='debit' THEN amount END),0) AS spend,
                       COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS payments,
                       COALESCE(MAX(CASE WHEN direction='debit' THEN amount END),0) AS largest
                FROM transactions t WHERE {clause}""",
            args,
        ).fetchone()
        monthly = conn.execute(
            f"""SELECT substr(txn_date,1,7) AS month,
                       COALESCE(SUM(CASE WHEN direction='debit' THEN amount END),0) AS spend,
                       COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS payments,
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
    """Group debit spending by month, category, merchant, or card."""
    expressions = {
        "month": "substr(t.txn_date,1,7)",
        "category": "COALESCE(t.category,'Uncategorised')",
        "merchant": "COALESCE(t.merchant,t.description)",
        "card": "c.display_name",
    }
    expression = expressions[group_by]
    clause, args = _where(card_ids, date_from, date_to, direction="debit")
    limit = max(1, min(limit, 200))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT {expression} AS label, SUM(t.amount) AS amount, COUNT(*) AS transactions
                FROM transactions t JOIN cards c ON c.id=t.card_id
                WHERE {clause} GROUP BY label ORDER BY amount DESC LIMIT ?""",
            args + [limit],
        ).fetchall()
        total = conn.execute(
            f"SELECT COALESCE(SUM(t.amount),0) FROM transactions t WHERE {clause}", args
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
        clause, args = _where(card_ids, start, end, direction="debit")
        with _connect() as conn:
            total = conn.execute(
                f"SELECT COALESCE(SUM(amount),0), COUNT(*) FROM transactions t WHERE {clause}", args
            ).fetchone()
            categories = conn.execute(
                f"""SELECT COALESCE(category,'Uncategorised') label, SUM(amount) amount
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
    clause, args = _where(card_ids, date_from, date_to, direction="debit")
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(t.merchant,t.description) AS merchant,
                       COUNT(*) AS transactions, COUNT(DISTINCT substr(txn_date,1,7)) AS months,
                       SUM(amount) AS total, MIN(amount) AS minimum, MAX(amount) AS maximum,
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
    clause, args = _where(card_ids, date_from, date_to, direction="debit")
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.txn_date, t.description, t.merchant, t.category, t.amount,
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
        "Cards own statements and transactions. Dates are ISO YYYY-MM-DD. "
        "Tool outputs express card amounts in INR rupees; the SQLite database stores paise. "
        "direction=debit means spending and direction=credit means payments/refunds. "
        "Reward points and original foreign-currency amounts are separate units. "
        "Statements with confidence=1 passed every arithmetic reconciliation check."
    )


@mcp.prompt()
def analyze_statement_history(question: str) -> str:
    """Create a disciplined workflow for answering a financial-history question."""
    return (
        f"Answer this question using Statement Analytics MCP tools: {question}\n\n"
        "First inspect cards/date coverage. Choose the narrowest relevant tools and filters. "
        "Separate debit spend from credits, INR from foreign currency, and points from money. "
        "Quantify claims with totals/counts/date ranges, identify limitations, and do not infer "
        "intent or fraud from a merchant name alone."
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
