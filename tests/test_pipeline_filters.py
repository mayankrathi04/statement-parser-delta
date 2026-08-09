import datetime as dt

import pytest

from sparser import store
from sparser.pipeline import month_range_window


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
