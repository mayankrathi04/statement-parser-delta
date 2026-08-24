from sparser.doctype import classify_mail, document_kind
from sparser.mailbox import _gmail_search_query
import datetime as dt


def test_credit_card_statement_subject_is_accepted():
    ok, _ = classify_mail("Your HDFC Credit Card e-Statement for July")
    assert ok


def test_account_statement_subject_is_rejected():
    ok, reason = classify_mail("Your Account Statement for July")
    assert not ok
    assert "account statement" in reason.lower()


def test_card_only_issuer_statement_is_accepted():
    ok, _ = classify_mail(
        "Your monthly statement", sender="statements@americanexpress.com"
    )
    assert ok


def test_non_statement_bank_mail_is_rejected():
    ok, _ = classify_mail("Exclusive offers on your HDFC card")
    assert not ok


def test_pdf_text_identifies_bank_account_statement():
    kind, reason = document_kind(
        "Statement of Account Savings Account IFSC HDFC0001234 "
        "Cheque No Withdrawal Deposit Amount Closing Balance"
    )
    assert kind == "bank_account"
    assert "not a card statement" in reason


def test_pdf_text_identifies_credit_card_statement():
    kind, _ = document_kind(
        "Credit Card Statement Credit Limit Minimum Amount Due Payment Due Date"
    )
    assert kind == "credit_card"


def test_unknown_pdf_is_allowed_to_reach_templates():
    kind, _ = document_kind("Transactions Date Description Amount")
    assert kind == "unknown"


def test_gmail_search_combines_issuers_subjects_and_date_window():
    query = _gmail_search_query(
        ["hdfcbank.com", "axisbank.com"], dt.date(2026, 1, 25), dt.date(2026, 3, 11)
    )
    assert "after:2026/01/25" in query
    assert "before:2026/03/11" in query
    assert "from:hdfcbank.com" in query
    assert "from:axisbank.com" in query
    assert 'subject:\\"credit card statement\\"' in query
