"""Text -> typed values. All the messy real-world coercion lives here."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

RUPEE = "₹"

# Amount token, after glyph normalisation and currency-symbol stripping.
# Handles Indian lakh grouping (1,23,456.78) and plain (1234.56).
_AMOUNT_RE = re.compile(r"^[+\-]?\s*[\d,]+(?:\.\d{1,2})?$")
_NUM_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")


def apply_glyph_fixes(text: str, fixes: dict[str, str]) -> str:
    """Repair broken ToUnicode CMaps.

    HDFC's statement font maps the rupee glyph to U+0043 ('C'), so pdfplumber
    reports "C33,527.36". Applied only where the char abuts a digit, so genuine
    letters in merchant names survive.
    """
    for bad, good in fixes.items():
        text = re.sub(rf"{re.escape(bad)}(?=\s*[\d,])", good, text)
    return text


def is_amount(token: str) -> bool:
    t = token.replace(RUPEE, "").replace("₨", "").strip()
    return bool(t) and bool(_AMOUNT_RE.match(t))


# Issuers mark currency differently, and broken fonts mangle the mark itself:
# HDFC's rupee decodes as 'C', ICICI's as a backtick, YES Bank spells "Rs.".
_CURRENCY_RE = re.compile(r"(?:₹|₨|`|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?", re.I)


def is_currency_amount(token: str) -> bool:
    """Stricter than `is_amount`: a currency mark must be present.

    Summary panels sit next to bare numerals (a due date's "04", a GST rate),
    and those are not amounts. Requiring the currency mark keeps label anchors
    from latching onto a neighbouring digit.
    """
    return bool(_CURRENCY_RE.search(token))


_DECIMAL_RE = re.compile(r"\d[\d,]*\.\d{2}")


def is_decimal_amount(token: str) -> bool:
    """For issuers that print no currency mark: two decimal places is the signal.

    Rejects the bare integers that share a summary panel with real figures —
    a year, a due-date day, a rewards count.
    """
    return bool(_DECIMAL_RE.search(token))


def parse_amount(text: str) -> Optional[Decimal]:
    """Return magnitude as Decimal. Sign/direction is decided by the caller."""
    if not text:
        return None
    t = text.replace(RUPEE, " ").replace("₨", " ").replace("`", " ")
    t = re.sub(r"\b(?:Rs\.?|INR)\b", " ", t, flags=re.I)
    m = _NUM_RE.search(t)
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def parse_signed_amount(text: str) -> Optional[Decimal]:
    """Balance fields carry a direction marker; magnitude alone loses it.

    A card can sit in credit after an overpayment, and the issuer prints that as
    "6,627.00 CR". Treated as a positive due, every downstream reconciliation is
    off by twice the balance, so the sign is captured here.
    """
    amount = parse_amount(text)
    if amount is None:
        return None
    return -amount if is_credit_marker(text) else amount


def is_credit_marker(text: str) -> bool:
    """Credits are flagged either by a leading '+' (HDFC CC) or a trailing Cr."""
    t = text.replace(RUPEE, "").strip()
    # Some older HDFC PDFs kern the suffix directly against the amount
    # ("315.60Cr"), so a word boundary before ``Cr`` is not guaranteed.
    return t.startswith("+") or bool(re.search(r"Cr\.?$", text.strip(), re.I))


def parse_date(text: str, fmts: list[str]) -> Optional[dt.date]:
    """Strict, format-pinned parsing. Never let a guesser swap day and month."""
    t = text.strip().strip(",|")
    for f in fmts:
        try:
            return dt.datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


def parse_time(text: str) -> Optional[dt.time]:
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return dt.time(h, mi)


def parse_int(text: str) -> Optional[int]:
    m = re.search(r"\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
