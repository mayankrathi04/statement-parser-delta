"""Where statement PDFs live on disk, and the one place that decides it.

The layout is two levels and no hashes::

    inbox/
      cards/
        _unsorted/                     just downloaded, not parsed yet
        hdfc-bank-regalia-1111/        one folder per card, named for the card
        icici-bank-2222/
      bank/
        _unsorted/
        hdfc-bank-savings-3333/        one folder per account
        indusind-bank-indus-classic-4444/

A PDF lands in ``_unsorted`` because nothing but the mail headers is known at
download time — which card or account a statement belongs to is only decided by
parsing it. :func:`file_under` moves it the moment the parser says who it is, so
the folder name always reflects what the database believes, and a file that
never parsed stays visibly unsorted instead of being filed under a guess.

Callers hold the inbox *root* and ask this module for the rest. Nothing outside
here should join path components onto the inbox.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("sparser.inbox")

CARDS = "cards"
BANK = "bank"
UNSORTED = "_unsorted"

#: Every kind of statement the inbox holds, and the folder it gets.
KINDS = (CARDS, BANK)


def root() -> Path:
    """The inbox every component agrees on.

    One environment variable so the API server, the CLI and the pipelines
    cannot disagree about where statements are kept.
    """
    return Path(os.environ.get("SPARSER_INBOX", "inbox"))


def slug(label: str) -> str:
    """A folder name from a card or account's display name.

    ``"HDFC Bank Regalia ••1111"`` becomes ``"hdfc-bank-regalia-1111"``: lower
    case, ASCII, dash separated. The masking bullets carry no information once
    the digits survive, so they are dropped rather than transliterated.
    """
    text = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return text[:64] or "unknown"


def landing(root: Path, kind: str) -> Path:
    """The folder new downloads and uploads of ``kind`` are written to."""
    target = Path(root) / kind / UNSORTED
    target.mkdir(parents=True, exist_ok=True)
    return target


def folder_for(root: Path, kind: str, label: str) -> Path:
    """The folder holding every statement for one card or account."""
    return Path(root) / kind / slug(label)


def file_under(pdf: Path, root: Path, kind: str, label: str) -> Path:
    """Move ``pdf`` into its card's or account's folder; return where it landed.

    Returns the path unchanged when the file is already there, when the move
    fails, or when the PDF is not inside this inbox at all — an import of
    someone's ``~/Downloads`` must not relocate their files. A failure here is
    never fatal: the statement has already been parsed, and losing the tidy
    location matters far less than losing the run.
    """
    pdf = Path(pdf)
    root = Path(root)
    if not label or not _inside(pdf, root):
        return pdf
    target_dir = folder_for(root, kind, label)
    if pdf.parent == target_dir:
        return pdf
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / pdf.name
        if target.exists():
            if _same_bytes(target, pdf):
                # The same attachment arrived twice; keep the filed copy.
                pdf.unlink(missing_ok=True)
                return target
            target = _free_name(target)
        shutil.move(str(pdf), str(target))
        return target
    except OSError as exc:
        log.warning("could not file %s under %s: %s", pdf.name, target_dir, exc)
        return pdf


def index(root: Path) -> dict[str, Path]:
    """Every PDF in the inbox by file name, so a download can tell it already
    has this attachment even after the file was filed away from ``_unsorted``.

    Later folders win over earlier ones only when a name genuinely repeats,
    which the download naming scheme already makes unlikely.
    """
    found: dict[str, Path] = {}
    root = Path(root)
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            found.setdefault(path.name, path)
    return found


def find(root: Path, filename: str) -> Optional[Path]:
    """Where a statement with this file name still sits, or ``None``."""
    if not filename:
        return None
    return index(root).get(Path(filename).name)


def pdfs(root: Path) -> list[Path]:
    """Every statement in the inbox, deepest folder ordering aside, sorted by
    path so a run's file order is stable between machines."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _free_name(target: Path) -> Path:
    """A name in the same folder that does not clobber a different PDF."""
    for n in range(2, 1000):
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    return target
