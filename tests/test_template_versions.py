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
Domestic Transactions
"""
    assert match_template(text, load_templates())["id"] == "hdfc_cc_legacy_v1"
