import datetime as dt
from decimal import Decimal

import pytest

from sparser.geometry import Line, Word
from sparser.normalize import (
    apply_glyph_fixes,
    is_credit_amount_in_row,
    is_credit_marker,
    is_currency_amount,
    parse_amount,
    parse_date,
    parse_int,
    parse_signed_amount,
    parse_time,
)

FIXES = {"C": "₹"}


@pytest.mark.parametrize(
    "raw,want",
    [
        ("C33,527.36", "₹33,527.36"),
        ("C 840.00", "₹ 840.00"),
        ("CAFE NILOUFER AND BAKE", "CAFE NILOUFER AND BAKE"),  # letters must survive
        ("PACE HOSPITALS A UNIT", "PACE HOSPITALS A UNIT"),
    ],
)
def test_glyph_fix_only_touches_currency(raw, want):
    assert apply_glyph_fixes(raw, FIXES) == want


@pytest.mark.parametrize(
    "raw,want",
    [
        ("₹ 840.00", Decimal("840.00")),
        ("₹1,904.00", Decimal("1904.00")),
        ("₹6,67,000", Decimal("667000")),  # Indian lakh grouping
        ("+ ₹ 33,527.00", Decimal("33527.00")),
        ("", None),
        ("l", None),
    ],
)
def test_parse_amount(raw, want):
    assert parse_amount(raw) == want


@pytest.mark.parametrize(
    "raw,want",
    [("+ ₹ 20.14", True), ("₹ 840.00", False), ("1,234.00 Cr", True), ("₹0.00", False)],
)
def test_credit_marker(raw, want):
    assert is_credit_marker(raw) is want


@pytest.mark.parametrize(
    "raw,want",
    [
        # HDFC prints a card sitting in credit as a plain negative from 2023 on;
        # read as a positive due it throws the reconciliation out by twice the
        # balance. The Cr suffix and the leading + keep working alongside it.
        ("-7,253.23", Decimal("-7253.23")),
        ("₹ -7,253.23", Decimal("-7253.23")),
        ("− 1,904.00", Decimal("-1904.00")),  # true minus sign
        ("7,253.23", Decimal("7253.23")),
        ("6,627.00 CR", Decimal("-6627.00")),
        ("+ ₹ 33,527.00", Decimal("-33527.00")),
        # A hyphen must sit against the figure to be its sign, not merely
        # somewhere in the cell.
        ("Total - Dues 1,234.00", Decimal("1234.00")),
        ("1,234.00 -", Decimal("1234.00")),
    ],
)
def test_parse_signed_amount_reads_a_printed_minus(raw, want):
    assert parse_signed_amount(raw) == want


def test_leading_minus_does_not_reach_transaction_rows():
    """Only balance fields ask for a signed amount; rows keep their Dr/Cr column."""
    assert is_credit_marker("-7,253.23") is False
    assert parse_amount("-7,253.23") == Decimal("7253.23")


def test_credit_marker_recovered_for_exact_transaction_amount_only():
    debit = "15/05/2025 ZOMATO RESTAURANTS 302.13 Dr 3.00 Cr"
    credit = "30/05/2025 BBPS PAYMENT RECEIVED 29,154.89 Cr 0.00 Dr"
    assert not is_credit_amount_in_row(debit, Decimal("302.13"))
    assert is_credit_amount_in_row(credit, Decimal("29154.89"))


def test_currency_predicate_rejects_bare_numerals():
    # A due date's "04" and a year "2026" sit next to summary amounts; neither is one.
    assert is_currency_amount("₹1,904.00")
    assert not is_currency_amount("04")
    assert not is_currency_amount("2026")


def test_parse_date_is_format_pinned():
    assert parse_date("05/01/2026", ["%d/%m/%Y"]) == dt.date(2026, 1, 5)
    assert parse_date("04 Feb, 2026", ["%d %b, %Y"]) == dt.date(2026, 2, 4)
    # Day/month must never be guessed from an unlisted format.
    assert parse_date("2026-01-05", ["%d/%m/%Y"]) is None


def test_parse_time_and_int():
    assert parse_time("27/12/2025| 19:22") == dt.time(19, 22)
    assert parse_time("no time here") is None
    assert parse_int("+ 188") == 188
    assert parse_int("") is None


def _line(words, fixes=None):
    return Line(
        top=100.0,
        words=[Word(t, x0, x0 + 20, 100.0, 108.0) for t, x0 in words],
        glyph_fixes=fixes or {},
    )


def test_cell_assignment_uses_overlap_not_whitespace():
    ln = _line([("ZOMATONEW", 136.0), ("DELHI", 160.0), ("+2", 430.0), ("C", 520.0), ("156.05", 532.0)], FIXES)
    assert ln.cell((132.0, 420.0)) == "ZOMATONEW DELHI"
    assert ln.cell((420.0, 470.0)) == "+2"
    assert ln.cell((470.0, 562.0)) == "₹ 156.05"


def test_glyph_fix_applies_across_word_boundary():
    # The rupee glyph is its own word, so the digit it precedes is only visible
    # after neighbouring words are joined.
    ln = _line([("C", 520.0), ("840.00", 532.0)], FIXES)
    assert ln.cell((470.0, 562.0)) == "₹ 840.00"
    assert parse_amount(ln.cell((470.0, 562.0))) == Decimal("840.00")
