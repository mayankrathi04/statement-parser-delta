"""Regenerate the golden corpus from samples/.

Run after any deliberate parser change, then diff the JSON: an unexpected line
in that diff is a regression you would otherwise have shipped.

    python scripts/make_golden.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sparser import parse_pdf  # noqa: E402

SAMPLES = ROOT / "samples"
GOLDEN = ROOT / "tests" / "golden"


def main() -> int:
    pdfs = sorted(SAMPLES.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs in {SAMPLES}")
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
