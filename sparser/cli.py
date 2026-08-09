"""Command line entry point.

    python -m sparser parse  statement.pdf -o out.xlsx
    python -m sparser import samples/*.pdf --db statements.db
    python -m sparser fetch  --dest inbox --name "<your name>" --dob DD/MM/YYYY
    python -m sparser serve  --db statements.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from pathlib import Path

from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from .engine import NoTemplateMatch, parse_pdf
from .output import WRITERS
from .schema import Statement

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _report(stmt: Statement, verbose: bool) -> None:
    mark = f"{GREEN}OK{RESET}" if stmt.ok else f"{RED}FAILED{RESET}"
    print(f"\n{stmt.source_file}  [{mark}]  confidence {stmt.confidence:.0%}")
    print(f"  {stmt.issuer} {stmt.product or ''} {stmt.account_masked or ''}".rstrip())
    print(f"  period {stmt.period_start} -> {stmt.period_end}   {len(stmt.transactions)} transactions")
    for c in stmt.checks:
        if c.passed and not verbose:
            continue
        colour = GREEN if c.passed else (RED if c.severity == "error" else YELLOW)
        print(f"    {colour}{'PASS' if c.passed else 'FAIL'}{RESET} {c.name}: {DIM}{c.detail}{RESET}")


def _creds(args):
    dob = dt.datetime.strptime(args.dob, "%d/%m/%Y").date() if args.dob else None
    return ([args.password] if args.password else []) + candidate_passwords(
        args.name, dob, args.card_last4
    )


def _resolve(path: Path, args, tmpdir: Path) -> Path:
    """Returns a readable path; the original file is never modified."""
    if not is_encrypted(path):
        return path
    dest = tmpdir / f"dec_{path.name}"
    out, used = decrypt_to(path, dest, _creds(args))
    if used and not args.password:
        print(f"{DIM}  decrypted {path.name} with a derived password{RESET}")
    return out


def _load(pdfs, args, tmpdir):
    """Yield parsed statements, reporting per-file failures without aborting."""
    for pdf in pdfs:
        if not pdf.exists():
            print(f"{RED}missing:{RESET} {pdf}", file=sys.stderr)
            continue
        try:
            stmt = parse_pdf(_resolve(pdf, args, tmpdir))
            stmt.source_file = pdf.name
            yield pdf, stmt
        except (NoTemplateMatch, DecryptError) as exc:
            print(f"{RED}{pdf.name}: {exc}{RESET}", file=sys.stderr)


def cmd_parse(args) -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        for pdf, stmt in _load(args.pdfs, args, Path(td)):
            _report(stmt, args.verbose)
            failures += 0 if stmt.ok else 1
            if args.output and len(args.pdfs) == 1:
                dest = args.output
            else:
                outdir = args.outdir or pdf.parent
                outdir.mkdir(parents=True, exist_ok=True)
                dest = outdir / f"{pdf.stem}.{args.format}"
            WRITERS[args.format](stmt, dest)
            print(f"  {DIM}wrote{RESET} {dest}")
    return 1 if (failures and args.strict) else 0


def _expand(paths) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        out += sorted(p.glob("*.pdf")) if p.is_dir() else [p]
    return out


def cmd_import(args) -> int:
    """Import through the recorded pipeline, so CLI runs show up in the UI too."""
    from . import pipeline, store

    pdfs = _expand(args.pdfs)
    if not pdfs:
        print(f"{RED}no PDFs found{RESET}", file=sys.stderr)
        return 1

    creds = {
        "password": args.password, "name": args.name, "card_last4": args.card_last4,
        "dob": dt.datetime.strptime(args.dob, "%d/%m/%Y").date() if args.dob else None,
    }
    run_id = pipeline.run_import(args.db, pdfs, creds, force=args.force)

    conn = store.connect(args.db)
    detail = pipeline.run_detail(conn, run_id)
    failures = 0
    for f in detail["files"]:
        if f["status"] == "ok":
            print(
                f"{GREEN}imported{RESET} {f['filename']}: {f['txn_count']} txns  "
                f"{f['card'] or ''} {DIM}{f['template_id']} · {f['confidence']:.0%}{RESET}"
            )
        else:
            failures += 1
            print(f"{RED}failed{RESET} {f['filename']}: {DIM}{(f['error'] or '')[:120]}{RESET}")

    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM cards) c, (SELECT COUNT(*) FROM statements) s,"
        " (SELECT COUNT(*) FROM transactions) t"
    ).fetchone()
    print(
        f"\n{args.db}: {counts['c']} cards, {counts['s']} statements, {counts['t']} transactions"
    )
    conn.close()
    return 1 if failures and args.strict else 0


def cmd_fetch(args) -> int:
    from . import mailbox

    accounts = mailbox.accounts_from_env()
    if not accounts:
        print(
            f"{RED}No mailboxes configured.{RESET}\n"
            "  Create an app password at https://myaccount.google.com/apppasswords\n"
            '  then: export SPARSER_GMAIL="you@gmail.com:apppassword,other@gmail.com:apppassword"',
            file=sys.stderr,
        )
        return 2

    dest = args.dest
    pdfs = mailbox.fetch_all(accounts, dest, months=args.months)
    print(f"\n{len(pdfs)} new statement PDF(s) in {dest}")
    if not pdfs or args.no_import:
        return 0

    args.pdfs = [dest]
    return cmd_import(args)


def cmd_serve(args) -> int:
    from .server import serve

    serve(
        args.db, host=args.host, port=args.port,
        open_browser=not args.no_browser, inbox=args.inbox, reload=args.reload,
    )
    return 0


def _add_credentials(p):
    p.add_argument("--password", help="PDF password")
    p.add_argument("--name", help="cardholder name, for deriving the password")
    p.add_argument("--dob", help="date of birth DD/MM/YYYY, for deriving the password")
    p.add_argument("--card-last4", help="last 4 digits, for deriving the password")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sparser", description="Bank & credit card statement parser")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("parse", help="parse PDFs to xlsx/csv/json")
    p.add_argument("pdfs", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, help="output file (single input only)")
    p.add_argument("--outdir", type=Path)
    p.add_argument("-f", "--format", choices=sorted(WRITERS), default="xlsx")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--strict", action="store_true")
    _add_credentials(p)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("import", help="parse PDFs into the SQLite store")
    p.add_argument("pdfs", nargs="+", type=Path)
    p.add_argument("--db", type=Path, default=Path("statements.db"))
    p.add_argument("--force", action="store_true", help="import even if validation fails")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--strict", action="store_true")
    _add_credentials(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("fetch", help="download statements from Gmail, then import")
    p.add_argument("--dest", type=Path, default=Path("inbox"))
    p.add_argument("--db", type=Path, default=Path("statements.db"))
    p.add_argument("--months", type=int, default=12, help="how far back to search")
    p.add_argument("--no-import", action="store_true", help="download only")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--strict", action="store_true")
    _add_credentials(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("serve", help="run the analytics dashboard")
    p.add_argument("--db", type=Path, default=Path("statements.db"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--inbox", type=Path, default=Path("inbox"))
    p.add_argument("--reload", action="store_true", help="auto-reload for development")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
