"""IDFC prints the transaction time two different ways. Both must read.

Statements for the same account, months apart, break the date cell differently:

    2025            2026
    18 Nov 25       30 Jun 26 02:55
    18:24

The date used to be anchored to the end of its printed line, so the newer form
matched nothing, every row was skipped, and the statement arrived with a clean
period and header and *zero* transactions — a silent emptying rather than a
parse error. These tests pin both eras so it cannot regress into silence again.
"""
from __future__ import annotations

import datetime as dt

import pytest

from sparser.banks.idfc import _DATE, _dated


def _word(text: str, top: float, x0: float) -> dict:
    return {"text": text, "top": top, "bottom": top + 8, "x0": x0, "x1": x0 + 6 * len(text)}


def _cell(*lines: list[str]) -> list[dict]:
    """Word dicts for a cell printed as the given lines, top to bottom."""
    words = []
    for row, texts in enumerate(lines):
        x = 55.0
        for text in texts:
            words.append(_word(text, 100.0 + row * 10, x))
            x += 6 * len(text) + 3
    return words


@pytest.mark.parametrize("printed,expected", [
    ("18 Nov 25", dt.date(2025, 11, 18)),          # the date alone
    ("30 Jun 26 02:55", dt.date(2026, 6, 30)),     # date and time on one line
    ("30 Jun 26 2:55", dt.date(2026, 6, 30)),      # unpadded hour
    ("1 Jan 26 23:59:07", dt.date(2026, 1, 1)),    # seconds, single-digit day
])
def test_both_ways_idfc_prints_a_transaction_date_are_read(printed, expected):
    found = _DATE.match(printed)
    assert found, f"{printed!r} was not recognised as a transaction date"
    assert dt.datetime.strptime(found.group(1), "%d %b %y").date() == expected


@pytest.mark.parametrize("printed", [
    "10,467.18",        # an amount that drifted into the date column
    "Opening Balance",
    "02:55",            # a bare time is not a date
    "30 Jun 2026",      # a four-digit year is the period header, not a row
    "",
])
def test_what_is_not_a_transaction_date_stays_rejected(printed):
    assert not _DATE.match(printed)


def test_the_2025_layout_reads_the_date_above_its_time():
    assert _dated(_cell(["18", "Nov", "25"], ["18:24"])) == dt.date(2025, 11, 18)


def test_the_2026_layout_reads_the_date_beside_its_time():
    assert _dated(_cell(["30", "Jun", "26", "02:55"])) == dt.date(2026, 6, 30)


def test_a_cell_with_no_date_yields_none_rather_than_guessing():
    assert _dated(_cell(["Opening", "Balance"])) is None
    assert _dated([]) is None
