"""User-managed categories and the rules that assign them.

Categorisation used to live entirely in code: `enrich._CATEGORY_RULES` for cards
and `bank_store._category` for bank rows. Those still ship as the fallback, but a
user can now own the list — add a category, give it a regex, reorder it, delete
it — and re-apply it across both ledgers.

Precedence, highest first:

1. ``category_override`` on the row — a manual decision is never overwritten.
2. A user rule whose regex matches the narration. Ordered; first match wins.
   These deliberately beat the issuer's own label: an issuer that calls every
   food delivery "RESTAURANTS" should not override a rule the user wrote.
3. The category the issuer printed on the statement (credit cards only; banks
   print none).
4. The built-in rules, so a fresh install categorises without any setup.
5. ``Other``.

Rules are per portal user. The pipeline resolves the user from the member a
statement was filed under, so a household's rules never reach another user's rows.

Two levels, two tables
----------------------

``major_categories`` is the short, stable list the household thinks in, shared by
name with the expense tracker so the two pillars can be read side by side. It
holds names and nothing else — no regex, no scope — because a major is a heading,
not a rule.

``categories`` is unchanged: the issuer-shaped labels and the user's own rules,
each pointing at one major through ``major_id``. Many subs to one major.

One invariant makes the split safe, and it is the reason the two tables need no
shared name check: **only a SUB name is ever written to a ledger row.** A major
is reached by lookup — sub name, then ``major_id``, then the major's name — so a
major called `Transfers` and a sub called `Transfers` are not ambiguous. The
ledger text always means the sub. The category picker on a transaction therefore
offers subs, never majors; booking a row straight to a heading would break the
one rule that keeps a stored label resolvable.

Re-parenting is one UPDATE on ``categories``. Renaming a major touches no
transaction at all, because no transaction carries its name.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Iterable, Optional

from .enrich import _CATEGORY_RULES, categorize

MAX_NAME_LENGTH = 80
MAX_PATTERN_LENGTH = 500

#: Where a rule is allowed to apply. Kept explicit because bank narrations and
#: card descriptions look nothing alike, so a rule written for one often misfires
#: on the other.
SCOPES = ("both", "cards", "bank")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    pattern    TEXT,
    applies_to TEXT NOT NULL DEFAULT 'both',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS ix_categories_user ON categories(user_id, position);

/* Headings, not rules: no pattern and no scope, because a major never matches
   a narration itself — its subs do that, and it collects what they catch. */
CREATE TABLE IF NOT EXISTS major_categories (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS ix_major_categories_user ON major_categories(user_id, position);
"""

#: Added to databases created before the two-level model. A sub with no major is
#: the honest starting state for an existing install: it says "not mapped yet"
#: rather than quietly inventing a heading for rows the user never placed.
_COLUMNS = {
    "major_id": "INTEGER REFERENCES major_categories(id) ON DELETE SET NULL",
}

#: The 11 majors the expense tracker already uses, spelled exactly as it spells
#: them. The spelling is the join: "House & Maintainence" keeps its typo here
#: deliberately, because a corrected name on this side would silently stop
#: matching the other pillar and the two would drift apart without an error.
_TRACKER_MAJORS = (
    "Household & Grocery",
    "House & Maintainence",
    "Personal Expense",
    "Gifting",
    "Trips & Travel",
    "Maids",
    "Medicine and Hospital",
    "EMI",
    "Outing",
    "Petrol & Cabs",
    "Electricity & Gas",
)

#: Majors a hand-kept tracker never needed and a statement ledger cannot avoid.
#: Bank mechanics are not consumption, and folding them into a spending major
#: would report money moving between your own accounts as money spent.
_LEDGER_MAJORS = (
    "Bills & Utilities",
    "Fees & Charges",
    "Rewards",
    "Investments",
    "Transfers",
    "Income",
    "Taxes",
    "Cash",
)

MAJOR_SEED: tuple[str, ...] = _TRACKER_MAJORS + _LEDGER_MAJORS

#: Where each built-in sub lands by default. Applied only to a sub that has no
#: parent yet, so it never overrules a choice the user has already made.
#:
#: Some of these are a guess at intent that only the reader can settle — a
#: restaurant charge is `Outing` here, but it is `Household & Grocery` on the
#: night it fed the house. The guess is made anyway, because an unmapped sub
#: shows up as unmapped and gets fixed, while an unseeded install shows nothing
#: at all and reads as though the feature does not work.
#:
#: Where the two tables hold the same name — the `Transfers` rule under the
#: `Transfers` heading — nothing special happens. They are separate namespaces
#: and only the sub name reaches a ledger row, so the pair is unambiguous.
_DEFAULT_PARENTS: dict[str, str] = {
    "Beauty & Wellness": "Personal Expense",
    "Bills & Utilities": "Bills & Utilities",
    "Cash": "Cash",
    "Cashback & Rewards": "Rewards",
    "Dividends": "Income",
    "Entertainment": "Outing",
    "Fees & Charges": "Fees & Charges",
    "Food & Dining": "Outing",
    "Fuel": "Petrol & Cabs",
    "Groceries": "Household & Grocery",
    "Health & Pharmacy": "Medicine and Hospital",
    "Investments": "Investments",
    "Loans & EMI": "EMI",
    # A card bill is money moving between your own accounts, not consumption:
    # the purchases it settles are already on the card ledger line by line.
    "Payment": "Transfers",
    "Salary & Income": "Income",
    "Shopping": "Personal Expense",
    "Software & Services": "Personal Expense",
    "Tax": "Taxes",
    "Transfers": "Transfers",
    "Travel & Transport": "Trips & Travel",
    "Utilities & Telecom": "Bills & Utilities",
}

#: The bank-specific rules that used to be hardcoded in ``bank_store._category``,
#: seeded so a user sees and can edit exactly what is already classifying rows.
_BANK_SEED: tuple[tuple[str, str], ...] = (
    ("Dividends", r"^\s*ACH\s+C\s*-"),
    ("Tax", r"\bincome\s+tax\b|\btax\b|\bgst\b"),
    ("Bills & Utilities", r"\bIB\s*BILLPAY\b|\bCHEQ\b|\bCRED\b"),
    ("Salary & Income", r"salary|payroll"),
    ("Investments", r"zerodha|broking|mutual fund|\bipo\b|\bfd\b|fixed deposit"),
    ("Loans & EMI", r"\bemi\b|loan|finance"),
    ("Cash", r"\batm\b|cash withdrawal"),
    ("Transfers", r"\bimps\b|\bneft\b|transfer"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(categories)").fetchall()}
    for column, declaration in _COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE categories ADD COLUMN {column} {declaration}")
    # After the migration, never inside SCHEMA: on a database created before the
    # two-level model, indexing a column the table does not have yet fails, and
    # it fails on connect — taking the whole application down, not one screen.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_categories_major ON categories(major_id)")
    _adopt_single_table_levels(conn, have)
    conn.commit()


def _adopt_single_table_levels(conn: sqlite3.Connection, columns: set[str]) -> None:
    """Carry across the short-lived shape that kept both levels in one table.

    That version marked majors with a ``kind`` column and pointed subs at them
    with ``parent_id``. Any database opened while it existed is migrated here
    rather than left half-converted, and the two columns are then dropped so
    there is one place a level can be recorded.
    """
    if "kind" not in columns and "parent_id" not in columns:
        return
    if "kind" in columns:
        conn.execute(
            """INSERT OR IGNORE INTO major_categories(user_id,name,position,created_at)
               SELECT user_id,name,position,created_at FROM categories WHERE kind='major'"""
        )
        # A sub keeps the heading it pointed at, by name — ids differ across tables.
        conn.execute(
            """UPDATE categories SET major_id=(
                   SELECT m.id FROM major_categories m JOIN categories p ON p.id=categories.parent_id
                   WHERE m.user_id=categories.user_id AND m.name=p.name COLLATE NOCASE)
               WHERE parent_id IS NOT NULL AND major_id IS NULL"""
        )
        # A promoted rule kept its pattern, so it stays a sub — now filed under
        # the heading of the same name. A major that was only ever a heading has
        # no rule to keep and is not duplicated into the sub list.
        conn.execute(
            """UPDATE categories SET major_id=(
                   SELECT m.id FROM major_categories m
                   WHERE m.user_id=categories.user_id AND m.name=categories.name COLLATE NOCASE)
               WHERE kind='major' AND major_id IS NULL"""
        )
        conn.execute(
            "DELETE FROM categories WHERE kind='major' AND (pattern IS NULL OR TRIM(pattern)='')"
        )
    conn.execute("DROP INDEX IF EXISTS ix_categories_parent")
    for column in ("parent_id", "kind"):
        if column in columns:
            try:
                conn.execute(f"ALTER TABLE categories DROP COLUMN {column}")
            except sqlite3.OperationalError:
                # Older SQLite cannot drop a column. Leaving it costs nothing:
                # nothing reads it any more.
                pass


def _stamp() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _valid_pattern(pattern: Optional[str]) -> Optional[str]:
    """Reject a regex that cannot compile — a broken rule would silently match nothing."""
    cleaned = (pattern or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_PATTERN_LENGTH:
        raise ValueError(f"pattern must be at most {MAX_PATTERN_LENGTH} characters")
    try:
        re.compile(cleaned, re.I)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    return cleaned


def _valid_name(name: str) -> str:
    cleaned = " ".join((name or "").split()).strip()
    if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"category name must be between 1 and {MAX_NAME_LENGTH} characters")
    return cleaned


def _valid_scope(scope: Optional[str]) -> str:
    value = (scope or "both").strip().lower()
    if value not in SCOPES:
        raise ValueError(f"applies_to must be one of {', '.join(SCOPES)}")
    return value


def _valid_major(
    conn: sqlite3.Connection, user_id: int, major_id: Optional[int]
) -> Optional[int]:
    """A major must be one this user owns.

    Checked here rather than left to the foreign key, which would happily accept
    another household's heading — producing a tree that reads as correct while
    filing your spending under someone else's list.
    """
    if major_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM major_categories WHERE id=? AND user_id=?", (int(major_id), user_id)
    ).fetchone()
    if not row:
        raise KeyError("major category not found")
    return int(major_id)


def seed(conn: sqlite3.Connection, user_id: int) -> int:
    """Populate a user's list from the built-in rules, once.

    Without this, opening the screen would show nothing while rows are visibly
    categorised, and saving anything would look like it changed the world.

    Returns the number of RULES added, so a user who already has a list still
    reports zero — the majors seeded alongside them are not new rules and do not
    change how a single row is categorised.
    """
    ensure_schema(conn)
    added = _seed_rules(conn, user_id)
    seed_majors(conn, user_id)
    return added


def seed_majors(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """Give a user the shared major list, and place the built-in subs under it.

    Runs on every read of the category list, not once at registration: an
    install that predates the two-level model has a full set of subs and no
    majors, and it has to acquire them without the user being told to migrate
    anything.

    Seeded once per household and then never again, which is what makes a
    deletion stick: this runs on every read of the category list, so re-seeding
    on each call would resurrect a heading the reader deleted and re-file a sub
    they deliberately unfiled — both silently, both on the next page load.

    "Once" is recorded rather than inferred from the table being non-empty. A
    user who creates a heading of their own before ever opening the list would
    otherwise never receive the shared one, and would be left wondering where
    the expense tracker's names went.
    """
    ensure_schema(conn)
    marker = f"major_categories_seeded:{user_id}"
    if conn.execute("SELECT 1 FROM app_metadata WHERE key=?", (marker,)).fetchone():
        return {"created": 0, "linked": 0}
    created = linked = 0
    now = _stamp()
    for position, name in enumerate(MAJOR_SEED):
        cur = conn.execute(
            """INSERT OR IGNORE INTO major_categories(user_id,name,position,created_at)
               VALUES(?,?,?,?)""",
            (user_id, name, position, now),
        )
        created += cur.rowcount or 0

    headings = {
        row["name"].strip().lower(): int(row["id"])
        for row in conn.execute(
            "SELECT id,name FROM major_categories WHERE user_id=?", (user_id,)
        ).fetchall()
    }
    for sub, major in _DEFAULT_PARENTS.items():
        major_id = headings.get(major.strip().lower())
        if major_id is None:
            continue
        cur = conn.execute(
            """UPDATE categories SET major_id=? WHERE user_id=? AND name=? COLLATE NOCASE
                 AND major_id IS NULL""",
            (major_id, user_id, sub),
        )
        linked += cur.rowcount or 0
    conn.execute("INSERT OR REPLACE INTO app_metadata(key,value) VALUES(?,?)", (marker, now))
    conn.commit()
    return {"created": created, "linked": linked}


def _seed_rules(conn: sqlite3.Connection, user_id: int) -> int:
    existing = conn.execute(
        "SELECT COUNT(*) FROM categories WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    if existing:
        return 0
    rows = [(name, pattern, "bank") for name, pattern in _BANK_SEED]
    rows += [(name, rx.pattern, "both") for name, rx in _CATEGORY_RULES]
    now = _stamp()
    added = 0
    for position, (name, pattern, scope) in enumerate(rows):
        try:
            conn.execute(
                """INSERT INTO categories(user_id,name,pattern,applies_to,position,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, name, pattern, scope, position, now),
            )
            added += 1
        except sqlite3.IntegrityError:
            # A name seeded by both lists keeps its first (bank-specific) rule.
            continue
    conn.commit()
    return added


#: The heading rides along on every read: a sub is meaningless to the reader
#: without the major it rolls up into, and resolving it caller-side would mean
#: every consumer reimplementing the join.
_SELECT = """SELECT c.id,c.name,c.pattern,c.applies_to,c.position,c.created_at,
                    c.major_id,m.name AS major
             FROM categories c LEFT JOIN major_categories m ON m.id=c.major_id"""


def listing(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    ensure_schema(conn)
    return [dict(row) for row in conn.execute(
        f"{_SELECT} WHERE c.user_id=? ORDER BY c.position, c.id", (user_id,),
    ).fetchall()]


def majors(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The heading list, each with the subs filed under it."""
    ensure_schema(conn)
    subs: dict[int, list[dict]] = {}
    for row in listing(conn, user_id):
        if row["major_id"] is not None:
            subs.setdefault(int(row["major_id"]), []).append(row)
    return [
        {**dict(row), "children": subs.get(int(row["id"]), [])}
        for row in conn.execute(
            """SELECT id,name,position,created_at FROM major_categories
               WHERE user_id=? ORDER BY position, id""",
            (user_id,),
        ).fetchall()
    ]


def unmapped(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Subs with no heading yet — the tray that says what is still uncounted.

    Deliberately not folded into an "Other" major. A sub silently absorbed into
    a bucket looks mapped, and nobody goes looking for it again.
    """
    return [row for row in listing(conn, user_id) if row["major_id"] is None]


def major_for(conn: sqlite3.Connection, user_id: int, name: str) -> Optional[str]:
    """The heading a stored category label rolls up into, if any.

    Takes the name because that is what the ledgers hold — a row carries the
    label, not the id. Only sub names are looked up: a ledger row never carries
    a heading, so a major and a sub sharing a name cannot be confused here.
    """
    row = conn.execute(
        f"{_SELECT} WHERE c.user_id=? AND c.name=? COLLATE NOCASE", (user_id, name),
    ).fetchone()
    return row["major"] if row else None


def add(
    conn: sqlite3.Connection,
    user_id: int,
    name: str,
    pattern: Optional[str] = None,
    applies_to: str = "both",
    major_id: Optional[int] = None,
) -> dict:
    """Create a sub-category, optionally filed under a major straight away.

    The pattern stays optional: a sub added purely to give an issuer's own label
    a home — or one the user simply wants to file rows under by hand — is a
    label, not a rule, and must not compete for matches with the rules above it.
    """
    ensure_schema(conn)
    clean_name = _valid_name(name)
    clean_pattern = _valid_pattern(pattern)
    scope = _valid_scope(applies_to)
    major = _valid_major(conn, user_id, major_id)
    tail = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM categories WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    try:
        cur = conn.execute(
            """INSERT INTO categories(user_id,name,pattern,applies_to,position,created_at,major_id)
               VALUES(?,?,?,?,?,?,?)""",
            (user_id, clean_name, clean_pattern, scope, int(tail), _stamp(), major),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("a category with that name already exists") from exc
    return _row(conn, user_id, int(cur.lastrowid))


def _row(conn: sqlite3.Connection, user_id: int, category_id: int) -> dict:
    row = conn.execute(
        f"{_SELECT} WHERE c.id=? AND c.user_id=?", (category_id, user_id),
    ).fetchone()
    if not row:
        raise KeyError("category not found")
    return dict(row)


# ----------------------------------------------------------- major categories

def _major_row(conn: sqlite3.Connection, user_id: int, major_id: int) -> dict:
    row = conn.execute(
        """SELECT id,name,position,created_at FROM major_categories
           WHERE id=? AND user_id=?""",
        (major_id, user_id),
    ).fetchone()
    if not row:
        raise KeyError("major category not found")
    return dict(row)


def major_add(conn: sqlite3.Connection, user_id: int, name: str) -> dict:
    ensure_schema(conn)
    clean_name = _valid_name(name)
    tail = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM major_categories WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    try:
        cur = conn.execute(
            "INSERT INTO major_categories(user_id,name,position,created_at) VALUES(?,?,?,?)",
            (user_id, clean_name, int(tail), _stamp()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("a major category with that name already exists") from exc
    return {**_major_row(conn, user_id, int(cur.lastrowid)), "children": []}


def major_update(conn: sqlite3.Connection, user_id: int, major_id: int, name: str) -> dict:
    """Rename a heading.

    No ledger row carries a major's name, so unlike renaming a sub this moves no
    transactions and cannot strand one — the subs keep pointing at it by id.
    """
    _major_row(conn, user_id, major_id)
    try:
        conn.execute(
            "UPDATE major_categories SET name=? WHERE id=? AND user_id=?",
            (_valid_name(name), major_id, user_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("a major category with that name already exists") from exc
    return _major_row(conn, user_id, major_id)


def major_remove(conn: sqlite3.Connection, user_id: int, major_id: int) -> dict:
    """Delete a heading; its subs return to the unmapped tray.

    Cleared explicitly rather than left to `ON DELETE SET NULL`, which the column
    only acquired by migration and which does nothing at all on a connection
    where foreign keys are off.
    """
    _major_row(conn, user_id, major_id)
    orphaned = conn.execute(
        "UPDATE categories SET major_id=NULL WHERE user_id=? AND major_id=?",
        (user_id, major_id),
    ).rowcount or 0
    conn.execute("DELETE FROM major_categories WHERE id=? AND user_id=?", (major_id, user_id))
    conn.commit()
    return {"status": "removed", "unmapped": int(orphaned)}


def major_reorder(conn: sqlite3.Connection, user_id: int, ordered_ids: Iterable[int]) -> list[dict]:
    """Headings are ordered for reading only — they never compete for a match."""
    owned = {int(row["id"]) for row in majors(conn, user_id)}
    wanted = [int(value) for value in ordered_ids]
    if set(wanted) != owned:
        raise ValueError("reorder must list every major category exactly once")
    for position, major_id in enumerate(wanted):
        conn.execute(
            "UPDATE major_categories SET position=? WHERE id=? AND user_id=?",
            (position, major_id, user_id),
        )
    conn.commit()
    return majors(conn, user_id)


def link(
    conn: sqlite3.Connection, user_id: int, category_id: int, major_id: Optional[int]
) -> dict:
    """File one sub under a major, or send it back to the tray with ``None``.

    The mapping screen's whole job, kept separate from `update` so that moving a
    category between headings cannot accidentally rewrite its rule.
    """
    _row(conn, user_id, category_id)
    major = _valid_major(conn, user_id, major_id)
    conn.execute(
        "UPDATE categories SET major_id=? WHERE id=? AND user_id=?",
        (major, category_id, user_id),
    )
    conn.commit()
    return _row(conn, user_id, category_id)


def _member_ids(conn: sqlite3.Connection, user_id: int) -> list[int]:
    return [
        int(row["id"])
        for row in conn.execute("SELECT id FROM members WHERE user_id=?", (user_id,)).fetchall()
    ]


def rename_rows(conn: sqlite3.Connection, user_id: int, old: str, new: str) -> int:
    """Carry existing transactions over to a renamed category.

    The ledgers store the category as text, so without this a rename would leave
    every row showing the old name until the rules were re-applied — and rows the
    user set by hand would keep it forever.
    """
    members = _member_ids(conn, user_id)
    if not members or old == new:
        return 0
    marks = ",".join("?" * len(members))
    moved = 0
    for table, join in (
        ("transactions", f"card_id IN (SELECT id FROM cards WHERE member_id IN ({marks}))"),
        (
            "bank_transactions",
            f"account_id IN (SELECT id FROM bank_accounts WHERE member_id IN ({marks}))",
        ),
    ):
        for column in ("category", "category_override"):
            cur = conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {column}=? AND {join}",
                [new, old, *members],
            )
            moved += cur.rowcount or 0
    conn.commit()
    return moved


def update(
    conn: sqlite3.Connection,
    user_id: int,
    category_id: int,
    name: Optional[str] = None,
    pattern: Optional[str] = None,
    applies_to: Optional[str] = None,
    clear_pattern: bool = False,
    major_id: Optional[int] = None,
    clear_major: bool = False,
) -> dict:
    current = _row(conn, user_id, category_id)
    new_name = _valid_name(name) if name is not None else current["name"]
    if clear_pattern:
        new_pattern = None
    elif pattern is not None:
        new_pattern = _valid_pattern(pattern)
    else:
        new_pattern = current["pattern"]
    scope = _valid_scope(applies_to) if applies_to is not None else current["applies_to"]
    # `major_id=None` means "leave it alone" on a partial update, the same way
    # `pattern` does; `clear_major` is how a sub is sent back to the tray.
    if clear_major:
        new_major = None
    elif major_id is not None:
        new_major = _valid_major(conn, user_id, major_id)
    else:
        new_major = current["major_id"]
    try:
        conn.execute(
            """UPDATE categories SET name=?, pattern=?, applies_to=?, major_id=?
               WHERE id=? AND user_id=?""",
            (new_name, new_pattern, scope, new_major, category_id, user_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("a category with that name already exists") from exc
    updated = _row(conn, user_id, category_id)
    updated["moved"] = rename_rows(conn, user_id, current["name"], new_name)
    return updated


def remove(conn: sqlite3.Connection, user_id: int, category_id: int) -> dict:
    _row(conn, user_id, category_id)
    conn.execute("DELETE FROM categories WHERE id=? AND user_id=?", (category_id, user_id))
    conn.commit()
    return {"status": "removed"}


def reorder(conn: sqlite3.Connection, user_id: int, ordered_ids: Iterable[int]) -> list[dict]:
    """Order is meaningful: the first matching rule wins."""
    owned = {row["id"] for row in listing(conn, user_id)}
    wanted = [int(value) for value in ordered_ids]
    if set(wanted) != owned:
        raise ValueError("reorder must list every category exactly once")
    for position, category_id in enumerate(wanted):
        conn.execute(
            "UPDATE categories SET position=? WHERE id=? AND user_id=?",
            (position, category_id, user_id),
        )
    conn.commit()
    return listing(conn, user_id)


class Rules:
    """Compiled user rules for one ledger, in priority order."""

    def __init__(self, rows: Iterable[dict], ledger: str):
        self._rules: list[tuple[str, re.Pattern]] = []
        for row in rows:
            if not row.get("pattern"):
                continue
            if row["applies_to"] not in ("both", ledger):
                continue
            try:
                self._rules.append((row["name"], re.compile(row["pattern"], re.I)))
            except re.error:
                # A rule saved before validation, or hand-edited in the database:
                # skip it rather than breaking every import.
                continue

    def __bool__(self) -> bool:
        return bool(self._rules)

    def match(self, description: str) -> Optional[str]:
        for name, rx in self._rules:
            if rx.search(description or ""):
                return name
        return None


def rules_for(conn: sqlite3.Connection, user_id: Optional[int], ledger: str) -> Rules:
    if user_id is None:
        return Rules([], ledger)
    return Rules(listing(conn, user_id), ledger)


def user_for_member(conn: sqlite3.Connection, member_id: Optional[int]) -> Optional[int]:
    """The portal user a member belongs to — how the pipeline finds the right rules."""
    if member_id is None:
        return None
    row = conn.execute("SELECT user_id FROM members WHERE id=?", (member_id,)).fetchone()
    return int(row["user_id"]) if row else None


def sole_user(conn: sqlite3.Connection) -> Optional[int]:
    """The only portal user, when there is exactly one.

    Re-categorisation and imports that carry no member still need rules in the
    ordinary single-household case; with several users the answer is ambiguous,
    so no rules are applied rather than the wrong ones.
    """
    rows = conn.execute("SELECT id FROM portal_users LIMIT 2").fetchall()
    return int(rows[0]["id"]) if len(rows) == 1 else None


def resolve(
    conn: sqlite3.Connection, member_id: Optional[int] = None, user_id: Optional[int] = None
) -> Optional[int]:
    return user_id or user_for_member(conn, member_id) or sole_user(conn)


def categorize_card(
    description: str, issuer_category: Optional[str], rules: Optional[Rules] = None
) -> str:
    """User rules first, then the issuer's own label, then the built-ins."""
    if rules:
        matched = rules.match(description)
        if matched:
            return matched
    return categorize(description, issuer_category)


def reapply(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """Recompute derived categories on both ledgers for one user's rows.

    Manual overrides are left untouched — only the derived column moves, so a row
    the user set by hand keeps showing what they chose.
    """
    from . import bank_store

    members = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM members WHERE user_id=?", (user_id,)).fetchall()
    ]
    if not members:
        return {"cards": 0, "bank": 0}
    marks = ",".join("?" * len(members))

    card_rules = rules_for(conn, user_id, "cards")
    card_rows = conn.execute(
        f"""SELECT t.id, t.description, t.category, t.issuer_category FROM transactions t
            JOIN cards c ON c.id = t.card_id WHERE c.member_id IN ({marks})""",
        members,
    ).fetchall()
    card_updates = []
    for row in card_rows:
        matched = card_rules.match(row["description"])
        if matched:
            fresh = matched
        elif row["issuer_category"] is not None:
            fresh = categorize(row["description"], row["issuer_category"])
        else:
            # Imported before the issuer's label was kept, so its provenance is
            # unknown. Recomputing could replace a good issuer category with a
            # guess, so the row is left exactly as it is.
            continue
        if fresh != row["category"]:
            card_updates.append((fresh, row["id"]))
    conn.executemany("UPDATE transactions SET category=? WHERE id=?", card_updates)

    bank_rules = rules_for(conn, user_id, "bank")
    bank_rows = conn.execute(
        f"""SELECT t.id, t.description, t.category FROM bank_transactions t
            JOIN bank_accounts a ON a.id = t.account_id WHERE a.member_id IN ({marks})""",
        members,
    ).fetchall()
    bank_updates = [
        (fresh, row["id"])
        for row in bank_rows
        if (fresh := bank_store._category(row["description"], bank_rules)) != row["category"]
    ]
    conn.executemany("UPDATE bank_transactions SET category=? WHERE id=?", bank_updates)
    conn.commit()
    return {"cards": len(card_updates), "bank": len(bank_updates)}


def in_use(conn: sqlite3.Connection, user_id: int, name: str) -> dict[str, int]:
    """How many rows currently show this category, derived or manually set."""
    members = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM members WHERE user_id=?", (user_id,)).fetchall()
    ]
    if not members:
        return {"cards": 0, "bank": 0}
    marks = ",".join("?" * len(members))
    cards = conn.execute(
        f"""SELECT COUNT(*) FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE c.member_id IN ({marks})
              AND COALESCE(NULLIF(TRIM(t.category_override),''), t.category) = ?""",
        [*members, name],
    ).fetchone()[0]
    bank = conn.execute(
        f"""SELECT COUNT(*) FROM bank_transactions t
            JOIN bank_accounts a ON a.id = t.account_id
            WHERE a.member_id IN ({marks})
              AND COALESCE(NULLIF(TRIM(t.category_override),''), t.category) = ?""",
        [*members, name],
    ).fetchone()[0]
    return {"cards": int(cards), "bank": int(bank)}


def provider_categories(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Categories the card issuers printed themselves — shown read-only.

    These are not ours to edit: they arrive on the statement. A user rule can
    still claim those rows, which is why they are listed beside the editable ones.
    """
    members = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM members WHERE user_id=?", (user_id,)).fetchall()
    ]
    if not members:
        return []
    marks = ",".join("?" * len(members))
    rows = conn.execute(
        f"""SELECT t.issuer_category AS name, COUNT(*) AS rows,
                   GROUP_CONCAT(DISTINCT c.issuer) AS sources
            FROM transactions t JOIN cards c ON c.id = t.card_id
            WHERE c.member_id IN ({marks})
              AND t.issuer_category IS NOT NULL AND TRIM(t.issuer_category) != ''
            GROUP BY t.issuer_category ORDER BY rows DESC""",
        members,
    ).fetchall()
    owned = {row["name"].strip().lower() for row in listing(conn, user_id)}
    return [
        {
            "name": row["name"],
            "rows": int(row["rows"]),
            "sources": sorted(filter(None, (row["sources"] or "").split(","))),
        }
        for row in rows
        if row["name"].strip().lower() not in owned
    ]
