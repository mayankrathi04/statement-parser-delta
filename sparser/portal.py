"""Portal identity, sessions and household members.

Financial rows belong to a member, and members belong to one portal user.  A
fresh user always receives a ``Personal`` member; the first user also claims
legacy rows that pre-date ownership support.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
import re
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS portal_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_profiles (
    member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
    full_name BLOB,
    dob BLOB,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_members_user ON members(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON portal_sessions(user_id);
"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(value: Optional[dt.datetime] = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, expected = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = _hash_password(password, bytes.fromhex(salt_hex)).rsplit("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # ``mailboxes`` is owned by accounts.py rather than the core schema.
    have = {row["name"] for row in conn.execute("PRAGMA table_info(mailboxes)").fetchall()}
    if have and "member_id" not in have:
        conn.execute("ALTER TABLE mailboxes ADD COLUMN member_id INTEGER")
    conn.commit()


def has_users(conn: sqlite3.Connection) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT 1 FROM portal_users LIMIT 1").fetchone() is not None


def register(conn: sqlite3.Connection, username: str, password: str, display_name: str) -> dict:
    ensure_schema(conn)
    username = username.strip().lower()
    display_name = display_name.strip()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
        raise ValueError("username must be 3-64 letters, numbers, dots, dashes, or underscores")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if not display_name:
        raise ValueError("display name is required")
    first_user = not has_users(conn)
    now = _stamp()
    try:
        cur = conn.execute(
            "INSERT INTO portal_users(email,display_name,password_hash,created_at) VALUES(?,?,?,?)",
            (username, display_name, _hash_password(password), now),
        )
        user_id = int(cur.lastrowid)
        member = conn.execute(
            "INSERT INTO members(user_id,name,is_default,created_at) VALUES(?, 'Personal', 1, ?)",
            (user_id, now),
        )
        member_id = int(member.lastrowid)
        if first_user:
            conn.execute("UPDATE cards SET member_id=? WHERE member_id IS NULL", (member_id,))
            conn.execute("UPDATE bank_accounts SET member_id=? WHERE member_id IS NULL", (member_id,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mailboxes'").fetchone():
                conn.execute("UPDATE mailboxes SET member_id=? WHERE member_id IS NULL", (member_id,))
            old = conn.execute("SELECT full_name,dob,updated_at FROM profile WHERE id=1").fetchone()
            if old:
                conn.execute(
                    "INSERT OR IGNORE INTO member_profiles(member_id,full_name,dob,updated_at) VALUES(?,?,?,?)",
                    (member_id, old["full_name"], old["dob"], old["updated_at"]),
                )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("an account with that username already exists") from exc
    return user(conn, user_id)


def login(conn: sqlite3.Connection, username: str, password: str) -> tuple[str, dict]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM portal_users WHERE email=? COLLATE NOCASE", (username.strip(),)
    ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        raise ValueError("invalid username or password")
    token = secrets.token_urlsafe(32)
    # Expired rows are never read again; clearing them here keeps a long-lived
    # portal from accumulating one dead session per sign-in forever.
    conn.execute("DELETE FROM portal_sessions WHERE expires_at<=?", (_stamp(),))
    conn.execute(
        "INSERT INTO portal_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        (_token_hash(token), row["id"], _stamp(_now() + dt.timedelta(days=30)), _stamp()),
    )
    conn.commit()
    return token, user(conn, int(row["id"]))


def logout(conn: sqlite3.Connection, token: str) -> bool:
    """Invalidate one session server-side.

    Dropping the token in the browser alone would leave it usable for the rest of
    its 30-day life if it had already leaked.
    """
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM portal_sessions WHERE token_hash=?", (_token_hash(token),))
    conn.commit()
    return bool(cur.rowcount)


def user_for_token(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT u.id,u.email,u.display_name,u.created_at
           FROM portal_sessions s JOIN portal_users u ON u.id=s.user_id
           WHERE s.token_hash=? AND s.expires_at>?""",
        (_token_hash(token), _stamp()),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["username"] = result.pop("email")
    return result


def user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT id,email,display_name,created_at FROM portal_users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        raise KeyError("portal user not found")
    result = dict(row)
    result["username"] = result.pop("email")
    result["members"] = members(conn, user_id)
    return result


def replace_credentials(conn: sqlite3.Connection, user_id: int, username: str, password: str) -> None:
    """Rename/reset one user in place; ownership foreign keys remain unchanged."""
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
        raise ValueError("invalid username")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    conn.execute(
        "UPDATE portal_users SET email=?, display_name=?, password_hash=? WHERE id=?",
        (username, username, _hash_password(password), user_id),
    )
    conn.execute("DELETE FROM portal_sessions WHERE user_id=?", (user_id,))
    conn.commit()


def members(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    ensure_schema(conn)
    return [dict(row) for row in conn.execute(
        "SELECT id,name,is_default,created_at FROM members WHERE user_id=? ORDER BY is_default DESC,name",
        (user_id,),
    ).fetchall()]


def add_member(conn: sqlite3.Connection, user_id: int, name: str) -> dict:
    name = " ".join(name.split()).strip()
    if not name or len(name) > 80:
        raise ValueError("member name must be between 1 and 80 characters")
    try:
        cur = conn.execute(
            "INSERT INTO members(user_id,name,is_default,created_at) VALUES(?,?,0,?)",
            (user_id, name, _stamp()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("that member already exists") from exc
    return dict(conn.execute(
        "SELECT id,name,is_default,created_at FROM members WHERE id=?", (cur.lastrowid,)
    ).fetchone())


def _member_row(conn: sqlite3.Connection, user_id: int, member_id: int) -> dict:
    row = conn.execute(
        "SELECT id,name,is_default,created_at FROM members WHERE id=? AND user_id=?",
        (member_id, user_id),
    ).fetchone()
    if not row:
        raise PermissionError("member does not belong to this user")
    return dict(row)


def rename_member(conn: sqlite3.Connection, user_id: int, member_id: int, name: str) -> dict:
    _member_row(conn, user_id, member_id)
    name = " ".join(name.split()).strip()
    if not name or len(name) > 80:
        raise ValueError("member name must be between 1 and 80 characters")
    try:
        conn.execute("UPDATE members SET name=? WHERE id=?", (name, member_id))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("that member already exists") from exc
    return _member_row(conn, user_id, member_id)


def set_default_member(conn: sqlite3.Connection, user_id: int, member_id: int) -> dict:
    """The member an import falls back to when the selection names no single one."""
    _member_row(conn, user_id, member_id)
    conn.execute("UPDATE members SET is_default=0 WHERE user_id=?", (user_id,))
    conn.execute("UPDATE members SET is_default=1 WHERE id=?", (member_id,))
    conn.commit()
    return _member_row(conn, user_id, member_id)


#: Rows that would be orphaned by removing a member, and what to call them.
_MEMBER_REFERENCES = (
    ("cards", "card"),
    ("bank_accounts", "bank account"),
    ("mailboxes", "mailbox"),
)


def member_usage(conn: sqlite3.Connection, member_id: int) -> dict[str, int]:
    counts = {}
    for table, label in _MEMBER_REFERENCES:
        counts[label] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE member_id=?", (member_id,)
        ).fetchone()[0])
    return counts


def delete_member(conn: sqlite3.Connection, user_id: int, member_id: int) -> None:
    """Only ever removes an empty member — data is never deleted as a side effect."""
    member = _member_row(conn, user_id, member_id)
    if len(members(conn, user_id)) == 1:
        raise ValueError("a household needs at least one member")
    if member["is_default"]:
        raise ValueError("make another member the default before removing this one")
    attached = {label: n for label, n in member_usage(conn, member_id).items() if n}
    if attached:
        detail = ", ".join(f"{n} {label}{'' if n == 1 else 's'}" for label, n in attached.items())
        raise ValueError(
            f"{member['name']} still has {detail}. Reassign them on the Cards, Bank Accounts "
            "and Connections tabs first."
        )
    conn.execute("DELETE FROM members WHERE id=?", (member_id,))
    conn.commit()


def allowed_member_ids(conn: sqlite3.Connection, user_id: int, requested=()) -> list[int]:
    owned = {row["id"] for row in members(conn, user_id)}
    wanted = {int(value) for value in requested if str(value).isdigit()}
    if wanted and not wanted.issubset(owned):
        raise PermissionError("one or more selected members do not belong to this user")
    return sorted(wanted or owned)


def require_member(conn: sqlite3.Connection, user_id: int, member_id: int) -> int:
    if member_id not in set(allowed_member_ids(conn, user_id)):
        raise PermissionError("member does not belong to this user")
    return member_id
