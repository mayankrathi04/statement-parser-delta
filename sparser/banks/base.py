"""Canonical models and the PDF primitives every bank layout needs.

Bank accounts deliberately use a separate model from credit cards. A bank row has
a value date, cheque/reference number and running balance; forcing those fields
through the card schema would make both storage formats less honest.

Every bank prints the same six ideas — date, narration, reference, withdrawal,
deposit, running balance — in its own geometry, so each bank gets its own module
in this package. Shared here is only what is genuinely bank-agnostic: the
canonical row, the reconciliation checks every parser is judged by, and the
handful of geometry helpers (words → lines, ruled-table edges, rejoining a
hard-wrapped cell) that the layouts have in common.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Check, TxnType


class UnsupportedBankStatement(RuntimeError):
    """Raised when a bank statement has no installed parser."""


class BankTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt.date
    value_date: Optional[dt.date] = None
    description: str
    reference: Optional[str] = None
    amount: Decimal = Field(description="Positive transaction magnitude")
    type: TxnType
    balance: Decimal
    page: int = 0
    raw: str = ""

    @property
    def signed(self) -> Decimal:
        """Credits increase a bank balance; debits decrease it."""
        return self.amount if self.type is TxnType.CREDIT else -self.amount


class BankStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parser_id: str
    bank_code: str
    bank_name: str
    account_holder: Optional[str] = None
    account_number: str = Field(exclude=True, repr=False)
    account_type: Optional[str] = None
    product: Optional[str] = None
    branch: Optional[str] = None
    period_start: dt.date
    period_end: dt.date
    currency: str = "INR"
    transactions: list[BankTransaction] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    source_file: str = ""

    @property
    def account_fingerprint(self) -> str:
        # Some banks only ever print the number partly masked. The fingerprint is
        # built from whatever digits the statement shows, so it stays stable for
        # that bank's own statements without inventing the hidden ones.
        raw = f"{self.bank_code}:{re.sub(r'\D', '', self.account_number)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def last4(self) -> str:
        """The account's visible trailing digits, at most four.

        Stripping every non-digit first would splice a masked number's leading
        digits onto its trailing ones ("50XXXXXXX123" → "0123") and print an
        ending the customer has never seen. Only the visible tail counts.
        """
        tail = re.search(r"\d{1,4}$", self.account_number.strip())
        return tail.group(0) if tail else ""

    @property
    def masked_number(self) -> str:
        return f"•••• {self.last4}"

    @property
    def confidence(self) -> float:
        if not self.checks:
            return 0.0
        weights = {"error": 1.0, "warning": 0.3}
        total = sum(weights.get(c.severity, 1.0) for c in self.checks)
        got = sum(weights.get(c.severity, 1.0) for c in self.checks if c.passed)
        return round(got / total, 4) if total else 0.0

    @property
    def opening_balance(self) -> Optional[Decimal]:
        if not self.transactions:
            return None
        first = self.transactions[0]
        return first.balance - first.signed

    @property
    def closing_balance(self) -> Optional[Decimal]:
        return self.transactions[-1].balance if self.transactions else None


MONEY = re.compile(r"^-?[\d,]+\.\d{2}$")


def money(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid statement amount {value!r}") from exc


def line_groups(words: list[dict], tolerance: float = 2.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        line = next(
            (row for row in reversed(lines[-4:]) if abs(row[0]["top"] - word["top"]) <= tolerance),
            None,
        )
        if line is None:
            lines.append([word])
        else:
            line.append(word)
    return lines


def line_text(row: list[dict]) -> str:
    return " ".join(w["text"] for w in sorted(row, key=lambda item: item["x0"]))


def unwrap(words: list[dict], right_edge: float, slack: float = 7.0) -> str:
    """Rejoin a cell's printed lines the way the bank wrapped them.

    A narration too long for its column is cut at the column edge, mid-token:
    "…@okhdfcban" / "k/…" is one string the layout broke in half, and joining
    those halves with a space invents a handle nobody typed. A line that stops
    short of the edge ended on its own, so that break really was a space.
    """
    rows = line_groups(words)
    text = ""
    wrapped = False
    for index, row in enumerate(rows):
        if index:
            text += "" if wrapped else " "
        text += line_text(row)
        wrapped = max(float(w["x1"]) for w in row) >= right_edge - slack
    return " ".join(text.split()).strip()


def _cluster(values: list[float], tolerance: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def ruled_columns(
    page, header_top: float, *, tolerance: float = 2.0
) -> tuple[list[float], float]:
    """Column edges and foot of the ruled table whose header starts at ``header_top``.

    Banks that draw their table borders have already answered the question every
    geometric parser guesses at: where one column stops and the next begins.
    Reading the rules is exact where a midpoint between two headings is not —
    right-aligned amounts drift left as they gain digits and cross any midpoint
    you pick.

    A column rule is one that *crosses the header line and starts at it*. Both
    halves earn their place: crossing alone would elect the page border, which
    runs past every header on the page, while starting alone misses the banks
    that box their header a few points above its first word.
    """
    verticals = [
        edge for edge in page.edges
        if edge.get("orientation") == "v"
        and header_top - 30 <= float(edge["top"]) <= header_top + 8
        and float(edge["bottom"]) >= header_top + 4
    ]
    if not verticals:
        return [], 0.0
    xs = [(float(edge["x0"]) + float(edge["x1"])) / 2 for edge in verticals]
    edges = [round(sum(group) / len(group), 2) for group in _cluster(xs, tolerance)]
    # The header's own borders may be short. The table's foot is the lowest point
    # any rule standing on one of those column edges reaches.
    foot = max(
        (
            float(edge["bottom"]) for edge in page.edges
            if edge.get("orientation") == "v" and float(edge["bottom"]) > header_top
            and any(abs(float(edge["x0"]) - x) <= tolerance for x in edges)
        ),
        default=header_top,
    )
    return edges, foot


def ruled_rows(
    page, top: float, bottom: float, left: float, right: float, *, coverage: float = 0.6
) -> list[float]:
    """Y positions of the rules that separate one printed row from the next.

    Cell borders are drawn segment by segment, so a row separator arrives as a
    run of short horizontals at the same height rather than one long line; they
    are merged before their span is measured against the table's width.
    """
    spans: dict[float, list[float]] = {}
    for edge in page.edges:
        if edge.get("orientation") != "h":
            continue
        y = (float(edge["top"]) + float(edge["bottom"])) / 2
        if not top - 2 <= y <= bottom + 2:
            continue
        key = next((known for known in spans if abs(known - y) <= 1.5), y)
        span = spans.setdefault(key, [float(edge["x0"]), float(edge["x1"])])
        span[0] = min(span[0], float(edge["x0"]))
        span[1] = max(span[1], float(edge["x1"]))
    width = right - left
    return sorted(
        y for y, (x0, x1) in spans.items()
        if min(x1, right) - max(x0, left) >= coverage * width
    )


def columns(
    edges: list[float], header: list[dict], roles: tuple[tuple[str, str], ...]
) -> dict[str, tuple[float, float]]:
    """Name each ruled band after the header word printed inside it.

    Roles are given in the order the bank prints them and are consumed in that
    order, so "Date and Time" claims the date role and "Value Date" the next one
    instead of both answering to the first column that mentions a date. Reading
    the roles off the header — rather than counting bands from the left — is what
    lets a layout gain a serial-number or cheque column without renumbering
    everything after it.
    """
    found: dict[str, tuple[float, float]] = {}
    pending = list(roles)
    for left, right in zip(edges, edges[1:]):
        text = " ".join(w["text"] for w in cell(header, left, right)).lower()
        match = next(
            ((index, role) for index, (role, word) in enumerate(pending) if word in text),
            None,
        )
        if match:
            index, role = match
            found[role] = (left, right)
            pending = pending[index + 1:]
    return found


def cell(words: list[dict], left: float, right: float, *, slack: float = 2.0) -> list[dict]:
    """The words whose centre falls inside a column."""
    return [
        word for word in words
        if left - slack <= (float(word["x0"]) + float(word["x1"])) / 2 < right + slack
    ]


def checks(stmt: BankStatement) -> list[Check]:
    """The same five questions asked of every bank, whatever the layout.

    The running balance is the one that matters: an extraction can only report
    the rows the bank printed if each one moves the balance exactly as stated.
    """
    txns = stmt.transactions
    balance_errors = 0
    checked_transitions = 0
    for previous, current in zip(txns, txns[1:]):
        # Mini PDFs may contain selected page ranges from a much larger parent
        # statement. A printed-page gap is not a failed balance transition.
        if current.page not in (previous.page, previous.page + 1):
            continue
        checked_transitions += 1
        if previous.balance + current.signed != current.balance:
            balance_errors += 1
    dates_in_period = all(stmt.period_start <= row.date <= stmt.period_end for row in txns)
    directions_valid = all(row.amount > 0 for row in txns)
    return [
        Check(name="account identity", passed=bool(stmt.account_number and stmt.last4),
              detail=f"{stmt.bank_name} account ending {stmt.last4}"),
        Check(name="transactions found", passed=bool(txns),
              detail=f"{len(txns)} transaction rows extracted"),
        Check(name="transaction directions", passed=directions_valid,
              detail="each row has exactly one positive withdrawal or deposit"),
        Check(name="statement period", passed=dates_in_period,
              detail=f"rows fall within {stmt.period_start} → {stmt.period_end}"),
        Check(name="running balance", passed=balance_errors == 0,
              detail=(f"all {checked_transitions} available adjacent balance transitions reconcile"
                      if not balance_errors
                      else f"{balance_errors} adjacent balance transition(s) do not reconcile")),
    ]
