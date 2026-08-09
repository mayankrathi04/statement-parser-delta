"""Arithmetic validators — the difference between a demo and a product.

Every check reconciles extracted rows against a number the issuer printed
independently. A statement that fails these should never be shipped to a user
as clean output; it should be flagged or routed to review.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Callable

from .schema import Check, Statement, TxnType

# Issuers round the final due amount down to the rupee, so exact equality is the
# wrong bar; anything under a rupee is presentation, not a parse error.
ROUNDING_TOLERANCE = Decimal("1.00")

CHECKS: dict[str, Callable[[Statement], Check]] = {}


def check(name: str):
    def deco(fn):
        CHECKS[name] = fn
        return fn

    return deco


@check("cc_summary_reconcile")
def cc_summary_reconcile(stmt: Statement) -> Check:
    """previous dues - payments + purchases + finance charges == total dues."""
    s = stmt.summary
    # Not every issuer prints a finance-charges box; absent means nil, not unknown.
    charges = s.finance_charges or Decimal("0")
    required = [s.previous_dues, s.payments_credits, s.purchases_debits, s.total_dues]
    if any(p is None for p in required):
        return Check(
            name="cc_summary_reconcile",
            passed=False,
            severity="error",
            detail="summary panel incomplete; cannot reconcile",
        )
    computed = s.previous_dues - s.payments_credits + s.purchases_debits + charges
    delta = abs(computed - s.total_dues)
    return Check(
        name="cc_summary_reconcile",
        passed=delta <= ROUNDING_TOLERANCE,
        severity="error",
        detail=f"computed {computed} vs stated {s.total_dues} (delta {delta})",
    )


def _side_check(stmt: Statement, name: str, kind: TxnType, stated: Decimal | None, label: str) -> Check:
    """Compare one side of the ledger against the figure the issuer printed.

    Splitting debits and credits (rather than checking the net) is what makes a
    miss diagnosable: a wrong total on one side alone points straight at either
    a dropped row or a misread credit marker.
    """
    if stated is None:
        return Check(name=name, passed=False, severity="warning", detail=f"no {label} figure in summary")
    rows = [t for t in stmt.transactions if t.type is kind]
    total = sum((t.amount for t in rows), Decimal("0"))
    delta = abs(total - stated)
    return Check(
        name=name,
        passed=delta <= ROUNDING_TOLERANCE,
        severity="error",
        detail=f"{len(rows)} {kind.value} rows sum to {total} vs stated {label} {stated} (delta {delta})",
    )


@check("debits_match_purchases")
def debits_match_purchases(stmt: Statement) -> Check:
    return _side_check(
        stmt, "debits_match_purchases", TxnType.DEBIT, stmt.summary.purchases_debits, "purchases"
    )


@check("credits_match_payments")
def credits_match_payments(stmt: Statement) -> Check:
    return _side_check(
        stmt, "credits_match_payments", TxnType.CREDIT, stmt.summary.payments_credits, "payments/credits"
    )


@check("transactions_reconcile_to_total")
def transactions_reconcile_to_total(stmt: Statement) -> Check:
    """End-to-end: opening dues + every extracted row + charges == closing dues.

    This is the check that actually proves the row list is complete — it ties the
    transactions we parsed to two numbers printed independently of them.
    """
    s = stmt.summary
    if s.previous_dues is None or s.total_dues is None:
        return Check(
            name="transactions_reconcile_to_total",
            passed=False,
            severity="error",
            detail="opening or closing dues missing",
        )
    net = sum((t.signed for t in stmt.transactions), Decimal("0"))
    computed = s.previous_dues + net + (s.finance_charges or Decimal("0"))
    delta = abs(computed - s.total_dues)
    return Check(
        name="transactions_reconcile_to_total",
        passed=delta <= ROUNDING_TOLERANCE,
        severity="error",
        detail=(
            f"{s.previous_dues} + net {net} + charges {s.finance_charges or 0} "
            f"= {computed} vs stated {s.total_dues} (delta {delta})"
        ),
    )


# A card is billed on posting date but prints the transaction date, so a swipe a
# few days before the cycle opens legitimately appears in it. Only dates well
# outside this window indicate a misparse.
SETTLEMENT_LAG = dt.timedelta(days=5)


@check("transactions_within_period")
def transactions_within_period(stmt: Statement) -> Check:
    if not (stmt.period_start and stmt.period_end):
        return Check(
            name="transactions_within_period",
            passed=False,
            severity="warning",
            detail="billing period not extracted",
        )
    lo, hi = stmt.period_start - SETTLEMENT_LAG, stmt.period_end
    stray = [t for t in stmt.transactions if not (lo <= t.date <= hi)]
    return Check(
        name="transactions_within_period",
        passed=not stray,
        severity="warning",
        detail=(
            "all dates inside billing period"
            if not stray
            else f"{len(stray)} txn(s) outside period, e.g. {stray[0].date} {stray[0].description[:30]}"
        ),
    )


@check("balance_chain")
def balance_chain(stmt: Statement) -> Check:
    """Bank-statement running balance: balance[i-1] +/- amount[i] == balance[i].

    Localises the exact offending row rather than reporting a bulk mismatch.
    """
    rows = [t for t in stmt.transactions if getattr(t, "balance", None) is not None]
    if len(rows) < 2:
        return Check(
            name="balance_chain",
            passed=False,
            severity="warning",
            detail="no running-balance column on this document",
        )
    for prev, cur in zip(rows, rows[1:]):
        expected = prev.balance - cur.signed
        if abs(expected - cur.balance) > Decimal("0.01"):
            return Check(
                name="balance_chain",
                passed=False,
                severity="error",
                detail=f"break at {cur.date} {cur.description[:40]}: expected {expected}, got {cur.balance}",
            )
    return Check(name="balance_chain", passed=True, detail=f"{len(rows)} rows chain cleanly")


def run_checks(stmt: Statement, names: list[str]) -> list[Check]:
    out = []
    for n in names:
        fn = CHECKS.get(n)
        if fn is None:
            out.append(Check(name=n, passed=False, severity="warning", detail="unknown check"))
        else:
            out.append(fn(stmt))
    return out
