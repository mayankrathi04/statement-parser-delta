import datetime as dt

import pytest

from sparser import accounts, mailbox, store
from sparser.pipeline import _scan_card_rule_groups, month_range_window, run_scan
from sparser.store import same_card
from sparser.schema import DocType, Statement, Transaction, TxnType
from sparser.validate import run_checks, transactions_within_period


def test_month_range_includes_statement_delivery_edges():
    assert month_range_window("2026-01", "2026-03") == (
        dt.date(2025, 12, 25),
        dt.date(2026, 4, 11),
    )


def test_month_range_rejects_reverse_order():
    with pytest.raises(ValueError):
        month_range_window("2026-04", "2026-02")


def test_card_mail_rules_round_trip(tmp_path):
    conn = store.connect(tmp_path / "rules.db")
    try:
        conn.execute(
            "INSERT INTO cards (issuer, product, masked_number, last4, display_name) "
            "VALUES ('Axis', 'Flipkart', 'XX21', '4321', 'Flipkart Axis ••4321')"
        )
        card_id = conn.execute("SELECT id FROM cards").fetchone()[0]
        assert store.set_card_mail_rules(
            conn, card_id, ["statements@axisbank.com"], ["flipkart statement"]
        )
        card = store.cards(conn)[0]
        assert card["sender_ids"] == ["statements@axisbank.com"]
        assert card["subject_patterns"] == ["flipkart statement"]
    finally:
        conn.close()


def test_scan_all_cards_unions_every_saved_mail_rule(tmp_path):
    conn = store.connect(tmp_path / "all-card-rules.db")
    try:
        first = conn.execute(
            "INSERT INTO cards "
            "(issuer, masked_number, display_name, sender_ids_json, subject_patterns_json) "
            "VALUES ('Axis', 'XX97', 'Axis 97', "
            "'[\"axis.example\"]', '[\"axis card statement\"]') "
            "RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO cards "
            "(issuer, masked_number, display_name, sender_ids_json, subject_patterns_json) "
            "VALUES ('ICICI', 'XX07', 'ICICI 07', "
            "'[\"icici.example\"]', '[\"icici card statement\"]')"
        )
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('Blank', 'XX00', 'Blank 00')"
        )

        card_filter, groups = _scan_card_rule_groups(
            conn, [], ["default.example"], ["default statement"]
        )
        assert card_filter is None
        assert groups == [
            (
                {"axis.example", "icici.example"},
                {"axis card statement", "icici card statement"},
            ),
            ({"default.example"}, {"default statement"}),
        ]

        card_filter, groups = _scan_card_rule_groups(
            conn, [first], ["default.example"], ["default statement"]
        )
        assert card_filter.allowed == {"XX97"}
        assert card_filter.known == {"XX97", "XX07", "XX00"}
        assert groups == [({"axis.example"}, {"axis card statement"})]
    finally:
        conn.close()


def test_scan_card_rules_fall_back_only_when_no_saved_rules_exist(tmp_path):
    conn = store.connect(tmp_path / "default-card-rules.db")
    try:
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('Blank', 'XX00', 'Blank 00')"
        )
        card_filter, groups = _scan_card_rule_groups(
            conn, [], ["default.example"], ["default statement"]
        )
        assert card_filter is None
        assert groups == [({"default.example"}, {"default statement"})]
    finally:
        conn.close()


def test_incomplete_card_uses_saved_field_and_defaults_only_missing_field(tmp_path):
    conn = store.connect(tmp_path / "partial-card-rules.db")
    try:
        conn.execute(
            "INSERT INTO cards "
            "(issuer, masked_number, display_name, sender_ids_json, subject_patterns_json) "
            "VALUES ('Axis', 'XX97', 'Axis 97', '[\"axis.example\"]', '[]')"
        )
        card_filter, groups = _scan_card_rule_groups(
            conn, [], ["default.example"], ["default statement"]
        )
        assert card_filter is None
        assert groups == [({"axis.example"}, {"default statement"})]
    finally:
        conn.close()


def test_same_card_ignores_masking_style_and_separators():
    # The same card printed by two of its issuer's own templates.
    assert same_card("5394 94** **** 4321", "539494XXXXXX4321")
    # Axis widened the mask mid-2021; one physical card, two printed forms.
    assert same_card("53346700****8765", "533467******8765")
    # Two real cards sharing a BIN must not collapse into one.
    assert not same_card("485498XXXXXX1111", "485498XXXXXX2222")
    # Non-overlapping visible windows must not vacuously agree.
    assert not same_card("533467**********", "**********008765")
    # Different issuers' PAN lengths never match.
    assert not same_card("3561XXXXXXX1234", "3561XXXXXXXX1234")
    # The unknown-card fallback keys stay exact-match only.
    assert same_card("HDFC Bank-unknown", "HDFC Bank-unknown")
    assert not same_card("HDFC Bank-unknown", "Axis Bank-unknown")
    assert not same_card(None, None)


def test_unrecognized_cards_are_kept_only_when_asked_for(tmp_path):
    conn = store.connect(tmp_path / "unknown-card-scan.db")
    try:
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('HDFC', '485498XXXXXX2222', 'HDFC Regalia 2222')"
        )
        saved = conn.execute("SELECT id FROM cards").fetchone()[0]

        strict, _ = _scan_card_rule_groups(
            conn, [saved], ["default.example"], ["default statement"]
        )
        kept, why = strict.verdict("485498XXXXXX1111")
        assert not kept
        assert "Unrecognized cards" in why
        assert strict.verdict("5394 94** **** 4321")[0] is False

        widened, groups = _scan_card_rule_groups(
            conn, [saved], ["default.example"], ["default statement"],
            include_unrecognized=True,
        )
        assert widened.verdict("485498XXXXXX1111")[0] is True
        # A statement with no readable card number is unrecognized too.
        assert widened.verdict(None)[0] is True
        # A saved card left unticked stays excluded either way.
        assert widened.verdict("485498XXXXXX2222")[0] is True
        # Unimported cards have no saved mail rules, so the issuer defaults have
        # to be searched or the statement is never downloaded to be judged.
        assert ({"default.example"}, {"default statement"}) in groups
    finally:
        conn.close()


def test_untouched_saved_card_is_excluded_without_the_unrecognized_hint(tmp_path):
    conn = store.connect(tmp_path / "unticked-card-scan.db")
    try:
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('HDFC', '485498XXXXXX2222', 'HDFC Regalia 2222')"
        )
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('Axis', '539494******4321', 'Airtel Axis 4321')"
        )
        ticked = conn.execute(
            "SELECT id FROM cards WHERE masked_number = '485498XXXXXX2222'"
        ).fetchone()[0]

        card_filter, _ = _scan_card_rule_groups(
            conn, [ticked], ["default.example"], ["default statement"],
            include_unrecognized=True,
        )
        kept, why = card_filter.verdict("539494XXXXXX4321")
        assert not kept
        assert "not ticked" in why
    finally:
        conn.close()


def test_mixed_card_rules_run_two_mailbox_queries(tmp_path, monkeypatch):
    db_path = tmp_path / "two-query-scan.db"
    conn = store.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO cards "
            "(issuer, masked_number, display_name, sender_ids_json, subject_patterns_json) "
            "VALUES ('Axis', 'XX97', 'Axis 97', "
            "'[\"axis.example\"]', '[\"axis card statement\"]')"
        )
        conn.execute(
            "INSERT INTO cards (issuer, masked_number, display_name) "
            "VALUES ('Blank', 'XX00', 'Blank 00')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        mailbox, "accounts_from_store", lambda _conn: [mailbox.Account("me@example.com", "secret")]
    )
    monkeypatch.setattr(accounts, "mark", lambda *_args, **_kwargs: None)
    searches = []

    def fake_fetch(_account, _dest, **kwargs):
        searches.append((set(kwargs["senders"]), set(kwargs["subject_searches"])))
        return []

    monkeypatch.setattr(mailbox, "fetch_account", fake_fetch)
    run_scan(db_path, tmp_path / "inbox", {}, card_ids=[])

    assert searches == [
        ({"axis.example"}, {"axis card statement"}),
        (set(mailbox.STATEMENT_SENDERS), set(mailbox.SUBJECT_SEARCHES)),
    ]


def test_period_check_accepts_prior_period_credit_but_not_debit():
    statement = Statement(
        template_id="test", issuer="Test", doc_type=DocType.CREDIT_CARD,
        period_start=dt.date(2025, 2, 13), period_end=dt.date(2025, 3, 12),
        transactions=[
            Transaction(
                date=dt.date(2025, 1, 12), description="late refund",
                amount="1800", type=TxnType.CREDIT,
            )
        ],
    )
    accepted = transactions_within_period(statement)
    assert accepted.passed
    assert "prior-period credit" in accepted.detail

    statement.transactions[0].type = TxnType.DEBIT
    rejected = transactions_within_period(statement)
    assert not rejected.passed
    assert "debit(s) outside period" in rejected.detail


def test_prior_period_refund_is_analysed_in_statement_month(tmp_path):
    conn = store.connect(tmp_path / "dual-date.db")
    try:
        statement = Statement(
            template_id="test", issuer="Test Bank", doc_type=DocType.CREDIT_CARD,
            account_masked="XXXX1111", statement_date=dt.date(2025, 3, 12),
            period_start=dt.date(2025, 2, 13), period_end=dt.date(2025, 3, 12),
            transactions=[
                Transaction(
                    date=dt.date(2025, 1, 12), description="prior-cycle refund",
                    amount="1800", type=TxnType.CREDIT,
                )
            ],
        )
        store.import_statement(conn, statement)

        row = store.transactions(conn)[0]
        assert row["txn_date"] == "2025-01-12"
        assert row["statement_month"] == "2025-03"
        assert store.analytics(conn)["monthly"] == [
            {"month": "2025-03", "spend": -1800.0, "payments": 0.0}
        ]
    finally:
        conn.close()


def test_card_payment_is_not_treated_as_negative_spend(tmp_path):
    conn = store.connect(tmp_path / "payment-role.db")
    try:
        statement = Statement(
            template_id="test", issuer="Test Bank", doc_type=DocType.CREDIT_CARD,
            account_masked="XXXX2222", statement_date=dt.date(2025, 3, 12),
            period_start=dt.date(2025, 2, 13), period_end=dt.date(2025, 3, 12),
            transactions=[
                Transaction(
                    date=dt.date(2025, 3, 1), description="BBPS Payment received",
                    amount="1800", type=TxnType.CREDIT,
                )
            ],
        )
        store.import_statement(conn, statement)

        assert store.analytics(conn)["monthly"] == [
            {"month": "2025-03", "spend": 0.0, "payments": 1800.0}
        ]
    finally:
        conn.close()


def _axis_summary_glitch_statement(payment_description="BBPS PAYMENT RECEIVED - reference"):
    return Statement(
        template_id="axis_cc_legacy_v1",
        issuer="Axis Bank",
        doc_type=DocType.CREDIT_CARD,
        period_start=dt.date(2024, 8, 14),
        period_end=dt.date(2024, 9, 12),
        summary={
            "previous_dues": "2791.82",
            "payments_credits": "441.00",
            "purchases_debits": "5486.69",
            "finance_charges": "0",
            "total_dues": "5045.69",
        },
        transactions=[
            Transaction(
                date=dt.date(2024, 8, 20), description="purchases",
                amount="5486.69", type=TxnType.DEBIT,
            ),
            Transaction(
                date=dt.date(2024, 8, 29), description=payment_description,
                amount="2791.82", type=TxnType.CREDIT,
            ),
            Transaction(
                date=dt.date(2024, 9, 7), description="cashback and refunds",
                amount="441.00", type=TxnType.CREDIT,
            ),
        ],
    )


def _hdfc_offsetting_statement(credit_amount="1000.00"):
    """Summary counts ₹10.22 in both Purchase and Payments that no row prints."""
    return Statement(
        template_id="hdfc_cc_legacy_v1",
        issuer="HDFC Bank",
        doc_type=DocType.CREDIT_CARD,
        period_start=dt.date(2023, 8, 16),
        period_end=dt.date(2023, 9, 15),
        summary={
            "previous_dues": "500.00",
            "payments_credits": "1010.22",
            "purchases_debits": "2010.22",
            "finance_charges": "0",
            "total_dues": "1500.00",
        },
        transactions=[
            Transaction(
                date=dt.date(2023, 8, 20), description="RAZ*NAAMO Hyderabad",
                amount="2000.00", type=TxnType.DEBIT,
            ),
            Transaction(
                date=dt.date(2023, 8, 25), description="IMPS PMT",
                amount=credit_amount, type=TxnType.CREDIT,
            ),
        ],
    )


def test_hdfc_offsetting_summary_pair_passes_when_the_balance_reconciles():
    check = run_checks(_hdfc_offsetting_statement(), ["hdfc_legacy_side_totals"])[0]

    assert check.passed
    assert "10.22" in check.detail
    assert "the imported ledger is complete" in check.detail


def test_hdfc_one_sided_shortfall_remains_an_error():
    # A dropped credit row moves one side only, so the offset no longer cancels.
    check = run_checks(_hdfc_offsetting_statement("900.00"), ["hdfc_legacy_side_totals"])[0]

    assert not check.passed
    assert check.severity == "error"


def test_axis_legacy_proven_summary_glitch_is_visible_warning():
    statement = _axis_summary_glitch_statement()
    check = run_checks(statement, ["axis_legacy_summary_consistency"])[0]

    assert not check.passed
    assert check.severity == "warning"
    assert "glitch in the issuer-generated statement" in check.detail
    assert "verify the PDF once" in check.detail
    assert statement.ok


def test_axis_legacy_unexplained_credit_mismatch_remains_error():
    statement = _axis_summary_glitch_statement("UNIDENTIFIED CREDIT")
    check = run_checks(statement, ["axis_legacy_summary_consistency"])[0]

    assert not check.passed
    assert check.severity == "error"
    assert "manual parser review is required" in check.detail
