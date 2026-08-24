"""User-managed categories: one shared list driving both ledgers."""
import datetime as dt
from decimal import Decimal

import pytest

from sparser import bank_store, categories, portal, store
from sparser.banks import BankStatement, BankTransaction
from sparser.schema import Check, DocType, Statement, Transaction, TxnType


def _user(conn) -> int:
    created = portal.register(conn, "owner", "secure-pass", "Owner")
    return int(created["id"])


def _member(conn, user_id: int) -> int:
    return int(portal.members(conn, user_id)[0]["id"])


def _bank_statement() -> BankStatement:
    return BankStatement(
        parser_id="hdfc_bank_v1", bank_code="HDFC", bank_name="HDFC Bank",
        account_holder="Test", account_number="5010000012345", account_type="SAVINGS",
        period_start=dt.date(2025, 1, 1), period_end=dt.date(2025, 1, 31),
        source_file="x.pdf",
        checks=[Check(name="running balance", passed=True, detail="ok")],
        transactions=[
            BankTransaction(
                date=dt.date(2025, 1, 2), value_date=dt.date(2025, 1, 2),
                description="UPI-BIGCHAI-bigchai@hdfc", reference="R1",
                amount=Decimal("120.00"), type=TxnType.DEBIT,
                balance=Decimal("880.00"), page=1,
            ),
        ],
    )


def _card_statement() -> Statement:
    return Statement(
        doc_type=DocType.CREDIT_CARD,
        template_id="test_v1", issuer="Axis Bank", product="Flipkart",
        account_masked="XXXX1234", currency="INR",
        statement_date=dt.date(2025, 1, 20),
        period_start=dt.date(2025, 1, 1), period_end=dt.date(2025, 1, 31),
        source_file="card.pdf",
        transactions=[
            Transaction(
                date=dt.date(2025, 1, 5), description="BIGCHAI OUTLET NEW DELHI",
                amount=Decimal("300.00"), type=TxnType.DEBIT,
                category="RESTAURANTS", page=1,
            ),
        ],
    )


def test_seeding_reproduces_todays_built_in_rules_once(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)

    assert categories.seed(conn, user_id) > 0
    names = {row["name"] for row in categories.listing(conn, user_id)}
    assert {"Food & Dining", "Transfers", "Salary & Income"} <= names
    # Idempotent: opening the screen again must not duplicate the list.
    assert categories.seed(conn, user_id) == 0
    conn.close()


def test_one_rule_applies_to_both_ledgers(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    categories.seed(conn, user_id)
    categories.add(conn, user_id, "Chai runs", r"bigchai", "both")
    categories.reorder(
        conn, user_id,
        # First match wins, so the new rule has to lead.
        [row["id"] for row in sorted(
            categories.listing(conn, user_id), key=lambda r: r["name"] != "Chai runs",
        )],
    )

    bank_store.import_statement(conn, _bank_statement(), member)
    store.import_statement(conn, _card_statement(), member)

    bank_row = bank_store.transactions(conn)[0]
    card_row = store.transactions(conn)[0]
    assert bank_row["category"] == "Chai runs"
    # The issuer printed RESTAURANTS; the user's rule outranks it.
    assert card_row["category"] == "Chai runs"
    conn.close()


def test_reapply_updates_derived_rows_but_never_a_manual_one(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    categories.seed(conn, user_id)
    bank_store.import_statement(conn, _bank_statement(), member)
    store.import_statement(conn, _card_statement(), member)

    bank_id = bank_store.transactions(conn)[0]["id"]
    bank_store.update_transaction_category(conn, bank_id, "Hand picked")

    categories.add(conn, user_id, "Chai runs", r"bigchai", "both")
    categories.reorder(
        conn, user_id,
        [row["id"] for row in sorted(
            categories.listing(conn, user_id), key=lambda r: r["name"] != "Chai runs",
        )],
    )
    moved = categories.reapply(conn, user_id)
    assert moved["cards"] == 1

    bank_row = bank_store.transactions(conn)[0]
    card_row = store.transactions(conn)[0]
    assert bank_row["category"] == "Hand picked"        # manual choice survives
    assert bank_row["derived_category"] == "Chai runs"  # underneath, the rule applied
    assert card_row["category"] == "Chai runs"
    conn.close()


def test_a_broken_regex_is_rejected_rather_than_silently_matching_nothing(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    with pytest.raises(ValueError, match="valid regular expression"):
        categories.add(conn, user_id, "Broken", "(unclosed")
    conn.close()


def test_names_are_unique_and_scope_is_validated(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.add(conn, user_id, "Chai runs", "bigchai")
    with pytest.raises(ValueError, match="already exists"):
        categories.add(conn, user_id, "chai runs", "other")
    with pytest.raises(ValueError, match="applies_to"):
        categories.add(conn, user_id, "Elsewhere", "x", "sideways")
    conn.close()


def test_a_bank_scoped_rule_does_not_touch_card_rows(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    created = categories.add(conn, user_id, "Bank only tag", r"bigchai", "bank")
    categories.reorder(conn, user_id, [created["id"]])

    bank_store.import_statement(conn, _bank_statement(), member)
    store.import_statement(conn, _card_statement(), member)

    assert bank_store.transactions(conn)[0]["category"] == "Bank only tag"
    assert store.transactions(conn)[0]["category"] != "Bank only tag"
    conn.close()


def test_rules_do_not_leak_between_users(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    first = _user(conn)
    second = int(portal.register(conn, "other", "secure-pass", "Other")["id"])
    categories.add(conn, first, "Mine", "bigchai")

    assert [row["name"] for row in categories.listing(conn, second)] == []
    with pytest.raises(KeyError):
        categories.update(conn, second, categories.listing(conn, first)[0]["id"], name="Theirs")
    conn.close()


def test_provider_categories_list_issuer_labels_we_do_not_own(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    store.import_statement(conn, _card_statement(), member)

    provider = categories.provider_categories(conn, user_id)
    # Exactly as printed on the statement, not the mapped/derived form.
    assert any(row["name"] == "RESTAURANTS" for row in provider), provider
    assert provider[0]["sources"] == ["Axis Bank"]

    # Once the user owns that name it stops being listed as read-only.
    categories.add(conn, user_id, "Restaurants", None)
    assert all(
        row["name"] != "RESTAURANTS" for row in categories.provider_categories(conn, user_id)
    )
    conn.close()


def test_renaming_a_category_carries_its_transactions_across_both_ledgers(tmp_path):
    """Ledgers store the label as text, so a rename has to migrate the rows."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    created = categories.add(conn, user_id, "Chai runs", r"bigchai", "both")
    categories.reorder(conn, user_id, [created["id"]])

    bank_store.import_statement(conn, _bank_statement(), member)
    store.import_statement(conn, _card_statement(), member)
    # One row is set by hand: a rename must move that too, not strand it.
    bank_id = bank_store.transactions(conn)[0]["id"]
    bank_store.update_transaction_category(conn, bank_id, "Chai runs")

    renamed = categories.update(conn, user_id, created["id"], name="Tea money")
    assert renamed["name"] == "Tea money"
    assert renamed["moved"] == 3  # card derived + bank derived + bank override

    assert store.transactions(conn)[0]["category"] == "Tea money"
    bank_row = bank_store.transactions(conn)[0]
    assert bank_row["category"] == "Tea money"
    assert bank_row["category_override"] == "Tea money"
    assert {row["label"] for row in bank_store.analytics(conn)["by_category"]} == {"Tea money"}
    conn.close()


def _major(conn, user_id: int, name: str) -> dict:
    return next(row for row in categories.majors(conn, user_id) if row["name"] == name)


def _sub(conn, user_id: int, name: str) -> dict:
    return next(row for row in categories.listing(conn, user_id) if row["name"] == name)


def test_seeding_gives_the_shared_major_list_and_files_the_built_in_subs(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)

    names = {row["name"] for row in categories.majors(conn, user_id)}
    # Spelled as the expense tracker spells them — the name is the join.
    assert {"Household & Grocery", "House & Maintainence", "Outing"} <= names
    # Plus the mechanics a hand-kept tracker never had to name.
    assert {"Fees & Charges", "Transfers", "Income"} <= names

    assert categories.major_for(conn, user_id, "Groceries") == "Household & Grocery"
    assert categories.major_for(conn, user_id, "Fuel") == "Petrol & Cabs"
    # A card bill settles purchases already recorded line by line; it is money
    # moving between your own accounts, not consumption.
    assert categories.major_for(conn, user_id, "Payment") == "Transfers"
    conn.close()


def test_the_two_tables_may_share_a_name_because_only_subs_reach_a_ledger(tmp_path):
    """`Transfers` the heading and `Transfers` the rule are different things."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)

    rule = _sub(conn, user_id, "Transfers")
    assert rule["pattern"]                       # the seeded rule is untouched
    assert rule["major"] == "Transfers"           # and files itself under the heading
    assert _major(conn, user_id, "Transfers")["name"] == "Transfers"
    conn.close()


def test_majors_reach_an_install_that_predates_them_without_touching_choices(tmp_path):
    """The migration path: subs already exist, majors do not, nothing is re-guessed."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)
    # The reader disagrees with the default: their restaurant spend is household.
    categories.link(
        conn, user_id,
        _sub(conn, user_id, "Food & Dining")["id"],
        _major(conn, user_id, "Household & Grocery")["id"],
    )

    before = len(categories.listing(conn, user_id))
    categories.seed_majors(conn, user_id)
    categories.seed_majors(conn, user_id)

    assert len(categories.listing(conn, user_id)) == before  # idempotent
    assert categories.major_for(conn, user_id, "Food & Dining") == "Household & Grocery"
    conn.close()


def test_a_database_from_before_the_two_level_model_opens_and_migrates(tmp_path):
    """`major_id` arrives by ALTER, so nothing keyed to it may run before that.

    Indexing it inside SCHEMA passed on a fresh database and failed on every
    existing one — on connect, which takes down the application rather than one
    screen.
    """
    path = tmp_path / "statements.db"
    conn = store.connect(path)
    user_id = _user(conn)
    conn.execute("DROP TABLE categories")
    conn.execute("DROP TABLE major_categories")
    conn.execute(
        """CREATE TABLE categories (
               id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
               pattern TEXT, applies_to TEXT NOT NULL DEFAULT 'both',
               position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
               UNIQUE(user_id, name COLLATE NOCASE))"""
    )
    conn.execute(
        """INSERT INTO categories(user_id,name,pattern,applies_to,position,created_at)
           VALUES(?,'Groceries','zepto','both',0,'2026-01-01T00:00:00')""",
        (user_id,),
    )
    conn.commit()
    conn.close()

    reopened = store.connect(path)
    assert categories.seed_majors(reopened, user_id)["created"] > 0
    assert categories.major_for(reopened, user_id, "Groceries") == "Household & Grocery"
    reopened.close()


def test_a_database_from_the_single_table_shape_is_carried_across(tmp_path):
    """The two levels briefly lived in `categories` itself; that state migrates."""
    path = tmp_path / "statements.db"
    conn = store.connect(path)
    user_id = _user(conn)
    conn.executescript(
        """DROP TABLE categories;
           DROP TABLE major_categories;
           CREATE TABLE categories (
               id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
               pattern TEXT, applies_to TEXT NOT NULL DEFAULT 'both',
               position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
               kind TEXT NOT NULL DEFAULT 'sub', parent_id INTEGER,
               UNIQUE(user_id, name COLLATE NOCASE));"""
    )
    rows = [
        ("Outing", None, "major", None),           # heading only, no rule
        ("Transfers", r"\bneft\b", "major", None),  # a promoted rule
        ("Food & Dining", "zomato", "sub", 1),
    ]
    for name, pattern, kind, parent in rows:
        conn.execute(
            """INSERT INTO categories(user_id,name,pattern,applies_to,position,created_at,kind,parent_id)
               VALUES(?,?,?,'both',0,'2026-01-01T00:00:00',?,?)""",
            (user_id, name, pattern, kind, parent),
        )
    conn.commit()
    conn.close()

    reopened = store.connect(path)
    names = {row["name"] for row in categories.majors(reopened, user_id)}
    assert {"Outing", "Transfers"} <= names
    # The heading with no rule leaves the sub list; the promoted rule stays a
    # rule and files itself under its own heading.
    assert not any(row["name"] == "Outing" for row in categories.listing(reopened, user_id))
    assert categories.major_for(reopened, user_id, "Transfers") == "Transfers"
    assert categories.major_for(reopened, user_id, "Food & Dining") == "Outing"
    reopened.close()


def test_a_sub_can_only_be_filed_under_one_of_your_own_majors(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    other = int(portal.register(conn, "other", "secure-pass", "Other")["id"])
    categories.seed(conn, user_id)
    categories.seed(conn, other)
    theirs = _major(conn, other, "Outing")

    # Another household's heading is not a parent, even though the id exists.
    with pytest.raises(KeyError):
        categories.add(conn, user_id, "Board games", "catan", "both", theirs["id"])
    with pytest.raises(KeyError):
        categories.link(conn, user_id, _sub(conn, user_id, "Groceries")["id"], theirs["id"])
    conn.close()


def test_a_sub_category_can_be_added_by_hand_under_a_major(tmp_path):
    """With a rule or without one — a label the reader files rows under is enough."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)
    outing = _major(conn, user_id, "Outing")

    with_rule = categories.add(conn, user_id, "Chai runs", r"bigchai", "both", outing["id"])
    plain = categories.add(conn, user_id, "Board games", None, "both", outing["id"])

    assert with_rule["major"] == "Outing" and plain["major"] == "Outing"
    assert {"Chai runs", "Board games"} <= {
        row["name"] for row in _major(conn, user_id, "Outing")["children"]
    }
    # A label with no pattern never competes for a match.
    assert categories.rules_for(conn, user_id, "cards").match("board games night") is None
    conn.close()


def test_an_issuer_label_becomes_a_sub_that_can_be_mapped(tmp_path):
    """The sprawl this exists to curb: `Misc Store` gets a home without a rule."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    categories.seed(conn, user_id)
    store.import_statement(conn, _card_statement(), member)

    assert any(
        row["name"] == "RESTAURANTS" for row in categories.provider_categories(conn, user_id)
    )
    # Adopted with no pattern: it is a label the issuer already applies, not a
    # rule we want competing with the user's own.
    adopted = categories.add(
        conn, user_id, "RESTAURANTS", None, "cards", _major(conn, user_id, "Outing")["id"]
    )

    assert adopted["major"] == "Outing"
    assert categories.major_for(conn, user_id, "RESTAURANTS") == "Outing"
    assert all(
        row["name"] != "RESTAURANTS" for row in categories.provider_categories(conn, user_id)
    )
    conn.close()


def test_an_unmapped_sub_is_listed_rather_than_swept_into_a_bucket(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)
    categories.add(conn, user_id, "Chai runs", r"bigchai", "both")

    assert "Chai runs" in {row["name"] for row in categories.unmapped(conn, user_id)}
    assert categories.major_for(conn, user_id, "Chai runs") is None

    # And a sub can be sent back to the tray after being filed.
    groceries = _sub(conn, user_id, "Groceries")
    categories.link(conn, user_id, groceries["id"], None)
    assert "Groceries" in {row["name"] for row in categories.unmapped(conn, user_id)}
    conn.close()


def test_deleting_a_major_returns_its_subs_to_the_tray(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)
    outing = _major(conn, user_id, "Outing")
    assert len(outing["children"]) >= 2

    removed = categories.major_remove(conn, user_id, outing["id"])
    assert removed["unmapped"] == len(outing["children"])
    assert categories.major_for(conn, user_id, "Food & Dining") is None
    assert "Food & Dining" in {row["name"] for row in categories.unmapped(conn, user_id)}
    # The rules themselves survive: only their heading went away.
    assert _sub(conn, user_id, "Food & Dining")["pattern"]
    conn.close()


def test_renaming_a_major_keeps_its_subs_and_touches_no_transaction(tmp_path):
    """No ledger row carries a heading, so this is the cheap rename of the two."""
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    member = _member(conn, user_id)
    categories.seed(conn, user_id)
    store.import_statement(conn, _card_statement(), member)
    before = store.transactions(conn)[0]["category"]

    categories.major_update(conn, user_id, _major(conn, user_id, "Outing")["id"], "Eating out")

    assert categories.major_for(conn, user_id, "Food & Dining") == "Eating out"
    assert categories.major_for(conn, user_id, "Entertainment") == "Eating out"
    assert store.transactions(conn)[0]["category"] == before
    conn.close()


def test_a_deleted_major_stays_deleted_and_an_unfiled_sub_stays_unfiled(tmp_path):
    """Seeding runs on every read of the list, so it must not undo a choice.

    Re-seeding each call resurrected a heading the reader had deleted and
    re-filed a sub they had deliberately unfiled — both on the next page load,
    both silently.
    """
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)

    categories.major_remove(conn, user_id, _major(conn, user_id, "Gifting")["id"])
    categories.link(conn, user_id, _sub(conn, user_id, "Groceries")["id"], None)

    categories.seed(conn, user_id)

    assert "Gifting" not in {row["name"] for row in categories.majors(conn, user_id)}
    assert categories.major_for(conn, user_id, "Groceries") is None
    conn.close()


def test_major_names_are_unique_within_a_household(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    user_id = _user(conn)
    categories.seed(conn, user_id)
    with pytest.raises(ValueError, match="already exists"):
        categories.major_add(conn, user_id, "outing")
    conn.close()


def test_a_rename_does_not_touch_another_users_rows(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    first = _user(conn)
    member = _member(conn, first)
    second = int(portal.register(conn, "other", "secure-pass", "Other")["id"])
    other_member = int(portal.members(conn, second)[0]["id"])

    created = categories.add(conn, first, "Chai runs", r"bigchai", "both")
    categories.reorder(conn, first, [created["id"]])
    store.import_statement(conn, _card_statement(), member)

    # The other user's row happens to carry the same label.
    conn.execute("UPDATE cards SET member_id=? WHERE id=(SELECT MIN(id) FROM cards)", (other_member,))
    conn.commit()
    assert categories.update(conn, first, created["id"], name="Tea money")["moved"] == 0
    assert store.transactions(conn)[0]["category"] == "Chai runs"
    conn.close()
