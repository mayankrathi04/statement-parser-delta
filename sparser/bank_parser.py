"""Parsers and canonical models for deposit-account statements.

Bank accounts deliberately use a separate model from credit cards. A bank row has
a value date, cheque/reference number and running balance; forcing those fields
through the card schema would make both storage formats less honest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pdfplumber
from pydantic import BaseModel, ConfigDict, Field

from .schema import Check, TxnType


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
        raw = f"{self.bank_code}:{re.sub(r'\D', '', self.account_number)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def last4(self) -> str:
        return re.sub(r"\D", "", self.account_number)[-4:]

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


_DATE = re.compile(r"^\d{2}/\d{2}/(?:\d{4}|\d{2})$")
_MONEY = re.compile(r"^-?[\d,]+\.\d{2}$")


def _date(value: str) -> dt.date:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid statement date {value!r}")


def _money(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid statement amount {value!r}") from exc


def _match(text: str, pattern: str, label: str) -> str:
    found = re.search(pattern, text, re.I | re.M)
    if not found:
        raise UnsupportedBankStatement(f"HDFC statement is missing {label}")
    return found.group(1).strip()


def _metadata(page_text: str) -> dict:
    account_number = _match(
        page_text,
        r"Account\s*(?:number|No\.?)[ \t]*:[ \t]*([0-9][0-9 ]{5,})",
        "account number",
    )
    period = re.search(
        r"(?:Statement\s+)?From\s*:\s*(\d{2}/\d{2}/(?:\d{4}|\d{2}))"
        r"\s+(?:TO|To)\s*:\s*(\d{2}/\d{2}/(?:\d{4}|\d{2}))",
        page_text,
        re.I,
    )
    if not period:
        raise UnsupportedBankStatement("HDFC statement is missing its date range")

    account_line = re.search(
        r"Account\s*(?:number|No\.?)[ \t]*:[ \t]*[0-9][0-9 ]{5,}[ \t]+([^\n]+)",
        page_text,
        re.I,
    )
    holder = re.search(r"^\s*((?:MR|MRS|MS)\.?\s+[A-Z][A-Z .'-]+?)\s*$", page_text, re.I | re.M)
    branch = re.search(r"Account\s*Branch\s*:\s*([^\n]+)", page_text, re.I)
    account_type = re.search(r"Account\s*Type\s*:\s*([^\n]+)", page_text, re.I)
    currency = re.search(r"Currency\s*:\s*([A-Z]{3})", page_text, re.I)

    return {
        "account_number": re.sub(r"\D", "", account_number),
        "period_start": _date(period.group(1)),
        "period_end": _date(period.group(2)),
        "product": account_line.group(1).strip() if account_line else None,
        "account_holder": " ".join(holder.group(1).split()).title() if holder else None,
        "branch": branch.group(1).strip() if branch else None,
        "account_type": account_type.group(1).strip() if account_type else None,
        "currency": currency.group(1).upper() if currency else "INR",
    }


def _line_groups(words: list[dict], tolerance: float = 2.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        line = next((row for row in reversed(lines[-4:]) if abs(row[0]["top"] - word["top"]) <= tolerance), None)
        if line is None:
            lines.append([word])
        else:
            line.append(word)
    return lines


def _columns(first_page) -> dict[str, float]:
    words = first_page.extract_words(x_tolerance=1, y_tolerance=2)
    for line in _line_groups(words):
        labels = " ".join(w["text"] for w in line).lower()
        if "narration" not in labels or "closing" not in labels:
            continue

        def x(prefix: str) -> float:
            word = next((w for w in line if w["text"].lower().startswith(prefix)), None)
            if not word:
                raise UnsupportedBankStatement(f"HDFC transaction table is missing {prefix}")
            return float(word["x0"])

        date_x = x("date")
        narration_x = x("narration")
        reference_x = x("chq")
        value_x = x("value")
        withdrawal_x = x("withdrawal")
        deposit_x = x("deposit")
        closing_x = x("closing")
        return {
            "date": date_x,
            "narration": narration_x,
            "reference": reference_x,
            "value": value_x,
            "withdrawal": withdrawal_x,
            "deposit": deposit_x,
            "closing": closing_x,
            "narration_left": date_x + 15,
            "narration_right": reference_x - 5,
            "reference_right": (reference_x + value_x) / 2,
            "value_right": (value_x + withdrawal_x) / 2,
            # Amounts are right-aligned. Their x0 shifts left as they gain
            # digits, so the next column's left edge is a safer boundary than
            # the midpoint between headings.
            "withdrawal_right": deposit_x - 5,
            "deposit_right": closing_x - 5,
            "header_bottom": max(float(w["bottom"]) for w in line),
        }
    raise UnsupportedBankStatement("HDFC transaction table header was not found")


def _words_text(words: list[dict]) -> str:
    if not words:
        return ""
    rows = _line_groups(words)
    text = " ".join(
        " ".join(w["text"] for w in sorted(row, key=lambda item: item["x0"]))
        for row in rows
    )
    # The newer PDF breaks rail names exactly at the column edge ("H DFC" and
    # "UP I"). Repair only these unmistakable fragments; ordinary names keep
    # their spaces.
    text = re.sub(r"\bH\s+DFC(?=\d)", "HDFC", text, flags=re.I)
    text = re.sub(r"\bUP\s+I\b", "UPI", text, flags=re.I)
    return " ".join(text.split()).strip()


def _page_transactions(page, page_number: int, columns: dict[str, float]) -> list[BankTransaction]:
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    date_words = [
        w for w in words
        if _DATE.match(w["text"]) and float(w["x0"]) < columns["narration_left"]
    ]
    date_words.sort(key=lambda w: w["top"])
    result: list[BankTransaction] = []
    # The current HDFC layout has a narrow date column and vertically centres
    # wrapped narration around its dated row. The legacy layout has a much wider
    # date-to-narration header gap and always wraps downward. Detect this from
    # stable table geometry, not from the first transaction on a page: that row
    # can be one line even when later rows wrap above their date.
    centred_narration = columns["narration"] - columns["date"] < 75

    for index, date_word in enumerate(date_words):
        top = float(date_word["top"])
        if centred_narration:
            lower = (
                (float(date_words[index - 1]["top"]) + top) / 2
                if index else max(0, top - 16)
            )
            upper = (
                (top + float(date_words[index + 1]["top"])) / 2
                if index + 1 < len(date_words) else min(float(page.height) - 45, top + 80)
            )
        else:
            lower = top - 2.5
            upper = (
                float(date_words[index + 1]["top"]) - 2.5
                if index + 1 < len(date_words) else min(float(page.height) - 45, top + 100)
            )
        band = [w for w in words if lower <= float(w["top"]) < upper]
        on_line = [w for w in band if abs(float(w["top"]) - top) <= 2.5]

        narrative = _words_text([
            w for w in band
            if columns["narration_left"] <= float(w["x0"]) < columns["narration_right"]
        ])
        reference = _words_text([
            w for w in band
            if columns["reference"] - 8 <= float(w["x0"]) < columns["reference_right"]
        ]) or None
        value_word = next((
            w for w in on_line
            if _DATE.match(w["text"])
            and columns["value"] - 8 <= float(w["x0"]) < columns["value_right"]
        ), None)

        def amount_between(left: float, right: float) -> Decimal:
            word = next((
                w for w in on_line
                if left <= float(w["x0"]) < right and _MONEY.match(w["text"])
            ), None)
            return _money(word["text"]) if word else Decimal("0")

        withdrawal = amount_between(columns["value_right"], columns["withdrawal_right"])
        deposit = amount_between(columns["withdrawal_right"], columns["deposit_right"])
        balance = amount_between(columns["deposit_right"], float(page.width) + 1)
        if not narrative or balance == 0 or (withdrawal == 0 and deposit == 0):
            continue
        if withdrawal and deposit:
            continue

        # Banks sometimes print reversals as a negative value in the original
        # column instead of moving the value to the opposite column. Canonical
        # rows always keep a positive magnitude and let direction express the
        # balance effect: a negative withdrawal is a credit, while a negative
        # deposit is a debit.
        if withdrawal:
            direction = TxnType.DEBIT if withdrawal > 0 else TxnType.CREDIT
            amount = abs(withdrawal)
        else:
            direction = TxnType.CREDIT if deposit > 0 else TxnType.DEBIT
            amount = abs(deposit)
        raw = _words_text(band)
        result.append(BankTransaction(
            date=_date(date_word["text"]),
            value_date=_date(value_word["text"]) if value_word else None,
            description=narrative,
            reference=reference,
            amount=amount,
            type=direction,
            balance=balance,
            page=page_number,
            raw=raw,
        ))
    return result


def _checks(stmt: BankStatement) -> list[Check]:
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
              detail=f"HDFC account ending {stmt.last4}"),
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


def parse_hdfc_bank_pdf(path: str | Path) -> BankStatement:
    """Parse the digital-text HDFC savings/current-account layouts in the samples."""
    source = Path(path)
    with pdfplumber.open(source) as document:
        if not document.pages:
            raise UnsupportedBankStatement("empty PDF")
        first_text = document.pages[0].extract_text() or ""
        compact = re.sub(r"\s+", "", first_text.upper())
        if "HDFCBANKLIMITED" not in compact or not re.search(r"ACCOUNT(?:NO|NUMBER)", compact):
            raise UnsupportedBankStatement("only HDFC bank account statements are supported for now")
        metadata = _metadata(first_text)
        columns = _columns(document.pages[0])
        transactions = []
        for physical_page, page in enumerate(document.pages, 1):
            page_text = page.extract_text() or ""
            printed = re.search(
                r"Page\s*(?:No\s*\.?)?\s*[:.]?\s*(\d+)(?:\s+of\s+\d+)?",
                page_text,
                re.I,
            )
            page_number = int(printed.group(1)) if printed else physical_page
            transactions.extend(
                row for row in _page_transactions(page, page_number, columns)
                if metadata["period_start"] <= row.date <= metadata["period_end"]
            )

    statement = BankStatement(
        parser_id="hdfc_bank_v1",
        bank_code="HDFC",
        bank_name="HDFC Bank",
        transactions=transactions,
        source_file=source.name,
        **metadata,
    )
    statement.checks = _checks(statement)
    return statement


# Bank dispatch is intentionally independent of the credit-card YAML engine.
# Register the next bank here; a failed fingerprint falls through without
# changing any card configuration or extraction code.
BANK_PARSERS = (parse_hdfc_bank_pdf,)


def parse_bank_pdf(path: str | Path) -> BankStatement:
    errors: list[str] = []
    for parser in BANK_PARSERS:
        try:
            return parser(path)
        except UnsupportedBankStatement as exc:
            errors.append(str(exc))
    raise UnsupportedBankStatement("; ".join(errors) or "no bank statement parser matched")
