import datetime as dt

from sparser.mailbox import _download_path, _gmail_search_query


def test_same_named_different_attachments_get_stable_collision_paths(tmp_path):
    args = (tmp_path, "you@gmail.com", "Statement", "Credit_Card_Statement.pdf")
    when = dt.datetime(2026, 6, 10)

    first, exists = _download_path(*args, when, b"101", 1, b"airtel")
    assert not exists
    first.write_bytes(b"airtel")

    repeated, exists = _download_path(*args, when, b"101", 1, b"airtel")
    assert (repeated, exists) == (first, True)

    flipkart, exists = _download_path(*args, when, b"202", 1, b"flipkart")
    assert not exists
    assert flipkart.name == "202606_you_Credit_Card_Statement_uid202_1.pdf"
    flipkart.write_bytes(b"flipkart")

    repeated_flipkart, exists = _download_path(*args, when, b"202", 1, b"flipkart")
    assert (repeated_flipkart, exists) == (flipkart, True)


def test_gmail_query_requires_sender_subject_and_pdf_attachment():
    query = _gmail_search_query(
        ["statements@axisbank.com"],
        dt.date(2026, 1, 1),
        dt.date(2026, 2, 1),
        ["Flipkart Axis Bank Credit Card Statement"],
    )
    assert "from:statements@axisbank.com" in query
    assert 'subject:\\"Flipkart Axis Bank Credit Card Statement\\"' in query
    assert "has:attachment filename:pdf" in query
