"""The bank pipeline's mail layer: what it searches, what it keeps, what it fetches."""
import datetime as dt

import pytest

from sparser import bank_pipeline, mailbox, store
from sparser.bank_pipeline import _scan_account_rule_groups
from sparser.doctype import classify_bank_mail, classify_mail
from sparser.mailbox import _gmail_search_query


# --------------------------------------------------------------- mail classification

@pytest.mark.parametrize("subject", [
    "HDFC Bank Account Statement",
    "Your Savings Account Statement for August 2026",
    "Statement of Account - 50100000000000",
    "Your HDFC Bank Smart Statement is ready",
])
def test_account_statement_subjects_are_accepted(subject):
    accepted, reason = classify_bank_mail(subject)
    assert accepted, reason


@pytest.mark.parametrize("subject", [
    "ICICI Bank Credit Card Statement",
    "Your Credit Card e-Statement for July",
    "Fixed Deposit maturity advice",
    "Interest Certificate for FY 2025-26",
])
def test_card_and_other_products_are_refused(subject):
    accepted, reason = classify_bank_mail(subject)
    assert not accepted, reason


def test_the_two_classifiers_never_claim_the_same_mail():
    """A bank sends both products from one address; only the subject separates
    them, so a mail the card gate takes must be one this gate leaves."""
    for subject in [
        "HDFC Bank Credit Card Statement", "HDFC Bank Account Statement",
        "Your card statement is ready", "Your account statement is ready",
    ]:
        assert not (classify_mail(subject)[0] and classify_bank_mail(subject)[0]), subject


# ------------------------------------------------------------------- the Gmail query

def test_bank_query_does_not_require_an_attachment():
    """HDFC mails a link, not a file. Demanding has:attachment would hide it."""
    query = _gmail_search_query(
        ["hdfcbank.net"], dt.date(2026, 8, 1), None, ["account statement"], require_pdf=False,
    )
    assert "has:attachment" not in query
    assert "from:hdfcbank.net" in query and 'subject:\\"account statement\\"' in query


def test_card_query_still_requires_a_pdf_attachment():
    query = _gmail_search_query(["hdfcbank.net"], dt.date(2026, 8, 1), None, ["card statement"])
    assert "has:attachment filename:pdf" in query


# ---------------------------------------------------------------- the account filter

def _account(conn, fingerprint, last4, senders="[]", subjects="[]"):
    return conn.execute(
        """INSERT INTO bank_accounts
           (bank_code, bank_name, account_fingerprint, masked_number, last4, display_name,
            created_at, sender_ids_json, subject_patterns_json)
           VALUES ('hdfc','HDFC Bank',?,?,?,?, '2026-08-21T00:00:00', ?, ?) RETURNING id""",
        (fingerprint, f"•••• {last4}", last4, f"HDFC •••• {last4}", senders, subjects),
    ).fetchone()[0]


def test_unticked_account_is_excluded_and_says_so(tmp_path):
    conn = store.connect(tmp_path / "accounts.db")
    try:
        wanted = _account(conn, "fp-wanted", "0972")
        _account(conn, "fp-other", "4455")
        account_filter, _ = _scan_account_rule_groups(conn, [wanted], ["d"], ["s"])
        assert account_filter.verdict("fp-wanted", "0972")[0]
        keep, why = account_filter.verdict("fp-other", "4455")
        assert not keep and "not ticked" in why
    finally:
        conn.close()


def test_unrecognized_accounts_are_kept_only_when_asked_for(tmp_path):
    conn = store.connect(tmp_path / "unknown.db")
    try:
        known = _account(conn, "fp-known", "0972")
        strict, _ = _scan_account_rule_groups(conn, [known], ["d"], ["s"])
        keep, why = strict.verdict("fp-never-seen", "9999")
        assert not keep and "Unrecognized accounts" in why

        loose, groups = _scan_account_rule_groups(
            conn, [known], ["d"], ["s"], include_unrecognized=True
        )
        assert loose.verdict("fp-never-seen", "9999")[0]
        # With no saved rules to search by, the query has to widen to the defaults.
        assert ({"d"}, {"s"}) in groups
    finally:
        conn.close()


def test_account_rules_replace_defaults_only_when_both_fields_are_set(tmp_path):
    conn = store.connect(tmp_path / "rules.db")
    try:
        _account(conn, "fp-full", "0972", '["hdfcbank.net"]', '["hdfc account statement"]')
        _account(conn, "fp-half", "4455", '["icicibank.com"]', "[]")
        _, groups = _scan_account_rule_groups(conn, [], ["default.example"], ["default subject"])
        assert groups == [
            ({"hdfcbank.net"}, {"hdfc account statement"}),
            ({"icicibank.com"}, {"default subject"}),
        ]
    finally:
        conn.close()


def test_account_mail_rules_round_trip(tmp_path):
    conn = store.connect(tmp_path / "roundtrip.db")
    try:
        account_id = _account(conn, "fp-1", "0972")
        assert store.set_bank_account_mail_rules(
            conn, account_id, ["estatement@hdfcbank.net"], ["account statement"]
        )
        from sparser import bank_store
        saved = bank_store.accounts(conn)[0]
        assert saved["sender_ids"] == ["estatement@hdfcbank.net"]
        assert saved["subject_patterns"] == ["account statement"]
    finally:
        conn.close()


# ------------------------------------------------------------ mailbox scope per pipeline

def test_a_mailbox_is_swept_only_by_the_pipelines_it_is_marked_for(tmp_path, monkeypatch):
    from sparser import accounts as acct

    monkeypatch.delenv("SPARSER_GMAIL", raising=False)
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    conn = store.connect(tmp_path / "scope.db")
    try:
        cards_only = acct.add(conn, "cards@example.com", "aaaabbbbccccdddd")
        bank_only = acct.add(conn, "bank@example.com", "eeeeffffgggghhhh")
        acct.set_scope(conn, cards_only, use_for_cards=True, use_for_bank=False)
        acct.set_scope(conn, bank_only, use_for_cards=False, use_for_bank=True)

        assert [a.address for a in mailbox.accounts_from_store(conn, "cards")] \
            == ["cards@example.com"]
        assert [a.address for a in mailbox.accounts_from_store(conn, "bank")] \
            == ["bank@example.com"]
        # Unscoped callers still see everything, so nothing silently narrows.
        assert len(mailbox.accounts_from_store(conn)) == 2
    finally:
        conn.close()


def test_an_existing_mailbox_serves_both_pipelines_by_default(tmp_path, monkeypatch):
    from sparser import accounts as acct

    monkeypatch.delenv("SPARSER_GMAIL", raising=False)
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    conn = store.connect(tmp_path / "default-scope.db")
    try:
        acct.add(conn, "both@example.com", "aaaabbbbccccdddd")
        assert len(mailbox.accounts_from_store(conn, "cards")) == 1
        assert len(mailbox.accounts_from_store(conn, "bank")) == 1
    finally:
        conn.close()


# --------------------------------------------------------- the sweep, end to end

def _stub_mailbox(monkeypatch, found, boxes=("me@example.com",), member_id=None):
    """Replace only the network: everything downstream of the sweep stays real."""
    from sparser import accounts as acct_store

    monkeypatch.setattr(
        mailbox, "accounts_from_store",
        lambda _conn, purpose=None: [
            mailbox.Account(address, "secret", member_id) for address in boxes
        ],
    )
    monkeypatch.setattr(acct_store, "mark", lambda *args, **kwargs: None)
    searches = []

    def fake_fetch(_account, _dest, **kwargs):
        searches.append({
            "senders": set(kwargs["senders"]),
            "subjects": set(kwargs["subject_searches"]),
            "passwords": list(kwargs["passwords"]),
        })
        return list(found)

    monkeypatch.setattr(mailbox, "fetch_bank_account", fake_fetch)
    return searches


def test_a_sweep_queues_what_it_found_without_importing_it(tmp_path, monkeypatch):
    from tests.corpus import BANK_PDFS

    if not BANK_PDFS:
        pytest.skip("no bank sample statements available")
    db_path = tmp_path / "sweep.db"
    _stub_mailbox(monkeypatch, [BANK_PDFS[0]])

    run_id = bank_pipeline.run_scan_mail(db_path, tmp_path / "inbox", {}, months=1)

    conn = store.connect(db_path)
    try:
        run = conn.execute("SELECT * FROM ingest_runs WHERE id=?", (run_id,)).fetchone()
        queued = conn.execute(
            "SELECT status, document_type FROM ingest_files WHERE run_id=?", (run_id,)
        ).fetchall()
        # Parsed and queued, but the ledger is untouched until someone approves.
        assert conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0] == 0
    finally:
        conn.close()
    assert run["kind"] == "bank_scan" and run["status"] == "done"
    assert [(row["status"], row["document_type"]) for row in queued] \
        == [("pending", "bank_account")]


def test_the_sweep_searches_saved_account_rules_then_the_defaults(tmp_path, monkeypatch):
    db_path = tmp_path / "queries.db"
    conn = store.connect(db_path)
    try:
        _account(conn, "fp-full", "0972", '["hdfcbank.net"]', '["hdfc account statement"]')
        _account(conn, "fp-bare", "4455")
        conn.commit()
    finally:
        conn.close()
    searches = _stub_mailbox(monkeypatch, [])

    bank_pipeline.run_scan_mail(db_path, tmp_path / "inbox", {}, account_ids=[])

    assert [(s["senders"], s["subjects"]) for s in searches] == [
        ({"hdfcbank.net"}, {"hdfc account statement"}),
        (set(mailbox.BANK_STATEMENT_SENDERS), set(mailbox.BANK_SUBJECT_SEARCHES)),
    ]


def test_the_smart_statement_gate_is_given_only_entered_passwords(tmp_path, monkeypatch):
    """The gate is a live bank login, so a derived name/DOB guess must never
    reach it — however confident the convention is. Only what a person actually
    typed or saved goes down; the derived conventions stay behind, for opening a
    PDF already sitting on this machine where a wrong attempt reaches no one."""
    from sparser import accounts as acct_store

    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    db_path = tmp_path / "passwords.db"
    conn = store.connect(db_path)
    try:
        acct_store.set_profile(conn, "Test Person", "01/01/1990")
    finally:
        conn.close()
    searches = _stub_mailbox(monkeypatch, [])

    bank_pipeline.run_scan_mail(
        db_path, tmp_path / "inbox", {"password": "TYPED0101"}, months=1
    )

    offered = searches[0]["passwords"]
    assert offered == ["TYPED0101"], "only the entered password may reach the gate"
    # The same run derives the convention for local PDFs — it is withheld from
    # the gate deliberately, not because it could not be worked out.
    assert "TEST0101" in bank_pipeline._password_candidates(db_path, {"password": "TYPED0101"})


def test_a_sweep_with_no_enabled_mailbox_fails_with_an_actionable_note(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARSER_GMAIL", raising=False)
    monkeypatch.setattr(mailbox, "accounts_from_store", lambda _conn, purpose=None: [])
    db_path = tmp_path / "empty.db"

    run_id = bank_pipeline.run_scan_mail(db_path, tmp_path / "inbox", {})

    conn = store.connect(db_path)
    try:
        run = conn.execute("SELECT * FROM ingest_runs WHERE id=?", (run_id,)).fetchone()
        step = conn.execute(
            "SELECT detail FROM ingest_steps ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert run["status"] == "failed"
    assert "Connections" in step["detail"]


def test_a_statement_for_an_unticked_account_is_skipped_not_queued(tmp_path, monkeypatch):
    """The account is only known after parsing, so the filter must run there —
    and say which account it turned away."""
    from tests.corpus import BANK_PDFS

    if not BANK_PDFS:
        pytest.skip("no bank sample statements available")
    db_path = tmp_path / "filtered.db"
    conn = store.connect(db_path)
    try:
        # A saved account that is not the one in the sample, and is the only one ticked.
        ticked = _account(conn, "fp-somewhere-else", "1111")
        conn.commit()
    finally:
        conn.close()
    _stub_mailbox(monkeypatch, [BANK_PDFS[0]])

    run_id = bank_pipeline.run_scan_mail(
        db_path, tmp_path / "inbox", {}, account_ids=[ticked]
    )

    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, error FROM ingest_files WHERE run_id=?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["skipped"]
    assert "matches no saved account" in rows[0]["error"]


def test_a_mailbox_owner_keeps_attribution_of_what_it_delivered(tmp_path, monkeypatch):
    from tests.corpus import BANK_PDFS

    if not BANK_PDFS:
        pytest.skip("no bank sample statements available")
    db_path = tmp_path / "owner.db"
    conn = store.connect(db_path)
    try:
        from sparser import portal

        user = portal.register(conn, "owner", "secret-passphrase", "Owner")
        member_id = portal.members(conn, user["id"])[0]["id"]
    finally:
        conn.close()
    _stub_mailbox(monkeypatch, [BANK_PDFS[0]], member_id=member_id)

    run_id = bank_pipeline.run_scan_mail(
        db_path, tmp_path / "inbox", {"_member_id": None}, months=1
    )

    conn = store.connect(db_path)
    try:
        row = conn.execute(
            "SELECT member_id FROM ingest_files WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["member_id"] == member_id


# ---------------------------------------------- the editable fallback search lists

def test_the_built_in_lists_are_what_a_fresh_install_searches(tmp_path):
    conn = store.connect(tmp_path / "defaults.db")
    try:
        for kind, (senders, subjects) in mailbox.BUILT_IN_RULES.items():
            resolved = mailbox.scan_defaults(conn, kind)
            assert resolved["senders"] == list(senders)
            assert resolved["subjects"] == list(subjects)
            assert not resolved["customised"]
    finally:
        conn.close()


def test_edited_lists_are_what_a_scan_actually_searches(tmp_path, monkeypatch):
    """The panel and the sweep must not drift apart — an edit the user can see
    has to be the query the mailbox receives."""
    db_path = tmp_path / "edited.db"
    conn = store.connect(db_path)
    try:
        mailbox.set_scan_defaults(conn, "bank", ["my-bank.example"], ["my statement"])
    finally:
        conn.close()
    searches = _stub_mailbox(monkeypatch, [])

    bank_pipeline.run_scan_mail(db_path, tmp_path / "inbox", {}, account_ids=[])

    assert searches[0]["senders"] == {"my-bank.example"}
    assert searches[0]["subjects"] == {"my statement"}


def test_an_emptied_list_falls_back_rather_than_searching_for_nothing(tmp_path):
    conn = store.connect(tmp_path / "emptied.db")
    try:
        saved = mailbox.set_scan_defaults(conn, "cards", [], ["only a subject"])
        assert saved["senders"] == list(mailbox.STATEMENT_SENDERS)
        assert saved["subjects"] == ["only a subject"]
    finally:
        conn.close()


def test_entries_are_normalised_the_way_the_search_compares_them(tmp_path):
    conn = store.connect(tmp_path / "normalised.db")
    try:
        saved = mailbox.set_scan_defaults(
            conn, "bank", ["  HDFCBank.net ", ""], ["  Account Statement "]
        )
        assert saved["senders"] == ["hdfcbank.net"]
        assert saved["subjects"] == ["account statement"]
    finally:
        conn.close()


def test_reset_restores_the_built_in_lists(tmp_path):
    conn = store.connect(tmp_path / "reset.db")
    try:
        mailbox.set_scan_defaults(conn, "cards", ["nowhere.example"], ["nothing"])
        assert mailbox.scan_defaults(conn, "cards")["customised"]
        restored = mailbox.clear_scan_defaults(conn, "cards")
        assert not restored["customised"]
        assert restored["senders"] == list(mailbox.STATEMENT_SENDERS)
    finally:
        conn.close()


def test_the_two_kinds_are_edited_independently(tmp_path):
    conn = store.connect(tmp_path / "independent.db")
    try:
        mailbox.set_scan_defaults(conn, "bank", ["bank.example"], ["bank subject"])
        assert mailbox.scan_defaults(conn, "cards")["senders"] == list(mailbox.STATEMENT_SENDERS)
        assert not mailbox.scan_defaults(conn, "cards")["customised"]
    finally:
        conn.close()


def test_an_unknown_kind_is_refused(tmp_path):
    conn = store.connect(tmp_path / "unknown-kind.db")
    try:
        with pytest.raises(ValueError, match="unknown scan kind"):
            mailbox.scan_defaults(conn, "loans")
    finally:
        conn.close()
