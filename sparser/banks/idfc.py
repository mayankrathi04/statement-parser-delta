"""IDFC FIRST Bank account statements ("Consolidated Statement").

The layout is fully ruled — every column *and* every row border is drawn — so a
transaction is read as a table cell rather than reconstructed from a band of
words. That matters here more than elsewhere: IDFC stacks three printed lines
inside one row (date over time, description over its continuation) and centres
the amounts against them, so a row's own borders are the only honest boundary.

The document is *consolidated*: one PDF can carry several accounts, each under
its own "… ACCOUNT DETAILS FOR A/C :" heading. A statement models one account, so
only the first transaction account's section is read and the rest are left alone.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pdfplumber

from ..schema import TxnType
from .base import (
    MONEY,
    BankStatement,
    BankTransaction,
    UnsupportedBankStatement,
    cell,
    checks,
    columns,
    line_groups,
    line_text,
    money,
    ruled_columns,
    ruled_rows,
    unwrap,
)

_MARKERS = (r"IDFCFIRSTBANK", r"IFSC:?IDFB0\d{6}", r"IDFCFIRSTBANK\.COM")

#: "18 Nov 25" — the transaction date, printed above its own time of day.
_DATE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{2}$")

#: The transaction table's columns, in the order IDFC prints them.
_ROLES = (
    ("date", "date"),
    ("value", "value"),
    ("details", "details"),
    ("reference", "ref"),
    ("withdrawal", "withdrawals"),
    ("deposit", "deposits"),
    ("balance", "balance"),
)

#: Each account section opens with its own heading; the same pattern finds where
#: our account's rows start and where the next account's take over.
_SECTION = re.compile(
    r"([A-Z][A-Z /&-]*?)\s*ACCOUNT\s+DETAILS\s+FOR\s+A\s*/?\s*C\s*:\s*([0-9X]+)", re.I
)


def _date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(" ".join(value.split()), "%d %b %y").date()
    except ValueError as exc:
        raise ValueError(f"invalid statement date {value!r}") from exc


def _period_date(value: str) -> dt.date:
    return dt.datetime.strptime(value.strip(), "%d-%b-%Y").date()


def _metadata(document_text: str) -> dict:
    period = re.search(
        r"STATEMENT\s+PERIOD\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*(?:to|TO)\s*(\d{2}-[A-Za-z]{3}-\d{4})",
        document_text,
        re.I,
    )
    if not period:
        raise UnsupportedBankStatement("IDFC FIRST statement is missing its date range")
    section = _SECTION.search(document_text)
    if not section:
        raise UnsupportedBankStatement("IDFC FIRST statement is missing its account number")

    holder = re.search(r"CUSTOMER\s+NAME\s+(.+?)\s+ACCOUNT\s+(?:BRANCH|NAME)", document_text, re.I)
    branch = re.search(r"ACCOUNT\s+BRANCH\s+([^\n]+)", document_text, re.I)
    currency = re.search(r"\bCURRENCY\s+([A-Z]{3})\b", document_text)

    return {
        "account_number": section.group(2),
        "account_type": " ".join(section.group(1).split()).upper() or None,
        "period_start": _period_date(period.group(1)),
        "period_end": _period_date(period.group(2)),
        "account_holder": " ".join(holder.group(1).split()) if holder else None,
        "branch": branch.group(1).strip() if branch else None,
        "currency": currency.group(1) if currency else "INR",
    }


def _header_rows(page) -> list[list[dict]]:
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    return [
        row for row in line_groups(words)
        if all(word in line_text(row).lower() for _role, word in _ROLES)
    ]


def _section_marks(page) -> list[tuple[float, str]]:
    """(y, account number) for each account heading printed on this page."""
    marks = []
    for row in line_groups(page.extract_words(x_tolerance=1, y_tolerance=2)):
        found = _SECTION.search(line_text(row))
        if found:
            marks.append((float(row[0]["top"]), found.group(2)))
    return marks


def _amount(words: list[dict], band: tuple[float, float]) -> Decimal:
    word = next((w for w in cell(words, *band) if MONEY.match(w["text"])), None)
    return money(word["text"]) if word else Decimal("0")


def _balance(words: list[dict], band: tuple[float, float]) -> Optional[Decimal]:
    """The running balance, signed by the Cr/Dr marker IDFC prints beside it."""
    inside = cell(words, *band)
    word = next((w for w in inside if MONEY.match(w["text"])), None)
    if not word:
        return None
    value = money(word["text"])
    overdrawn = any(w["text"].upper() == "DR" for w in inside)
    return -value if overdrawn else value


def _page_transactions(page, page_number: int, account: str, carried: bool) -> tuple[list[BankTransaction], bool]:
    """Rows on one page, plus whether our account's section runs onto the next."""
    marks = _section_marks(page)
    rows: list[BankTransaction] = []
    mine = carried

    for header in _header_rows(page):
        header_top = min(float(w["top"]) for w in header)
        header_bottom = max(float(w["bottom"]) for w in header)
        # Whose table is this? The heading immediately above it on this page, or
        # — if the page opens mid-section — whatever the previous page was in.
        above = [number for y, number in marks if y < header_top]
        mine = (above[-1] == account) if above else carried
        if not mine:
            continue

        edges, foot = ruled_columns(page, header_top)
        words = page.extract_words(x_tolerance=1, y_tolerance=2)
        bands = columns(edges, header, _ROLES)
        missing = [role for role, _ in _ROLES if role not in bands]
        if missing:
            raise UnsupportedBankStatement(
                f"IDFC FIRST transaction table is missing {', '.join(missing)}"
            )
        separators = ruled_rows(page, header_bottom, foot, edges[0], edges[-1])

        for top, bottom in zip(separators, separators[1:]):
            band = [w for w in words if top < float(w["top"]) < bottom]
            if not band:
                continue
            dated = next(
                (line for line in line_groups(cell(band, *bands["date"]))
                 if _DATE.match(line_text(line))),
                None,
            )
            balance = _balance(band, bands["balance"])
            withdrawal = _amount(band, bands["withdrawal"])
            deposit = _amount(band, bands["deposit"])
            # The opening-balance row states a balance and moves no money.
            if not dated or balance is None or (withdrawal == 0 and deposit == 0):
                continue
            if withdrawal and deposit:
                continue

            value_date = next(
                (line for line in line_groups(cell(band, *bands["value"]))
                 if _DATE.match(line_text(line))),
                None,
            )
            if withdrawal:
                direction = TxnType.DEBIT if withdrawal > 0 else TxnType.CREDIT
                amount = abs(withdrawal)
            else:
                direction = TxnType.CREDIT if deposit > 0 else TxnType.DEBIT
                amount = abs(deposit)
            rows.append(BankTransaction(
                date=_date(line_text(dated)),
                value_date=_date(line_text(value_date)) if value_date else None,
                description=unwrap(cell(band, *bands["details"]), bands["details"][1]),
                reference=unwrap(cell(band, *bands["reference"]), bands["reference"][1]) or None,
                amount=amount,
                type=direction,
                balance=balance,
                page=page_number,
                raw=" ".join(line_text(line) for line in line_groups(band)),
            ))

    # A section that ends on this page ends with the last heading printed on it.
    if marks:
        mine = marks[-1][1] == account
    return rows, mine


def parse(path: str | Path) -> BankStatement:
    source = Path(path)
    with pdfplumber.open(source) as document:
        if not document.pages:
            raise UnsupportedBankStatement("empty PDF")
        texts = [page.extract_text() or "" for page in document.pages]
        document_text = "\n".join(texts)
        compact = re.sub(r"\s+", "", document_text.upper())
        if not any(re.search(marker, compact) for marker in _MARKERS):
            raise UnsupportedBankStatement("not an IDFC FIRST bank account statement")
        metadata = _metadata(document_text)

        transactions = []
        carried = False
        for physical_page, page in enumerate(document.pages, 1):
            printed = re.search(r"Page\s+(\d+)\s+of\s+\d+", texts[physical_page - 1], re.I)
            page_number = int(printed.group(1)) if printed else physical_page
            rows, carried = _page_transactions(
                page, page_number, metadata["account_number"], carried
            )
            transactions.extend(
                row for row in rows
                if metadata["period_start"] <= row.date <= metadata["period_end"]
            )

    statement = BankStatement(
        parser_id="idfc_first_bank_v1",
        bank_code="IDFC",
        bank_name="IDFC FIRST Bank",
        transactions=transactions,
        source_file=source.name,
        **metadata,
    )
    statement.checks = checks(statement)
    return statement
