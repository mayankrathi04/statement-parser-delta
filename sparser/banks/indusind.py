"""IndusInd Bank savings/current account statements ("Statement of Customer").

Two things make this layout different from HDFC's. The table is *ruled*: the bank
draws every column border, so cells are cut on its own lines instead of on
midpoints guessed between headings. And the account number is printed masked
("50XXXXXXX123") everywhere it appears, so the statement identifies an account by
the digits it is willing to show.

A statement also carries blocks that are not transactions at all — a relationship
summary, an interest certificate — which is why the table is found by its header
and bounded by the rules that header stands on.
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
    unwrap,
)

#: Enough to identify the issuer without matching a counterparty's name quoted
#: inside another bank's statement: the bank's own name, its own branch IFSC
#: label, or its own domain.
_MARKERS = (r"INDUSINDBANK", r"BRANCHIFSCCODE:?INDB0\d{6}", r"INDUSIND\.BANK\.IN")

_DATE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")

#: The transaction table's columns, in the order IndusInd prints them.
_ROLES = (
    ("date", "date"),
    ("particulars", "particulars"),
    ("reference", "chq"),
    ("withdrawal", "withdrawal"),
    ("deposit", "deposit"),
    ("balance", "balance"),
)


def _date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError as exc:
        raise ValueError(f"invalid statement date {value!r}") from exc


def _metadata(text: str, document_text: str) -> dict:
    period = re.search(
        r"Statement\s*Period\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*(?:TO|to)\s*(\d{2}-[A-Za-z]{3}-\d{4})",
        text,
        re.I,
    )
    if not period:
        raise UnsupportedBankStatement("IndusInd statement is missing its date range")

    # The transaction-history block names the account the table belongs to; the
    # relationship summary above it may list several accounts, so it is only a
    # fallback for a statement that omits the block.
    identity = re.search(
        r"Account\s+Number\s+Name\s+Holding\s+Status[^\n]*\n\s*([0-9X]{6,})\s+(.+?)\s+"
        r"(?:Primary|Joint|Secondary|Sole)\s+Holder",
        text,
        re.I,
    )
    account_number = identity.group(1) if identity else None
    if not account_number:
        fallback = re.search(r"^\s*([0-9]{2}X{3,}[0-9]{2,}|[0-9]{9,18})\s", text, re.M)
        if not fallback:
            raise UnsupportedBankStatement("IndusInd statement is missing its account number")
        account_number = fallback.group(1)

    product = re.search(r"Product\s*Description\s*:\s*(.+?)\s*(?:Branch\s*Address|$)", text, re.I | re.M)
    branch = re.search(r"Branch\s*Address\s*:\s*([^\n]+)", text, re.I)
    # Only the interest certificate spells the deposit type out; without it the
    # product description alone names the account.
    account_type = re.search(
        r"[0-9X]{6,}\s+((?:SAVING|CURRENT|OVERDRAFT)[A-Z-]*)\s+[A-Z]{3}\b", document_text
    )

    return {
        "account_number": account_number,
        "period_start": _date(period.group(1)),
        "period_end": _date(period.group(2)),
        "account_holder": " ".join(identity.group(2).split()).title() if identity else None,
        "product": product.group(1).strip() if product else None,
        "branch": branch.group(1).strip() if branch else None,
        "account_type": account_type.group(1) if account_type else None,
    }


def _header_row(page) -> Optional[list[dict]]:
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    for row in line_groups(words):
        labels = line_text(row).lower()
        if all(word in labels for _role, word in _ROLES):
            return row
    return None


def _amount(words: list[dict], band: tuple[float, float]) -> Decimal:
    word = next((w for w in cell(words, *band) if MONEY.match(w["text"])), None)
    return money(word["text"]) if word else Decimal("0")


def _page_transactions(page, page_number: int) -> list[BankTransaction]:
    header = _header_row(page)
    if not header:
        return []
    edges, foot = ruled_columns(page, min(float(w["top"]) for w in header))
    bands = columns(edges, header, _ROLES)
    missing = [role for role, _ in _ROLES if role not in bands]
    if missing:
        raise UnsupportedBankStatement(
            f"IndusInd transaction table is missing {', '.join(missing)}"
        )

    body = [
        w for w in page.extract_words(x_tolerance=1, y_tolerance=2)
        if max(float(w["bottom"]) for w in header) < float(w["top"]) < foot
    ]
    dates = sorted(
        (w for w in cell(body, *bands["date"]) if _DATE.match(w["text"])),
        key=lambda w: float(w["top"]),
    )

    rows: list[BankTransaction] = []
    for index, date_word in enumerate(dates):
        top = float(date_word["top"])
        # A row owns everything printed under it until the next date: IndusInd
        # rules the table's columns but not its rows, and a narration too long
        # for its column continues on the lines below its own date.
        end = float(dates[index + 1]["top"]) - 2 if index + 1 < len(dates) else foot
        band = [w for w in body if top - 2 <= float(w["top"]) < end]

        description = unwrap(cell(band, *bands["particulars"]), bands["particulars"][1])
        reference = unwrap(cell(band, *bands["reference"]), bands["reference"][1]) or None
        withdrawal = _amount(band, bands["withdrawal"])
        deposit = _amount(band, bands["deposit"])
        balance = _amount(band, bands["balance"])
        # "Brought Forward" and "Carried Forward" carry a balance and no amount:
        # they restate the opening and closing figures rather than move money.
        if not description or (withdrawal == 0 and deposit == 0) or withdrawal and deposit:
            continue

        # A reversal can be printed as a negative value in its original column
        # rather than moved to the opposite one; direction, not sign, carries the
        # balance effect.
        if withdrawal:
            direction = TxnType.DEBIT if withdrawal > 0 else TxnType.CREDIT
            amount = abs(withdrawal)
        else:
            direction = TxnType.CREDIT if deposit > 0 else TxnType.DEBIT
            amount = abs(deposit)
        rows.append(BankTransaction(
            date=_date(date_word["text"]),
            description=description,
            reference=reference,
            amount=amount,
            type=direction,
            balance=balance,
            page=page_number,
            raw=" ".join(line_text(row) for row in line_groups(band)),
        ))
    return rows


def parse(path: str | Path) -> BankStatement:
    source = Path(path)
    with pdfplumber.open(source) as document:
        if not document.pages:
            raise UnsupportedBankStatement("empty PDF")
        texts = [page.extract_text() or "" for page in document.pages]
        document_text = "\n".join(texts)
        compact = re.sub(r"\s+", "", document_text.upper())
        if not any(re.search(marker, compact) for marker in _MARKERS):
            raise UnsupportedBankStatement("not an IndusInd bank account statement")

        anchor = next(
            (index for index, page in enumerate(document.pages) if _header_row(page)), None
        )
        if anchor is None:
            raise UnsupportedBankStatement("IndusInd transaction table header was not found")
        metadata = _metadata(texts[anchor], document_text)

        transactions = []
        for physical_page, page in enumerate(document.pages[anchor:], anchor + 1):
            printed = re.search(r"Page\s+(\d+)\s+of\s+\d+", texts[physical_page - 1], re.I)
            page_number = int(printed.group(1)) if printed else physical_page
            transactions.extend(
                row for row in _page_transactions(page, page_number)
                if metadata["period_start"] <= row.date <= metadata["period_end"]
            )

    statement = BankStatement(
        parser_id="indusind_bank_v1",
        bank_code="INDUSIND",
        bank_name="IndusInd Bank",
        transactions=transactions,
        source_file=source.name,
        **metadata,
    )
    statement.checks = checks(statement)
    return statement
