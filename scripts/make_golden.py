"""Regenerate the golden corpus from the card statements in samples/.

Run after any deliberate parser change, then diff the JSON: an unexpected line
in that diff is a regression you would otherwise have shipped.

    python scripts/make_golden.py

Bank-account PDFs in the same folder are skipped. They are parsed by different
code, and their narrations are not frozen into git — tests/test_bank_corpus.py
holds them to their own arithmetic instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pdfplumber  # noqa: E402

from sparser import parse_pdf  # noqa: E402
from sparser.doctype import document_kind  # noqa: E402

SAMPLES = ROOT / "samples"
GOLDEN = ROOT / "tests" / "golden"


def _is_card(pdf: Path) -> bool:
    with pdfplumber.open(pdf) as document:
        text = "\n".join((page.extract_text() or "") for page in document.pages)
    return document_kind(text)[0] != "bank_account"


def main() -> int:
    pdfs = [pdf for pdf in sorted(SAMPLES.glob("*.pdf")) if _is_card(pdf)]
    if not pdfs:
        print(f"no card statement PDFs in {SAMPLES}")
        return 1
    GOLDEN.mkdir(parents=True, exist_ok=True)

    rc = 0
    for pdf in pdfs:
        stmt = parse_pdf(pdf)
        data = json.loads(stmt.model_dump_json())
        data.pop("checks", None)  # checks are asserted separately, not frozen
        (GOLDEN / f"{pdf.stem}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        status = "ok" if stmt.ok else "FAILED VALIDATION"
        print(f"{pdf.name}: {len(stmt.transactions):>3} txns  {stmt.confidence:>6.0%}  {status}")
        if not stmt.ok:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
