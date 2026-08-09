"""Connected mailboxes, with their app passwords encrypted at rest.

An app password grants read access to a mailbox full of financial documents, so
it is never written to the database in the clear and never sent back to the
browser. It is encrypted with a key held in a 0600 file outside the project
directory, so copying or committing the database alone leaks nothing usable.

This is protection against casual exposure — a synced folder, a backup, a repo —
not against an attacker who already has the user's account on the machine. Full
protection would mean a passphrase the user types on every run, which defeats the
unattended monthly fetch this exists to enable.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    full_name  BLOB,
    dob        BLOB,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS card_secrets (
    id           INTEGER PRIMARY KEY,
    card_key     TEXT NOT NULL UNIQUE,   -- masked card number
    secret       BLOB NOT NULL,
    source       TEXT DEFAULT 'manual',  -- manual | learned
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS mailboxes (
    id           INTEGER PRIMARY KEY,
    address      TEXT NOT NULL UNIQUE,
    secret       BLOB NOT NULL,
    provider     TEXT DEFAULT 'gmail',
    status       TEXT DEFAULT 'unknown',   -- unknown | connected | failed
    last_error   TEXT,
    last_checked TEXT,
    last_sync    TEXT,
    added_at     TEXT
);
"""


def key_path() -> Path:
    override = os.environ.get("SPARSER_KEY_FILE")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sparser"
    return base / "secret.key"


def _key() -> bytes:
    path = key_path()
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(_key())


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def add(conn: sqlite3.Connection, address: str, app_password: str, provider: str = "gmail") -> int:
    ensure_schema(conn)
    # App passwords are displayed in groups of four and pasted with the spaces.
    secret = _fernet().encrypt(app_password.replace(" ", "").encode())
    cur = conn.execute(
        """INSERT INTO mailboxes (address, secret, provider, status, added_at)
           VALUES (?,?,?,'unknown',?)
           ON CONFLICT(address) DO UPDATE SET secret = excluded.secret, status = 'unknown',
             last_error = NULL""",
        (address.strip(), secret, provider, dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM mailboxes WHERE address = ?", (address.strip(),)).fetchone()
    return int(row["id"] if row else cur.lastrowid)


def remove(conn: sqlite3.Connection, mailbox_id: int) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,))
    conn.commit()


def secret_for(conn: sqlite3.Connection, address: str) -> Optional[str]:
    ensure_schema(conn)
    row = conn.execute("SELECT secret FROM mailboxes WHERE address = ?", (address,)).fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["secret"]).decode()
    except InvalidToken:
        return None


def listing(conn: sqlite3.Connection) -> list[dict]:
    """Never includes the secret — only whether one is present and usable."""
    ensure_schema(conn)
    out = []
    for r in conn.execute("SELECT * FROM mailboxes ORDER BY address").fetchall():
        d = dict(r)
        secret = d.pop("secret", None)
        try:
            _fernet().decrypt(secret)
            d["secret_ok"] = True
        except (InvalidToken, TypeError):
            # A rotated or missing key file leaves rows that can no longer be read.
            d["secret_ok"] = False
        out.append(d)
    return out


def mark(
    conn: sqlite3.Connection,
    address: str,
    status: str,
    error: Optional[str] = None,
    synced: bool = False,
) -> None:
    ensure_schema(conn)
    now = dt.datetime.now().isoformat(timespec="seconds")
    if synced:
        conn.execute(
            "UPDATE mailboxes SET status=?, last_error=?, last_checked=?, last_sync=? WHERE address=?",
            (status, error, now, now, address),
        )
    else:
        conn.execute(
            "UPDATE mailboxes SET status=?, last_error=?, last_checked=? WHERE address=?",
            (status, error, now, address),
        )
    conn.commit()


def test_connection(address: str, app_password: str, budget: float = 12.0) -> tuple[bool, str]:
    """Log in and sample how many statement mails are visible.

    Deliberately bounded. Searching every known issuer across a full All Mail
    folder can run for minutes on a mailbox with years of history, and a browser
    gives up long before that — which surfaces as an unexplained "failed to
    fetch" rather than an answer. So the search is limited to the last year, runs
    against a wall-clock budget, and reports a partial count if it runs out.
    Connecting at all is what this is really testing; the count is a bonus.
    """
    import datetime as _dt
    import imaplib
    import time

    from .mailbox import IMAP_TIMEOUT, STATEMENT_SENDERS

    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
    except OSError as exc:
        return False, f"cannot reach imap.gmail.com: {exc}"

    try:
        conn.login(address, app_password.replace(" ", ""))
        status, _ = conn.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            conn.select("INBOX", readonly=True)

        since = (_dt.date.today() - _dt.timedelta(days=365)).strftime("%d-%b-%Y")
        deadline = time.monotonic() + budget
        hits, scanned, partial = 0, 0, False
        for sender in STATEMENT_SENDERS:
            if time.monotonic() > deadline:
                partial = True
                break
            st, data = conn.search(None, "FROM", f'"{sender}"', "SINCE", since)
            scanned += 1
            if st == "OK" and data and data[0]:
                hits += len(data[0].split())

        note = (
            f"connected — {hits} statement mails in the last year"
            f" (sampled {scanned}/{len(STATEMENT_SENDERS)} issuers)"
            if partial
            else f"connected — {hits} statement mails in the last year"
        )
        return True, note
    except imaplib.IMAP4.error as exc:
        msg = str(exc)
        if "AUTHENTICATIONFAILED" in msg.upper() or "Invalid credentials" in msg:
            return False, (
                "login rejected — Gmail needs a 16-character app password "
                "(myaccount.google.com/apppasswords), not your normal password"
            )
        return False, msg
    except OSError as exc:
        return False, f"connection failed or timed out after {IMAP_TIMEOUT}s: {exc}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# --------------------------------------------------------- card passwords

def set_card_password(
    conn: sqlite3.Connection, card_key: str, password: str, source: str = "manual"
) -> None:
    """Store the PDF password for one card, encrypted with the same key."""
    ensure_schema(conn)
    blob = _fernet().encrypt(password.encode())
    conn.execute(
        """INSERT INTO card_secrets (card_key, secret, source, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(card_key) DO UPDATE SET
             secret = excluded.secret, source = excluded.source, updated_at = excluded.updated_at""",
        (card_key, blob, source, dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def card_password(conn: sqlite3.Connection, card_key: str) -> Optional[str]:
    ensure_schema(conn)
    row = conn.execute("SELECT secret FROM card_secrets WHERE card_key = ?", (card_key,)).fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["secret"]).decode()
    except InvalidToken:
        return None


def all_card_passwords(conn: sqlite3.Connection) -> dict[str, str]:
    """Every known card password, for the decrypt stage to try before guessing."""
    ensure_schema(conn)
    out: dict[str, str] = {}
    for row in conn.execute("SELECT card_key, secret FROM card_secrets").fetchall():
        try:
            out[row["card_key"]] = _fernet().decrypt(row["secret"]).decode()
        except InvalidToken:
            continue
    return out


def clear_card_password(conn: sqlite3.Connection, card_key: str) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM card_secrets WHERE card_key = ?", (card_key,))
    conn.commit()


def card_secret_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    ensure_schema(conn)
    return {
        r["card_key"]: {"source": r["source"], "updated_at": r["updated_at"]}
        for r in conn.execute("SELECT card_key, source, updated_at FROM card_secrets").fetchall()
    }


# --------------------------------------------------------------- profile

def set_profile(conn: sqlite3.Connection, full_name: str, dob: str) -> None:
    """The name and date of birth used to derive statement passwords.

    Encrypted like every other secret here: a date of birth is exactly the kind
    of identity detail that should not sit in plaintext in a file that gets
    backed up. The year is optional — "15/08" is enough for the DDMM form most
    issuers use.
    """
    ensure_schema(conn)
    f = _fernet()
    conn.execute(
        """INSERT INTO profile (id, full_name, dob, updated_at) VALUES (1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET full_name = excluded.full_name,
             dob = excluded.dob, updated_at = excluded.updated_at""",
        (
            f.encrypt((full_name or "").encode()),
            f.encrypt((dob or "").encode()),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection) -> dict:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if not row:
        return {"full_name": "", "dob": "", "updated_at": None}
    f = _fernet()

    def dec(blob):
        try:
            return f.decrypt(blob).decode()
        except (InvalidToken, TypeError):
            return ""

    return {
        "full_name": dec(row["full_name"]),
        "dob": dec(row["dob"]),
        "updated_at": row["updated_at"],
    }


def clear_profile(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM profile WHERE id = 1")
    conn.commit()
