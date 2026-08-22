"""Regenerate the golden corpus for the card ledger.

Run after any deliberate parser change, then diff the JSON: an unexpected line
in that diff is a regression you would otherwise have shipped.

    python scripts/make_golden.py                   # every statement in the corpus
    python scripts/make_golden.py 202607_foo.pdf    # admit new statements to it

The corpus is drawn from the inbox — the source of truth for documents — plus
anything staged in samples/. A statement in the inbox joins the corpus only once
it has a golden file, so naming it here is how it gets in; see tests/corpus.py.

Bank-account statements are skipped. They are parsed by different code, and
their narrations are not frozen into git — tests/test_bank_corpus.py holds them
to their own arithmetic instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from sparser import parse_pdf  # noqa: E402

import corpus  # noqa: E402

GOLDEN = corpus.GOLDEN


def _named(names: list[str]) -> list[Path]:
    """Statements asked for by file name, wherever they live and however they
    are encrypted. Names that match nothing readable are reported, not ignored."""
    passwords = corpus._passwords()
    available = {source.name: source for source in corpus._sources()}
    out: list[Path] = []
    for name in names:
        source = available.get(name) or available.get(Path(name).name)
        if source is None:
            print(f"{name}: not in the inbox or samples/")
            continue
        readable = corpus._readable(source, passwords)
        if readable is None:
            print(f"{name}: encrypted, and no password on this machine opens it")
            continue
        if corpus.kind(readable) == "bank_account":
            print(f"{name}: a bank account statement — see tests/test_bank_corpus.py")
            continue
        out.append(readable)
    return out


def main(argv: list[str]) -> int:
    pdfs = _named(argv) if argv else list(corpus.CARD_PDFS)
    if not pdfs:
        print("no card statements to write goldens for")
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
    raise SystemExit(main(sys.argv[1:]))
