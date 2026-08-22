"""Which statements the regression corpus is built from, and where they live.

The inbox is the source of truth for documents. It holds every statement exactly
as the issuer sent it, which means most of them are encrypted — so the corpus
opens each one with the passwords already recorded in the local database and
hands the tests a decrypted copy in a temporary folder. Nothing is ever written
back into the inbox, and no statement is stored twice on disk.

``samples/`` is a staging area, not a second archive: drop a PDF there while
designing a parser for a statement the inbox does not have yet, and the corpus
picks it up alongside the rest.

A checkout with no inbox, no database or no readable statements yields empty
lists and every corpus test skips, so CI stays green without any of this.
"""
from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pdfplumber

from sparser import accounts, inbox
from sparser.decrypt import DecryptError, candidate_passwords, decrypt_to, is_encrypted
from sparser.doctype import document_kind

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
GOLDEN = ROOT / "tests" / "golden"
DB = Path(__file__).resolve().parent.parent / "data" / "statements.db"

_UNLOCKED = Path(tempfile.mkdtemp(prefix="sparser-corpus-"))
atexit.register(shutil.rmtree, _UNLOCKED, True)


def _passwords() -> list[str]:
    """Every password the local install already knows, plus what the saved
    profiles imply. Exactly the list the ingest pipeline itself would try."""
    if not DB.is_file():
        return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        known = list(accounts.all_card_passwords(conn).values())
        known += list(accounts.all_bank_passwords(conn).values())
        derived = [
            password
            for profile in accounts.all_profiles(conn)
            for password in candidate_passwords(profile["full_name"], profile["dob"])
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return list(dict.fromkeys(known + derived))


def _readable(pdf: Path, passwords: list[str]) -> Path | None:
    """``pdf`` if it opens as-is, a decrypted copy if a known password fits,
    ``None`` if nothing on this machine can open it."""
    if not is_encrypted(pdf):
        return pdf
    if not passwords:
        return None
    target = _UNLOCKED / pdf.name
    if target.is_file():
        return target
    try:
        opened, _ = decrypt_to(pdf, target, passwords)
        return opened
    except (DecryptError, OSError):
        return None


def _sources() -> list[Path]:
    """Statement PDFs by name, the inbox first, then anything staged in
    samples/ that the inbox does not already have."""
    found: dict[str, Path] = {p.name: p for p in inbox.pdfs(inbox.root())}
    if SAMPLES.is_dir():
        for pdf in sorted(SAMPLES.glob("*.pdf")):
            found.setdefault(pdf.name, pdf)
    return [found[name] for name in sorted(found)]


def kind(pdf: Path) -> str:
    with pdfplumber.open(pdf) as document:
        text = "\n".join((page.extract_text() or "") for page in document.pages)
    return document_kind(text)[0]


def _half(source: Path) -> str | None:
    """``cards`` or ``bank`` if this path sits in the inbox, else ``None``."""
    for parent in source.parents:
        if parent.parent == inbox.root() and parent.name in inbox.KINDS:
            return parent.name
    return None


def _in_corpus(source: Path, goldens: set[str]) -> bool:
    """Whether a statement in the inbox belongs to the regression set.

    The two ledgers earn their place differently, because their tests differ.

    A card test freezes the exact parse against a golden file, so the card set
    has to be deliberate — the inbox holds hundreds of card statements and the
    corpus is not all of them. The golden *is* the membership list, and
    `scripts/make_golden.py <name>` is how a statement joins.

    A bank test only asserts that the arithmetic reconciles, which is cheap and
    true of every account statement that ever parsed. So the bank set is every
    statement filed under an account — and next month's statement joining
    automatically is the point: a new one that does not reconcile should turn
    the suite red. A statement still in ``_unsorted`` never parsed and is
    therefore not something the parser claims to handle.
    """
    if source.parent == SAMPLES:
        return True                      # staged by hand; that is what staging is for
    if source.stem in goldens:
        return True                      # the golden is the membership list
    # An account statement earns its place by having parsed: one still sitting
    # in _unsorted is not something any parser claims to handle.
    return _half(source) == inbox.BANK and source.parent.name != inbox.UNSORTED


def _split() -> tuple[list[Path], list[Path]]:
    """Split the corpus the way the ingest pipeline does.

    Card and account statements are parsed by completely separate code, so a
    card test that tripped over a bank statement would be reporting a failure
    the product does not have.
    """
    goldens = {path.stem for path in GOLDEN.glob("*.json")} if GOLDEN.is_dir() else set()
    passwords = _passwords()

    cards: list[Path] = []
    banks: list[Path] = []
    for source in _sources():
        if not _in_corpus(source, goldens):
            continue
        readable = _readable(source, passwords)
        if readable is None:
            continue
        # A statement in the inbox has already been classified — that is what put
        # it under cards/ or bank/. Only a staged sample has to be read to find
        # out, and reading every PDF here just to sort it is the single most
        # expensive thing collection could do.
        half = _half(source)
        is_bank = half == inbox.BANK if half else kind(readable) == "bank_account"
        (banks if is_bank else cards).append(readable)
    return cards, banks


CARD_PDFS, BANK_PDFS = _split()
