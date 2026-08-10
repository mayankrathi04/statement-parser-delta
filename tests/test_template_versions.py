import datetime as dt
from decimal import Decimal
import re

from sparser.emi import EmiEvidence, converted_purchase_keys
from sparser.engine import load_templates, match_template
from sparser.normalize import is_credit_marker


def test_legacy_hdfc_credit_marker_may_touch_amount():
    assert is_credit_marker("315.60Cr")


def test_axis_analyzer_is_selected_by_statement_month():
    templates = load_templates()
    old = """Axis Bank Credit Card Statement
Statement Period Payment Due Date Statement Generation Date
14/09/2025 - 12/10/2025 01/11/2025 12/10/2025
"""
    new = old.replace("12/10/2025", "12/11/2025")

    assert match_template(old, templates)["id"] == "axis_cc_legacy_v1"
    assert match_template(new, templates)["id"] == "axis_cc_v1"


def test_hdfc_legacy_layout_has_its_own_analyzer():
    text = """Visa Regalia Credit Card Statement
Statement for HDFC Bank Credit Card
Statement Date:15/07/2025
Account Summary
Opening Payment/ Purchase/ Finance
Domestic Transactions
"""
    assert match_template(text, load_templates())["id"] == "hdfc_cc_legacy_v1"


def test_hdfc_split_legacy_date_and_null_transaction_prefix_are_supported():
    text = """Visa Regalia Credit Card Statement
HDFC Bank Credit Cards GSTIN
Statement for HDFC Bank Credit Card
Email: person@example.com Statement Card No: XXXX
Address: example Date:15/06/2024
Account Summary
Opening Payment/ Purchase/ Finance
Domestic Transactions
"""
    templates = load_templates()
    legacy = match_template(text, templates)
    assert legacy["id"] == "hdfc_cc_legacy_v1"
    assert re.match(legacy["row"]["date_pattern"], "null08/10/2024 14:53 purchase")


def test_hdfc_current_layout_cannot_be_captured_by_legacy_template():
    current = """Visa Regalia Credit Card Statement
HDFC Bank Credit Cards GSTIN
Statement for HDFC Bank Credit Card
Statement Date:15/09/2025
Account Summary
PREVIOUS STATEMENT DUES
Domestic Transactions
"""
    future_old_headings = current.replace(
        "Statement Date:15/09/2025\nAccount Summary\nPREVIOUS STATEMENT DUES",
        "Statement Date:15/10/2025\nAccount Summary\nOpening Payment/ Purchase/ Finance",
    )
    templates = load_templates()
    assert match_template(current, templates)["id"] == "hdfc_cc_v1"
    assert match_template(future_old_headings, templates)["id"] == "hdfc_cc_v1"


def _emi_row(key, day, description, amount, direction="debit"):
    return EmiEvidence(
        key=key,
        date=dt.date(2025, 8, day),
        description=description,
        amount=Decimal(amount),
        direction=direction,
    )


def test_emi_eligibility_requires_conversion_evidence_and_honours_cancellation():
    purchase = _emi_row("purchase", 3, "GADGET WORLD ANDL CITY", "150000")
    assert converted_purchase_keys([purchase]) == set()

    conversion = _emi_row(
        "conversion", 6, "AGGREGATOR(cid:1)EMI(cid:1)-OFFUS(cid:1)CREDIT",
        "150000", "credit",
    )
    assert converted_purchase_keys([purchase, conversion]) == {"purchase"}

    cancellation = _emi_row(
        "cancellation", 21, "OFFUS EMI,LOAN CANCL,00000000000001", "150000"
    )
    assert converted_purchase_keys([purchase, conversion, cancellation]) == set()


def test_icici_first_amortization_proves_matching_purchase_conversion():
    rows = [
        _emi_row("purchase", 13, "FASHION RETAILER AND F", "45500"),
        _emi_row("conversion", 15, "FASHION RETAILER AND F", "45500", "credit"),
        _emi_row(
            "principal", 15,
            "Principal Amount Amortization - <1/6>FASHION RETAILER AND F",
            "14331.68",
        ),
    ]
    assert converted_purchase_keys(rows) == {"purchase"}


def test_icici_refund_foreclosure_cancels_only_the_matching_emi():
    def row(key, date, description, amount, direction="debit"):
        return EmiEvidence(
            key=key, date=date, description=description,
            amount=Decimal(amount), direction=direction,
        )

    january = [
        row("jan_purchase", dt.date(2026, 1, 18), "AMAZON PAY INDIA PVT LT", "28020.99"),
        row("jan_conversion", dt.date(2026, 1, 20), "AMAZON PAY INDIA PVT LT", "28020.99", "credit"),
        row("jan_1", dt.date(2026, 1, 20), "Principal Amount Amortization - <1/3>AMAZON PAY INDIA PVT LT", "9216.97"),
        row("jan_2", dt.date(2026, 2, 20), "Principal Amount Amortization - <2/3>AMAZON PAY INDIA PVT LT", "9339.79"),
    ]
    february = [
        row("feb_purchase", dt.date(2026, 2, 7), "AMAZON PAY INDIA PVT LT", "28000.99"),
        row("feb_conversion", dt.date(2026, 2, 10), "AMAZON PAY INDIA PVT LT", "28000.99", "credit"),
        row("feb_1", dt.date(2026, 2, 10), "Principal Amount Amortization - <1/3>AMAZON PAY INDIA PVT LT", "9210.39"),
    ]
    cancellation = [
        row("refund", dt.date(2026, 2, 23), "AMAZON PAY INDIA PVT LT", "27570.99", "credit"),
        row("payoff", dt.date(2026, 2, 25), "EMI PRINCIPAL", "9464.23"),
        row("reversal", dt.date(2026, 2, 25), "EMI PROCESSING FEE REVERSAL", "299", "credit"),
    ]

    assert converted_purchase_keys(january + february + cancellation) == {"feb_purchase"}
