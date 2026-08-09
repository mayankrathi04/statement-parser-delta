"""Word boxes -> lines -> column cells.

This is the core of the parser. We never split text on whitespace; every cell
assignment is decided by x-overlap against column bands, which is what makes
multi-word merchant names and right-aligned amounts land in the right place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .normalize import apply_glyph_fixes, squash


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    top: float
    words: list[Word] = field(default_factory=list)
    page: int = 0
    glyph_fixes: dict[str, str] = field(default_factory=dict)

    def _fix(self, s: str) -> str:
        # Applied after joining, never per word: the broken rupee glyph is emitted
        # as its own word ("C", "840.00"), so the digit it precedes is only
        # visible once neighbouring words sit in the same string.
        return apply_glyph_fixes(s, self.glyph_fixes) if self.glyph_fixes else s

    @property
    def text(self) -> str:
        return self._fix(squash(" ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))))

    def cell(self, band: tuple[float, float]) -> str:
        """Words whose horizontal overlap with `band` exceeds half their width."""
        lo, hi = band
        picked = []
        for w in sorted(self.words, key=lambda w: w.x0):
            overlap = min(w.x1, hi) - max(w.x0, lo)
            width = max(w.x1 - w.x0, 0.01)
            if overlap > 0 and (overlap / width) >= 0.5:
                picked.append(w.text)
        return self._fix(squash(" ".join(picked)))

    def cells(self, bands: dict[str, tuple[float, float]]) -> dict[str, str]:
        return {name: self.cell(b) for name, b in bands.items()}


def extract_lines(
    page,
    page_no: int,
    y_tolerance: float = 2.0,
    glyph_fixes: Optional[dict[str, str]] = None,
) -> list[Line]:
    """Cluster pdfplumber words into visual lines by their `top` coordinate."""
    raw = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    fixes = glyph_fixes or {}
    words = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"]) for w in raw]

    words.sort(key=lambda w: (w.top, w.x0))
    lines: list[Line] = []
    for w in words:
        if lines and abs(w.top - lines[-1].top) <= y_tolerance:
            lines[-1].words.append(w)
        else:
            lines.append(Line(top=w.top, words=[w], page=page_no, glyph_fixes=fixes))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
    return lines


def find_label(lines: Iterable[Line], label: str) -> Optional[tuple[Line, float, float]]:
    """Locate a multi-word label; return its line and x-extent.

    Labels wrap ("PAYMENTS/CREDITS" / "RECEIVED" on separate lines), so we match
    on the first line that contains the label's full word sequence.

    Matching is case-sensitive on purpose: marketing copy elsewhere on the page
    ("For hassle free payments register for...") otherwise steals the anchor from
    the real "Payments" column heading.
    """
    target = label.split()
    for ln in lines:
        toks = [w.text for w in ln.words]
        for i in range(len(toks) - len(target) + 1):
            if toks[i : i + len(target)] == target:
                span = ln.words[i : i + len(target)]
                return ln, span[0].x0, span[-1].x1
    return None


def amounts_row_below(
    lines: list[Line], label: str, index: int, predicate, *, dy: float = 40.0, min_values: int = 2
) -> Optional[str]:
    """Pick the n-th value from a row of figures printed beneath a row of labels.

    Some issuers lay the summary out as an equation — "Previous Balance -
    Payments - Credits + Purchase = Total" — with the figures on the line below.
    The labels are compressed and the figures spread wide, so matching by x
    silently picks a neighbour's number. Both rows are ordered left to right,
    though, so position is the reliable link, exactly as a person reads it.
    """
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line = hit[0]
    for ln in lines:
        if ln.page != lab_line.page:
            continue
        if not (0 < ln.top - lab_line.top <= dy):
            continue
        values = [t for t, _ in merged_tokens(ln) if predicate(t)]
        if len(values) >= min_values:
            return values[index] if -len(values) <= index < len(values) else None
    return None


def value_below(
    lines: list[Line],
    label: str,
    *,
    dy: float = 70.0,
    dx: float = 60.0,
    predicate=None,
) -> Optional[str]:
    """Find the value rendered underneath a label in a boxed summary panel.

    Summary panels are laid out as a header row of labels and a value row below.
    We anchor on the label's x-centre and take the nearest qualifying token below
    it, which survives column-width changes between card variants.
    """
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line, lx0, lx1 = hit
    lcx = (lx0 + lx1) / 2

    best: Optional[tuple[float, str]] = None
    for ln in lines:
        if ln.page != lab_line.page:
            continue
        gap = ln.top - lab_line.top
        if not (0 < gap <= dy):
            continue
        for text, cx in merged_tokens(ln):
            if abs(cx - lcx) > dx:
                continue
            if predicate and not predicate(text):
                continue
            score = gap + abs(cx - lcx) * 0.1
            if best is None or score < best[0]:
                best = (score, text)
    return best[1] if best else None


def merged_tokens(line: Line, max_gap: float = 4.0) -> list[tuple[str, float]]:
    """Glue words separated by less than a space width into one token.

    "Rs." and "989.00" and "Dr" are three words but one value; "₹33,527.36" and
    "₹33,547.14" sit far apart in the same panel and must stay two. Merging by
    physical gap distinguishes them without knowing either layout in advance.
    """
    out: list[list] = []
    for w in line.words:
        if out and w.x0 - out[-1][2] <= max_gap:
            out[-1][0] += " " + w.text
            out[-1][2] = w.x1
        else:
            out.append([w.text, w.x0, w.x1])
    return [(line._fix(t), (a + b) / 2) for t, a, b in out]


def text_below(lines: list[Line], label: str, *, dy: float = 40.0, dx: float = 80.0) -> Optional[str]:
    """Whole text of the nearest line below a label, within an x window."""
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line, lx0, lx1 = hit
    lcx = (lx0 + lx1) / 2
    best: Optional[tuple[float, str]] = None
    for ln in lines:
        if ln.page != lab_line.page:
            continue
        gap = ln.top - lab_line.top
        if not (0 < gap <= dy):
            continue
        near = [w.text for w in ln.words if abs(w.cx - lcx) <= dx]
        if near and (best is None or gap < best[0]):
            best = (gap, ln._fix(squash(" ".join(near))))
    return best[1] if best else None


# Issuers write dates every which way: "04 Feb, 2026" (HDFC), "August 5, 2026"
# (ICICI), "04/08/2026" (Axis). All three must be recoverable from a joined run.
_DATE_RUN = re.compile(
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{1,2}\s+[A-Za-z]{3,}[,.]?\s+\d{4}"
    r"|[A-Za-z]{3,}\s+\d{1,2},?\s+\d{4}"
)


def first_date_run(text: str) -> Optional[str]:
    """Pull the date out of a string carrying trailing furniture.

    Issuers append bullet glyphs and footnote marks to the same text run
    ("August 5, 2026 l"), which defeats a strict strptime.
    """
    m = _DATE_RUN.search(text or "")
    return m.group(0) if m else None


def date_below(lines: list[Line], label: str, *, dy: float = 70.0, dx: float = 80.0) -> Optional[str]:
    """Dates render as three separate words ("04", "Feb,", "2026").

    A single-token search cannot see them, so we join the words near the label's
    x-centre and pull the date run out of the joined string.
    """
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line, lx0, lx1 = hit
    lcx = (lx0 + lx1) / 2

    best: Optional[tuple[float, str]] = None
    for ln in lines:
        if ln.page != lab_line.page:
            continue
        gap = ln.top - lab_line.top
        if not (0 < gap <= dy):
            continue
        near = [w for w in ln.words if abs(w.cx - lcx) <= dx]
        if not near:
            continue
        m = _DATE_RUN.search(squash(" ".join(w.text for w in near)))
        if m and (best is None or gap < best[0]):
            best = (gap, m.group(0))
    return best[1] if best else None


def value_left_of(lines: list[Line], label: str) -> Optional[str]:
    """Value on the same line, to the left of a label."""
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line, lx0, _ = hit
    return squash(" ".join(w.text for w in lab_line.words if w.x1 <= lx0 + 0.5)) or None


def value_right_of(
    lines: list[Line], label: str, *, band: Optional[tuple[float, float]] = None
) -> Optional[str]:
    """Value on the same line, to the right of a label (key/value header block)."""
    hit = find_label(lines, label)
    if not hit:
        return None
    lab_line, _, lx1 = hit
    picked = [w.text for w in lab_line.words if w.x0 >= lx1 - 0.5]
    if band:
        lo, hi = band
        picked = [w.text for w in lab_line.words if w.x0 >= lx1 - 0.5 and lo <= w.cx <= hi]
    return squash(" ".join(picked)) or None
