"""PDF decryption.

Issuer statements arrive encrypted. The user supplies the password, or we derive
candidates from the well-known per-issuer patterns given their name/DOB/card.
We never brute-force: the candidate list is small and user-authorised.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Iterable, Optional

import pikepdf


class DecryptError(RuntimeError):
    pass


def dob_forms(dob: "str | dt.date | None") -> list[str]:
    """The date renderings issuers use, from whatever precision is known.

    A full date gives DDMM, DDMMYYYY and DDMMYY. A year-less "15/08" or "1508"
    still gives DDMM, which is the form most issuers actually use — so a user who
    would rather not record their birth year loses very little.
    """
    if isinstance(dob, dt.date):
        return [dob.strftime("%d%m"), dob.strftime("%d%m%Y"), dob.strftime("%d%m%y")]
    text = (dob or "").strip()
    if not text:
        return []
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8:                      # DDMMYYYY
        return [digits[:4], digits, digits[:4] + digits[6:]]
    if len(digits) == 6:                      # DDMMYY
        return [digits[:4], digits[:4] + "19" + digits[4:], digits]
    if len(digits) == 4:                      # DDMM only
        return [digits]
    return []


def candidate_passwords(
    name: Optional[str] = None,
    dob: "str | dt.date | None" = None,
    card_last4: Optional[str] = None,
) -> list[str]:
    """Documented Indian card-issuer statement password conventions.

    The dominant pattern is the first four letters of the name — upper or lower
    case depending on issuer — followed by either DDMM or the full DDMMYYYY.
    Both casings and both date widths are generated because a single cardholder
    routinely holds cards across issuers that disagree.
    """
    out: list[str] = []
    # Every word of the name, not just the first: issuers disagree about whether
    # the stem comes from the given name or the surname, and a cardholder holds
    # cards across issuers. A two-word name therefore yields a stem from each.
    words = [w for w in re.split(r"[\s.]+", (name or "").strip()) if w]
    stems: list[str] = []
    for w in words:
        stem = w[:4]
        if stem and stem not in stems:
            stems.append(stem)

    dates = dob_forms(dob)
    for stem in stems:
        for cased in (stem.lower(), stem.upper(), stem.capitalize()):
            out += [f"{cased}{d}" for d in dates]
    out += dates
    if card_last4:
        out += [f"{card_last4}{d}" for d in dates]
    if card_last4:
        out.append(card_last4)
    for stem in stems:
        if card_last4:
            out += [f"{stem.lower()}{card_last4}", f"{stem.upper()}{card_last4}"]
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def is_encrypted(path: str | Path) -> bool:
    try:
        with pikepdf.open(path):
            return False
    except pikepdf.PasswordError:
        return True


def decrypt_to(
    src: str | Path, dest: str | Path, passwords: Iterable[str] = ()
) -> tuple[Path, Optional[str]]:
    """Write a decrypted copy. Returns (path, password_used|None)."""
    src, dest = Path(src), Path(dest)
    try:
        with pikepdf.open(src) as pdf:
            pdf.save(dest)
            return dest, None
    except pikepdf.PasswordError:
        pass

    for pw in passwords:
        try:
            with pikepdf.open(src, password=pw) as pdf:
                pdf.save(dest)
                return dest, pw
        except pikepdf.PasswordError:
            continue
    raise DecryptError(f"{src.name}: encrypted and no supplied password worked")
