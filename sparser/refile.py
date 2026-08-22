"""File the statements that are still sitting in the inbox's ``_unsorted``.

A PDF lands in ``_unsorted`` because nothing but the mail headers was known when
it arrived, and it stays there when the parse that would have identified it did
not succeed — the file was encrypted with a password nobody had saved yet, or no
parser recognised the issuer at the time.

Both of those are temporary. Passwords get saved and parsers get written, so a
file that was stuck last month may be perfectly readable today. This re-reads
everything in ``_unsorted`` and moves what it can now identify.

Nothing here writes to the ledger. Filing a statement on disk and importing its
transactions are separate decisions, and this only makes the first one.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber

from . import accounts, bank_store, inbox, store
from .banks import UnsupportedBankStatement, parse_bank_pdf
from .decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from .doctype import document_kind
from .engine import NoTemplateMatch, TemplateError, parse_pdf

log = logging.getLogger("sparser.refile")


@dataclass
class Candidate:
    """One PDF in ``_unsorted`` and what could be made of it."""

    path: Path
    kind: Optional[str] = None
    label: Optional[str] = None
    reason: Optional[str] = None      # why it cannot be filed, if it cannot
    in_ledger: Optional[str] = None   # the source file its statement is stored under
    txn_count: int = 0
    moved_to: Optional[Path] = None

    @property
    def fileable(self) -> bool:
        return self.label is not None


def _passwords(conn) -> list[str]:
    """Every password this install knows — the same list the pipelines try."""
    known = list(accounts.all_card_passwords(conn).values())
    known += list(accounts.all_bank_passwords(conn).values())
    derived = [
        password
        for profile in accounts.all_profiles(conn)
        for password in candidate_passwords(profile["full_name"], profile["dob"])
    ]
    return list(dict.fromkeys(known + derived))


def _ledger_source(conn, statement, is_bank: bool) -> Optional[str]:
    """The file name the ledger already holds this billing cycle under, if any.

    Answers the question that decides what to do with a stuck file: are these
    transactions already counted? A statement delivered twice under two
    attachment names is in the ledger under whichever arrived last.
    """
    if is_bank:
        row = conn.execute(
            """SELECT s.source_file FROM bank_statements s
               JOIN bank_accounts a ON a.id = s.account_id
               WHERE a.account_fingerprint = ? AND s.period_start = ? AND s.period_end = ?""",
            (statement.account_fingerprint,
             statement.period_start.isoformat(), statement.period_end.isoformat()),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT s.source_file FROM statements s JOIN cards c ON c.id = s.card_id
               WHERE c.masked_number = ? AND s.period_start = ? AND s.period_end = ?""",
            (statement.account_masked, str(statement.period_start), str(statement.period_end)),
        ).fetchone()
    return row["source_file"] if row else None


def inspect(db_path: Path, root: Optional[Path] = None) -> list[Candidate]:
    """Re-read every unsorted PDF and work out where each one belongs."""
    root = Path(root or inbox.root())
    conn = store.connect(db_path)
    try:
        passwords = _passwords(conn)
        scratch = Path(tempfile.mkdtemp(prefix="sparser-refile-"))
        out: list[Candidate] = []
        for pdf in sorted(root.rglob(f"{inbox.UNSORTED}/*")):
            if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
                continue
            out.append(_inspect_one(conn, pdf, passwords, scratch))
        return out
    finally:
        conn.close()


def _inspect_one(conn, pdf: Path, passwords: list[str], scratch: Path) -> Candidate:
    found = Candidate(pdf)
    try:
        # Everything is inside the guard, the encryption probe included: a file
        # damaged enough that pikepdf cannot even read its header is exactly the
        # kind of thing that ends up stuck here, and one of them must not take
        # the whole sweep down with it.
        workfile = pdf
        if is_encrypted(pdf):
            try:
                workfile, _ = decrypt_to(pdf, scratch / pdf.name, passwords)
            except DecryptError:
                found.reason = "encrypted, and no password this install knows opens it"
                return found
        with pdfplumber.open(workfile) as document:
            text = "\n".join((page.extract_text() or "") for page in document.pages)
        is_bank = document_kind(text)[0] == "bank_account"
        if is_bank:
            statement = parse_bank_pdf(workfile)
            found.kind, found.label = inbox.BANK, bank_store.account_label(conn, statement)
        else:
            statement = parse_pdf(workfile)
            found.kind, found.label = inbox.CARDS, store.card_label(conn, statement)
        found.txn_count = len(statement.transactions)
        found.in_ledger = _ledger_source(conn, statement, is_bank)
    except (UnsupportedBankStatement, NoTemplateMatch, TemplateError) as exc:
        found.reason = str(exc)
    except Exception as exc:  # a bad PDF must not stop the sweep
        found.reason = f"{type(exc).__name__}: {exc}"
    return found


def refile(db_path: Path, root: Optional[Path] = None) -> list[Candidate]:
    """:func:`inspect`, then move everything it could identify."""
    root = Path(root or inbox.root())
    found = inspect(db_path, root)
    for candidate in found:
        if candidate.fileable:
            candidate.moved_to = inbox.file_under(
                candidate.path, root, candidate.kind, candidate.label
            )
            log.info("filed %s under %s", candidate.path.name, candidate.moved_to.parent.name)
    return found
