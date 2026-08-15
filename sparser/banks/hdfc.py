"""HDFC Bank savings/current account statements (current and legacy layouts).

HDFC draws no column rules, so the columns are measured from the table's own
header row and the rows from the dates in the first column.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import TxnType
from .base import (
    MONEY,
    BankStatement,
    BankTransaction,
    UnsupportedBankStatement,
    checks,
    line_groups,
    line_text,
    money,
)

_DATE = re.compile(r"^\d{2}/\d{2}/(?:\d{4}|\d{2})$")


def _date(value: str) -> dt.date:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid statement date {value!r}")


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
    # Some HDFC layouts print "From : 01/08/2025 To 31/08/2025" with no colon
    # after "To", so the separator is optional.
    period = re.search(
        r"(?:Statement\s+)?From\s*:\s*(\d{2}/\d{2}/(?:\d{4}|\d{2}))"
        r"\s+(?:TO|To)\s*:?\s*(\d{2}/\d{2}/(?:\d{4}|\d{2}))",
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


def _columns(first_page) -> dict[str, float]:
    words = first_page.extract_words(x_tolerance=1, y_tolerance=2)
    for line in line_groups(words):
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


def _repair(text: str) -> str:
    # The newer PDF breaks rail names exactly at the column edge ("H DFC" and
    # "UP I"). Repair only these unmistakable fragments; ordinary names keep
    # their spaces.
    text = re.sub(r"\bH\s+DFC(?=\d)", "HDFC", text, flags=re.I)
    text = re.sub(r"\bUP\s+I\b", "UPI", text, flags=re.I)
    return " ".join(text.split()).strip()


def _words_text(words: list[dict]) -> str:
    if not words:
        return ""
    return _repair(" ".join(line_text(row) for row in line_groups(words)))


#: HDFC chops a narration into fixed-width chunks before the PDF lays them out,
#: so a chunk boundary can fall inside a token.
_NARRATION_WRAP = 40


def _narration_text(words: list[dict]) -> str:
    """Rejoin a narration that was wrapped across several printed lines.

    The narration is cut into 40-character chunks first; only then may the PDF
    break a chunk over more than one printed line, and it does that at a space.
    Joining every printed line with a space therefore invents a space wherever a
    chunk boundary landed mid-token ("…@OKHDFCBA NK-HDFC0004821…" for what the
    bank actually sent as "…@OKHDFCBANK-HDFC0004821…"). Rebuild the chunks, then
    concatenate: a chunk that reassembles to exactly 40 characters was cut
    mid-token and abuts the next one, while 39 means the 40th character was a
    space the layout dropped. Anything else is a chunk this rule does not
    explain — a lone space is the safe join there.
    """
    if not words:
        return ""
    chunks: list[str] = []
    buffer = ""
    for row in line_groups(words):
        line = line_text(row)
        buffer = f"{buffer} {line}" if buffer else line
        if len(buffer) >= _NARRATION_WRAP - 1:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)

    text = ""
    for index, chunk in enumerate(chunks):
        if index and len(chunks[index - 1]) != _NARRATION_WRAP:
            text += " "
        text += chunk
    return _repair(text)


#: Every HDFC layout closes its transaction pages with the same disclaimer block.
#: Matched against a whole printed line, so a merchant named after one of these
#: phrases cannot trip it.
_FOOTER_MARKERS = re.compile(
    r"closing\s+balance\s+includes\s+funds"
    r"|contents\s+of\s+this\s+statement"
    r"|registered\s+office\s+address"
    r"|gstin\s+number\s+details"
    r"|state\s+account\s+branch\s+gst"
    r"|generation\s+date\s*:"
    r"|^hdfc\s+bank\s+limited$",
    re.I,
)


def _body_bottom(page, words: list[dict]) -> float:
    """Y coordinate where the transaction table ends and the page footer begins.

    The last dated row on a page has no following row to close its band, so the
    band runs on into the disclaimer block and the narration column collects
    "BANK LIMITED balance includes funds earmarked for hold and uncleared funds".
    Three independent bounds, whichever is highest: the table's bottom rule, the
    first footer line, and the original blind margin as a floor.
    """
    height = float(page.height)
    limits = [height - 45]
    # The bottom rule is a hint, not a requirement: one layout draws the table
    # without ruling lines at all, and the footer text alone bounds those pages.
    rules = [
        float(rect["top"]) for rect in getattr(page, "rects", ())
        if abs(float(rect["bottom"]) - float(rect["top"])) < 2
        and float(rect["x1"]) - float(rect["x0"]) > float(page.width) * 0.7
    ]
    if rules:
        limits.append(max(rules))
    # Only the lower half is searched: page one prints the bank's own name and
    # address above the table, and that is not a footer.
    footer = [w for w in words if float(w["top"]) > height * 0.5]
    limits.extend(
        float(row[0]["top"]) for row in line_groups(footer)
        if _FOOTER_MARKERS.search(line_text(row).strip())
    )
    return min(limits)


def _page_transactions(page, page_number: int, columns: dict[str, float]) -> list[BankTransaction]:
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    bottom = _body_bottom(page, words)
    words = [w for w in words if float(w["top"]) < bottom]
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
                if index + 1 < len(date_words) else min(bottom, top + 80)
            )
        else:
            lower = top - 2.5
            upper = (
                float(date_words[index + 1]["top"]) - 2.5
                if index + 1 < len(date_words) else min(bottom, top + 100)
            )
        band = [w for w in words if lower <= float(w["top"]) < upper]
        on_line = [w for w in band if abs(float(w["top"]) - top) <= 2.5]

        # A long reference can start a hair left of the narration's right edge,
        # so a word must also *end* before the reference column to count as
        # narration. Testing x0 alone appends "…0000CMS0000000000" to the text.
        narrative = _narration_text([
            w for w in band
            if columns["narration_left"] <= float(w["x0"]) < columns["narration_right"]
            and float(w["x1"]) <= columns["reference"]
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
                if left <= float(w["x0"]) < right and MONEY.match(w["text"])
            ), None)
            return money(word["text"]) if word else Decimal("0")

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


#: Enough to identify the issuer. The IFSC prefix is bank-assigned, so it holds on
#: layouts that never spell the bank's name out — but it is only accepted as the
#: account's *own* IFSC label, never a bare HDFC0… that could be a counterparty's
#: code quoted inside another bank's statement.
_HDFC_MARKERS = (r"HDFCBANKLIMITED", r"IFSC[:\s]*HDFC0\d{6}", r"HDFCBANK\.COM")

#: Only the front matter is searched for the account header; beyond this the
#: document is transaction pages and a miss is a genuine miss.
_HEADER_SEARCH_PAGES = 5


def _anchor_page(document) -> int:
    """Index of the page carrying the account header.

    Statements that lead with an account-relationship summary put the header on
    page 2, so it is found rather than assumed to be first.
    """
    for index, page in enumerate(document.pages[:_HEADER_SEARCH_PAGES]):
        text = page.extract_text() or ""
        has_account = re.search(r"Account\s*(?:number|No\.?)[ \t]*:[ \t]*[0-9]", text, re.I)
        has_period = re.search(r"From\s*:\s*\d{2}/\d{2}/\d{2,4}\s+To\s*:?\s*\d{2}/\d{2}/\d{2,4}", text, re.I)
        if has_account and has_period:
            return index
    return 0


def parse(path: str | Path) -> BankStatement:
    """Parse the digital-text HDFC savings/current-account layouts in the samples."""
    source = Path(path)
    with pdfplumber.open(source) as document:
        if not document.pages:
            raise UnsupportedBankStatement("empty PDF")
        anchor = _anchor_page(document)
        first_text = document.pages[anchor].extract_text() or ""
        compact = re.sub(r"\s+", "", first_text.upper())
        issuer = any(re.search(marker, compact) for marker in _HDFC_MARKERS)
        if not issuer or not re.search(r"ACCOUNT(?:NO|NUMBER)", compact):
            raise UnsupportedBankStatement("not an HDFC bank account statement")
        metadata = _metadata(first_text)
        columns = _columns(document.pages[anchor])
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
    statement.checks = checks(statement)
    return statement
