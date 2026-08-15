"""Which sample PDFs belong to which corpus.

samples/ holds both kinds of statement, and the two ledgers are parsed by
completely separate code. Splitting the folder by the same classifier the ingest
pipeline uses keeps each corpus test honest: a card test that tripped over a
bank statement would be reporting a failure the product does not have.
"""
from __future__ import annotations

from pathlib import Path

import pdfplumber

from sparser.doctype import document_kind

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
GOLDEN = ROOT / "tests" / "golden"


def kind(pdf: Path) -> str:
    with pdfplumber.open(pdf) as document:
        text = "\n".join((page.extract_text() or "") for page in document.pages)
    return document_kind(text)[0]


def _split() -> tuple[list[Path], list[Path]]:
    pdfs = sorted(SAMPLES.glob("*.pdf")) if SAMPLES.exists() else []
    banks = [pdf for pdf in pdfs if kind(pdf) == "bank_account"]
    return [pdf for pdf in pdfs if pdf not in banks], banks


CARD_PDFS, BANK_PDFS = _split()
