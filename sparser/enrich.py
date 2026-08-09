"""Turn raw statement descriptions into things you can group by.

Statement descriptions are machine-mangled: the merchant, its outlet and its city
are concatenated without separators ("ZOMATONEW DELHI", "TATA 1MG HEALTHCAREDELHI")
and gateway prefixes ride in front ("RSP*", "PYU*", "UPI_"). Aggregates are
meaningless until those collapse to one name per merchant.

Categories prefer what the issuer printed — Axis and YES Bank supply a merchant
category on every row, which beats anything guessed here. Rules only fill the gap
for issuers that print none.
"""
from __future__ import annotations

import re
from typing import Optional

# Payment gateways and rails that prefix the real merchant name.
_GATEWAY = re.compile(
    r"^(?:RSP\*|PYU\*|PAY\*|PTM\*|RAZ\*|UPI[_-]|BPPY\s|IND\*|MSWIPE\*|PAYU\*)", re.I
)
# Trailing city/location noise, glued on without a separator.
_CITIES = (
    "BANGALORE|BENGALURU|HYDERABAD|NEW DELHI|DELHI|MUMBAI|CHENNAI|KOLKATA|PUNE|GURGAON|"
    "GURUGRAM|NOIDA|NILGIRIS|COIMBATORE|OOTY|COONOOR|K V RANGAR|GORAKHPUR|LONDON|JAIPUR|"
    "AHMEDABAD|LUCKNOW|INDORE|THANE|SURAT|NAGPUR|IN"
)
_TRAIL_CITY = re.compile(rf"[\s,]*(?:{_CITIES})\b\.?$", re.I)
_REF = re.compile(r"\s*[-–]?\s*(?:Ref\s*No|Ref#)\s*[:.]?\s*\S+.*$", re.I)

# Ordered: first match wins, so specific rules precede general ones.
_CATEGORY_RULES: list[tuple[str, re.Pattern]] = [
    ("Payment", re.compile(r"payment received|cc payment|bbps|autopay|neft|imps", re.I)),
    ("Cashback & Rewards", re.compile(r"cashback|surcharge waiver|reward|neucoin|adj\b", re.I)),
    ("Fees & Charges", re.compile(r"\b(?:igst|cgst|sgst|gst|markup fee|fee|charge|interest)\b", re.I)),
    ("Groceries", re.compile(r"zepto|blinkit|blink commerce|bigbasket|dmart|grocer|superfood|instamart", re.I)),
    ("Food & Dining", re.compile(r"zomato|swiggy|bundl|restaurant|cafe|kitchen|pizza|thali|bakers?|bake|annapoorna|niloufer|dhaba|hotel.*food", re.I)),
    ("Health & Pharmacy", re.compile(r"apollo|pharmac|1mg|hospital|clinic|diagnost|medical|medplus", re.I)),
    ("Shopping", re.compile(r"amazon|flipkart|myntra|meesho|ajio|nykaa|retail|dept stores|store", re.I)),
    ("Travel & Transport", re.compile(r"irctc|indigo|makemytrip|cleartrip|uber|ola|rapido|airlines|travel|mahindra|club mahindra", re.I)),
    ("Fuel", re.compile(r"petrol|fuel|hpcl|bpcl|iocl|indian oil", re.I)),
    ("Utilities & Telecom", re.compile(r"airtel|jio|vodafone|electricity|utility|utilities|broadband|gas\b|recharge|axis bank ind", re.I)),
    ("Entertainment", re.compile(r"netflix|youtube|spotify|prime video|hotstar|pvr|inox|cinema|bookmyshow", re.I)),
    ("Software & Services", re.compile(r"google|microsoft|openai|adobe|turboscribe|vpn|nord|github|aws|cheq digital", re.I)),
    ("Beauty & Wellness", re.compile(r"salon|spa|beauty|hair|urban company|pebble", re.I)),
]

# Issuer category strings vary in wording; fold them onto the same buckets so a
# card that prints categories and one that does not can be charted together.
_ISSUER_CATEGORY_MAP = {
    "DEPT STORES": "Groceries",
    "RESTAURANTS": "Food & Dining",
    "MISCELLANEOUS": "Other",
    "UTILITIES": "Utilities & Telecom",
    "BUSINESS SERVICES": "Software & Services",
    "UTILITY SERVICES": "Utilities & Telecom",
    "HEALTH SERVICES": "Health & Pharmacy",
}


def merchant_name(description: str) -> str:
    """Reduce a description to a name that groups across statements."""
    s = _REF.sub("", description or "").strip()
    s = _GATEWAY.sub("", s).strip()
    s = re.split(r"\s*[-–]\s*(?=[A-Z0-9]{8,}$)", s)[0]
    prev = None
    while prev != s:  # a name can end with several stacked location tokens
        prev = s
        s = _TRAIL_CITY.sub("", s).strip(" ,.-")
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    return (s or (description or "").strip())[:60]


def categorize(description: str, issuer_category: Optional[str] = None) -> str:
    """Issuer-printed category wins; rules only fill the gap."""
    if issuer_category:
        key = issuer_category.strip().upper()
        mapped = _ISSUER_CATEGORY_MAP.get(key)
        if mapped:
            return mapped
        if key and key != "MISCELLANEOUS":
            return issuer_category.strip().title()
    for name, rx in _CATEGORY_RULES:
        if rx.search(description or ""):
            return name
    return "Other"
