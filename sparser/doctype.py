"""Is this a credit-card statement? Two gates, asked of two different things.

The **mail** is judged from its headers. That is cheap and lets us skip the
download entirely, but it is only ever a guess: issuers word their subjects
differently, and a bank sends card statements and savings-account statements
from the same address with nearly the same words.

The **PDF** is judged from its own text, and that is the ground truth. It is the
gate that matters. A savings-account statement whose subject slipped past the
header rules is still refused here, before it can pollute card analytics with
salary credits and ATM withdrawals.

Both gates always explain themselves, because a filter you cannot see is a
filter you cannot trust — every skipped mail is recorded with its subject and
the reason, so an over-strict rule is visible rather than silent.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------- mail headers

# "This is your card statement", however the issuer chose to phrase it. The
# gap in the middle absorbs the words issuers slip in — "Credit Card *Account*
# Statement", "Credit Card *e-*Statement", "Credit Card *Monthly* Statement" —
# and the second alternative catches the reversed "Statement for your Credit Card".
CARD_MAIL = re.compile(
    r"credit[-\s]*card[\w\s./'-]{0,24}?(e[-\s]?)?statement"
    r"|(e[-\s]?)?statement[\w\s./'-]{0,24}?credit[-\s]*card"
    r"|credit[-\s]*card[\w\s./'-]{0,24}?bill\b"
    r"|card[-\s]*member[\w\s]{0,12}?statement"
    r"|\bcard[-\s]*(e[-\s]?)?statement",
    re.I,
)

# Unmistakably a different product. Only consulted when nothing above matched,
# so "Credit Card Account Statement" is not vetoed by its own middle word.
NOT_CARD_MAIL = re.compile(
    r"(savings|current|salary|deposit|nre|nro)[-\s]*account"
    r"|account[-\s]*statement"
    r"|statement[-\s]*of[-\s]*account"
    r"|bank[-\s]*statement"
    r"|consolidated[-\s]*account"
    r"|mutual[-\s]*fund|demat|portfolio|folio|\bnps\b|\bppf\b|\bsip\b"
    r"|(home|personal|auto|car|education|gold|two[-\s]*wheeler)[-\s]*loan"
    r"|(fixed|recurring)[-\s]*deposit"
    r"|insurance|\bpolicy\b"
    r"|interest[-\s]*certificate"
    r"|\btds\b|form[-\s]*16|\bgst\b",
    re.I,
)

ANY_STATEMENT = re.compile(r"\bstatement\b|\bbill\b", re.I)

# Issuers with no other product to confuse us with: anything they call a
# statement is a card statement, which is how "SBI Card Monthly Statement" and
# "Your American Express Statement" get through without saying "credit card".
CARD_ONLY_SENDERS = ("sbicard.com", "americanexpress.com", "amex.com", "onecard.in")


def classify_mail(subject: str, filename: str = "", sender: str = "") -> tuple[bool, str]:
    """Should this mail be downloaded? Returns (accept, human-readable reason)."""
    text = f"{subject} {filename}"
    if CARD_MAIL.search(text):
        return True, "subject names a credit card statement"
    if NOT_CARD_MAIL.search(text):
        hit = NOT_CARD_MAIL.search(text).group(0).strip()
        return False, f"not a card statement — subject says {hit!r}"
    low = sender.lower()
    house = next((d for d in CARD_ONLY_SENDERS if d in low), None)
    if house and ANY_STATEMENT.search(text):
        return True, f"statement from {house}, which issues cards only"
    return False, "no credit-card statement wording in the subject"


# ------------------------------------------------------------------ PDF text

# Phrases that appear on card statements and essentially nowhere else. A credit
# limit is the giveaway: no deposit account has one.
CARD_MARKERS = (
    "credit limit", "available credit limit", "cash limit", "minimum amount due",
    "min amount due", "total amount due", "total dues", "payment due date",
    "credit card statement", "reward points", "statement period", "card number",
    "opening balance", "finance charge",
)

# Phrases that appear on deposit-account statements and essentially nowhere on a
# card statement. IFSC/MICR are routing codes only a bank account has.
BANK_MARKERS = (
    "ifsc", "micr", "account statement", "statement of account", "savings account",
    "current account", "account branch", "nomination", "cheque no", "chq no",
    "withdrawal", "deposit amount", "closing balance", "value date",
    "account holder", "branch code",
)


def document_kind(text: str) -> tuple[str, str]:
    """Classify extracted PDF text as credit_card / bank_account / unknown.

    Deliberately asymmetric. Only a clear bank-account verdict is actionable —
    'unknown' proceeds to fingerprinting, because a card statement from an
    issuer we have never seen must not be thrown away by a phrase list.
    """
    low = " ".join((text or "").lower().split())
    card = [m for m in CARD_MARKERS if m in low]
    bank = [m for m in BANK_MARKERS if m in low]

    if len(card) >= 2 and len(card) > len(bank):
        return "credit_card", f"card statement — found {', '.join(card[:4])}"
    if len(bank) >= 2 and len(bank) > len(card):
        return "bank_account", (
            f"reads as a bank account statement, not a card statement — found "
            f"{', '.join(bank[:4])}"
        )
    return "unknown", "no decisive card or account wording; letting the templates decide"
