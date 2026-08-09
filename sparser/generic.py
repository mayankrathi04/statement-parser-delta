"""Template-free parsing for layouts we have never seen.

A template is faster and safer when it exists, but the library can only grow if
an unknown statement still yields something. This module reproduces, mechanically,
the reasoning a human applies when first shown a statement:

  1. Find the runs of lines that *look* like transaction rows — a date on the
     left, a money amount on the right, several in a row. Real tables repeat;
     prose does not.
  2. Recover columns from those rows by whitespace projection: mark every x
     position covered by a word, then cut the table at the vertical gutters that
     survive across the whole run. This needs no header text, which is what lets
     it work on a bank whose column is called "Narration" or "Base NeuCoins*".
  3. Name the columns by what they contain, not what they are titled — the
     column that parses as dates is the date, the rightmost money column is the
     amount, the widest prose column is the description.

Output carries lower confidence by construction and still faces the same
arithmetic validators, so a wrong guess surfaces as a failed check rather than
as clean-looking bad data.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pdfplumber

from . import geometry as geo
from .normalize import is_credit_marker, parse_amount, parse_date, parse_time, squash
from .schema import DocType, Statement, Transaction, TxnType

DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d %b, %Y", "%d-%b-%y",
    "%Y-%m-%d", "%m/%d/%Y",
]
_DATE_RX = re.compile(
    r"^\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9},?[ -]\d{2,4}|\d{4}-\d{2}-\d{2})"
)
_MONEY_RX = re.compile(r"\d[\d,]*\.\d{2}\s*(?:Cr|Dr)?$", re.I)

# Two rows is enough to call something a table. Real statements carry short
# blocks — an international section with a single foreign charge, a page-1
# summary block with a payment — and a threshold of three silently drops them.
# Precision is recovered by the arithmetic validators, not by this constant.
MIN_RUN = 2
MIN_GUTTER = 5.0     # pt of continuous whitespace that counts as a column break
MAX_WRAP_GAP = 2     # non-row lines tolerated inside a run (wrapped descriptions)
MAX_ORPHAN_GAP = 20.0


def infer_glyph_fixes(lines: list[geo.Line]) -> dict[str, str]:
    """Detect a currency glyph broken by a bad ToUnicode CMap.

    A font that maps ₹ to some ASCII letter emits that letter as its own word
    immediately left of a money token, over and over. One letter doing that
    dozens of times is a broken glyph, not English.
    """
    counts: Counter[str] = Counter()
    for ln in lines:
        for a, b in zip(ln.words, ln.words[1:]):
            if len(a.text) == 1 and a.text.isalpha() and a.text.isupper():
                if b.x0 - a.x1 < 6 and _MONEY_RX.search(b.text):
                    counts[a.text] += 1
    return {ch: "₹" for ch, n in counts.items() if n >= 5}


def _looks_like_row(ln: geo.Line) -> bool:
    words = ln.words
    if len(words) < 2:
        return False
    # The date is usually the first word, but page furniture printed in the
    # margin (ICICI stamps a "100%" gauge at x=49) can precede it, so allow the
    # date to appear among the first few words rather than strictly at index 0.
    has_date = any(_DATE_RX.match(w.text) for w in words[:4])
    has_money = any(_MONEY_RX.search(w.text) for w in words[-3:])
    return has_date and has_money


Run = tuple[list[geo.Line], list[geo.Line]]  # (row-like lines, every line in the block)


def find_runs(lines: list[geo.Line]) -> list[Run]:
    """Consecutive row-like lines, keeping the wrapped lines between them.

    The interleaved non-row lines are carried along rather than discarded: a
    description that wraps onto its own line is part of the transaction, and
    dropping it is how a parser silently loses half a merchant name.
    """
    runs: list[Run] = []
    rows: list[geo.Line] = []
    block: list[geo.Line] = []
    slack = 0

    def flush():
        if len(rows) >= MIN_RUN:
            runs.append((list(rows), list(block)))
        rows.clear()
        block.clear()

    for ln in lines:
        if _looks_like_row(ln):
            rows.append(ln)
            block.append(ln)
            slack = 0
        elif rows:
            slack += 1
            if slack > MAX_WRAP_GAP:
                flush()
                slack = 0
            else:
                block.append(ln)
    flush()
    return runs


def columns_from(rows: list[geo.Line], page_width: float) -> list[tuple[float, float]]:
    """Whitespace projection: cut the table wherever no row places ink."""
    res = 0.5
    n = int(page_width / res) + 1
    ink = bytearray(n)
    for ln in rows:
        for w in ln.words:
            for i in range(max(0, int(w.x0 / res)), min(n, int(w.x1 / res) + 1)):
                ink[i] = 1

    bands: list[tuple[float, float]] = []
    start = None
    gap = 0
    for i in range(n):
        if ink[i]:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap * res >= MIN_GUTTER:
                bands.append((start * res, (i - gap) * res))
                start = None
    if start is not None:
        bands.append((start * res, (n - 1) * res))
    return bands


def _classify(bands: list[tuple[float, float]], rows: list[geo.Line]) -> dict[str, tuple[float, float]]:
    """Name columns by their contents."""
    total = max(len(rows), 1)
    profile = []
    for b in bands:
        cells = [ln.cell(b) for ln in rows]
        filled = [c for c in cells if c]
        dates = sum(1 for c in filled if _DATE_RX.match(c))
        money = sum(1 for c in filled if _MONEY_RX.search(c))
        avg_len = sum(len(c) for c in filled) / max(len(filled), 1)
        # Ratios are over every row, not just the populated ones. A sparse column
        # that happens to hold money in its one filled cell — ICICI's foreign
        # currency leg — would otherwise score a perfect 1.0 and be mistaken for
        # the amount column.
        profile.append(
            {
                "band": b,
                "n": len(filled),
                "dates": dates / total,
                "money": money / total,
                "len": avg_len,
            }
        )

    out: dict[str, tuple[float, float]] = {}
    dated = [p for p in profile if p["dates"] > 0.8]
    if dated:
        out["date"] = dated[0]["band"]

    monied = [p for p in profile if p["money"] > 0.8]
    if monied:
        # Rightmost near-complete money column is the amount. Only when two such
        # columns sit side by side is the right one a running balance — a sparse
        # neighbour is a currency leg or a rewards figure, not a balance.
        out["amount"] = monied[-1]["band"]
        if len(monied) >= 2:
            out["amount"], out["balance"] = monied[-2]["band"], monied[-1]["band"]

    text = [
        p
        for p in profile
        if p["band"] not in out.values() and p["n"] and p["dates"] == 0 and p["money"] == 0
    ]
    if text:
        out["description"] = max(text, key=lambda p: p["len"])["band"]
    return out


def parse_generic(pdf_path: str | Path) -> Statement:
    pdf_path = Path(pdf_path)
    lines: list[geo.Line] = []
    width = 595.0
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            width = max(width, float(page.width))
            lines += geo.extract_lines(page, i, 2.0, None)

    fixes = infer_glyph_fixes(lines)
    for ln in lines:
        ln.glyph_fixes = fixes

    runs = find_runs(lines)

    # A table that continues onto the next page can leave a single row stranded,
    # below the threshold that makes a run. Such a row still belongs to a table
    # we already understand, so adopt it into the run whose columns it fits.
    claimed = {id(ln) for rows, _ in runs for ln in rows}
    strays = [ln for ln in lines if _looks_like_row(ln) and id(ln) not in claimed]
    for stray in strays:
        for rows, block in runs:
            cols = _classify(columns_from(rows, width), rows)
            if "date" not in cols or "amount" not in cols:
                continue
            if _DATE_RX.match(stray.cell(cols["date"])) and parse_amount(stray.cell(cols["amount"])):
                rows.append(stray)
                block.append(stray)
                break

    txns: list[Transaction] = []
    for rows, block in runs:
        # Project columns from the row-like lines only: a wrapped description is
        # not constrained to the table's columns and would smear the gutters.
        cols = _classify(columns_from(rows, width), rows)
        if "date" not in cols or "amount" not in cols or "description" not in cols:
            continue

        wraps = [ln for ln in block if ln not in rows]
        for ln in rows:
            raw_date = ln.cell(cols["date"])
            m = _DATE_RX.match(raw_date)
            when = parse_date(m.group(1), DATE_FORMATS) if m else None
            amount_cell = ln.cell(cols["amount"])
            amount = parse_amount(amount_cell)
            if when is None or amount is None:
                continue

            near = sorted(
                (w for w in wraps if abs(w.top - ln.top) <= MAX_ORPHAN_GAP),
                key=lambda w: w.top,
            )
            owned = [
                w for w in near
                if min(rows, key=lambda r: abs(r.top - w.top)) is ln
            ]
            parts = [w.cell(cols["description"]) for w in owned if w.top < ln.top]
            parts.append(ln.cell(cols["description"]))
            parts += [w.cell(cols["description"]) for w in owned if w.top > ln.top]

            txns.append(
                Transaction(
                    date=when,
                    time=parse_time(raw_date),
                    description=squash(" ".join(p for p in parts if p)),
                    amount=amount,
                    type=TxnType.CREDIT if is_credit_marker(amount_cell) else TxnType.DEBIT,
                    balance=parse_amount(ln.cell(cols["balance"])) if "balance" in cols else None,
                    page=ln.page,
                    raw=ln.text,
                )
            )

    return Statement(
        template_id="generic",
        issuer="unknown",
        doc_type=DocType.CREDIT_CARD,
        transactions=txns,
        source_file=pdf_path.name,
    )
