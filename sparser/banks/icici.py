"""ICICI Bank account statements ("Statement of Transactions in … Account no.").

Fully ruled, like IDFC FIRST: every row is a boxed band and the remark inside it
is exactly the lines that box contains. That is worth more here than elsewhere,
because ICICI sizes each row to its remark and prints the dated line five points
below the band's top — so a remark's first line sits *above* its own date, and
any rule that groups words by proximity to a date hands it to the row above.

The header is three printed lines deep ("Transaction / Date", "Withdrawal /
Amount (INR)"), and the table opens with a serial-number column the other banks
do not have, so columns are named by the header word inside them rather than
counted from the left.
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

_MARKERS = (r"ICICIBANKLIMITED", r"ICICI\.BANK\.IN", r"IFSC[:\s]*ICIC0\d{6}")

_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

#: One sentence carries the account, its currency and the period. It is also the
#: proof that this really is an ICICI *account* statement: the bank's name alone
#: turns up in any statement that quotes an ICICI counterparty.
_SUMMARY = re.compile(
    r"Statement\s+of\s+Transactions\s+in\s+(?P<type>[A-Za-z ]+?)\s+Account\s+no\.?\s*"
    r"(?P<number>[0-9X]+)\s+in\s+(?P<currency>[A-Z]{3})\s+for\s+the\s+period\s+"
    r"(?P<start>[A-Za-z]+\s+\d{1,2},\s*\d{4})\s*(?:-|to)\s*(?P<end>[A-Za-z]+\s+\d{1,2},\s*\d{4})",
    re.I,
)

#: The transaction table's columns, in the order ICICI prints them.
_ROLES = (
    ("date", "date"),
    ("cheque", "cheque"),
    ("remarks", "remarks"),
    ("withdrawal", "withdrawal"),
    ("deposit", "deposit"),
    ("balance", "balance"),
)


#: ICICI opens a remark cell with a short field of its own: a counterparty
#: (a merchant tag, a remitter's name) where it has one, and this bare label where it
#: does not. The label only repeats the direction the row already carries, and
#: leaving it in front would make it the merchant every enrichment rule sees.
_TYPE_LABEL = re.compile(r"^(?:debit|credit)\s+trxn\b[\s,-]*", re.I)


def _date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError(f"invalid statement date {value!r}") from exc


def _period_date(value: str) -> dt.date:
    return dt.datetime.strptime(" ".join(value.split()), "%B %d, %Y").date()


def _header_row(page) -> Optional[list[dict]]:
    for row in line_groups(page.extract_words(x_tolerance=1, y_tolerance=2)):
        labels = line_text(row).lower()
        if all(label in labels for label in ("withdrawal", "deposit", "balance")):
            return row
    return None


def _metadata(page, document_text: str) -> dict:
    summary = _SUMMARY.search(" ".join(document_text.split()))
    if not summary:
        raise UnsupportedBankStatement("not an ICICI bank account statement")
    holder = re.search(r"^(.+?)\s+Your\s+Base\s+Branch\s*:", document_text, re.I | re.M)

    # The address block is two columns wide — the customer on the left, the base
    # branch on the right — so the branch is read by its x position. Splitting
    # the printed line on its label would keep only the first of its four lines.
    branch = None
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    label = next((w for w in words if w["text"].lower().startswith("branch")), None)
    header = _header_row(page)
    if label and header:
        limit = min(float(w["top"]) for w in header)
        branch = " ".join(
            line_text(row) for row in line_groups([
                w for w in words
                if float(w["x0"]) >= float(label["x1"]) - 90
                and float(label["top"]) - 2 <= float(w["top"]) < limit
            ])
        ).replace("Your Base Branch:", "").strip()

    return {
        "account_number": summary.group("number"),
        "account_type": summary.group("type").strip().upper(),
        "currency": summary.group("currency").upper(),
        "period_start": _period_date(summary.group("start")),
        "period_end": _period_date(summary.group("end")),
        "account_holder": " ".join(holder.group(1).split()).title() if holder else None,
        "branch": " ".join(branch.split()) or None if branch else None,
    }


def _amount(words: list[dict], band: tuple[float, float]) -> Optional[Decimal]:
    word = next((w for w in cell(words, *band) if MONEY.match(w["text"])), None)
    return money(word["text"]) if word else None


def _page_transactions(page, page_number: int) -> list[BankTransaction]:
    header = _header_row(page)
    if not header:
        return []
    header_top = min(float(w["top"]) for w in header)
    header_bottom = max(float(w["bottom"]) for w in header)
    edges, _foot = ruled_columns(page, header_top)
    if len(edges) < 6:
        raise UnsupportedBankStatement("ICICI transaction table has no column rules")

    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    # The header's own three lines all name columns, and only together do they
    # name all of them: "Transaction" sits above "Date", "Withdrawal" above
    # "Amount (INR)".
    bands = columns(
        edges,
        [w for w in words if header_top - 15 <= float(w["top"]) <= header_bottom + 12],
        _ROLES,
    )
    missing = [role for role, _ in _ROLES if role not in bands]
    if missing:
        raise UnsupportedBankStatement(
            f"ICICI transaction table is missing {', '.join(missing)}"
        )

    # ICICI rules every row but stops drawing verticals below the header, so the
    # table's foot is wherever its row separators stop. Reading to the end of the
    # page costs nothing: the legend printed underneath has no dated rows.
    separators = ruled_rows(page, header_bottom, float(page.height), edges[0], edges[-1])
    rows: list[BankTransaction] = []
    for top, bottom in zip(separators, separators[1:]):
        band = [w for w in words if top < float(w["top"]) < bottom]
        dated = next((w for w in cell(band, *bands["date"]) if _DATE.match(w["text"])), None)
        withdrawal = _amount(band, bands["withdrawal"])
        deposit = _amount(band, bands["deposit"])
        balance = _amount(band, bands["balance"])
        # A balance of 0.00 is a real balance — an account emptied to the paisa —
        # so a row is judged on whether the figure is *printed*, not on its value.
        if not dated or balance is None or (withdrawal is None and deposit is None):
            continue
        if withdrawal is not None and deposit is not None:
            continue

        if withdrawal is not None:
            direction = TxnType.DEBIT if withdrawal > 0 else TxnType.CREDIT
            amount = abs(withdrawal)
        else:
            direction = TxnType.CREDIT if deposit > 0 else TxnType.DEBIT
            amount = abs(deposit)
        rows.append(BankTransaction(
            date=_date(dated["text"]),
            description=_TYPE_LABEL.sub(
                "", unwrap(cell(band, *bands["remarks"]), bands["remarks"][1])
            ),
            reference=unwrap(cell(band, *bands["cheque"]), bands["cheque"][1]) or None,
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
            raise UnsupportedBankStatement("not an ICICI bank account statement")
        metadata = _metadata(document.pages[0], document_text)

        transactions = []
        for page_number, page in enumerate(document.pages, 1):
            transactions.extend(
                row for row in _page_transactions(page, page_number)
                if metadata["period_start"] <= row.date <= metadata["period_end"]
            )

    statement = BankStatement(
        parser_id="icici_bank_v1",
        bank_code="ICICI",
        bank_name="ICICI Bank",
        transactions=transactions,
        source_file=source.name,
        **metadata,
    )
    statement.checks = checks(statement)
    return statement
