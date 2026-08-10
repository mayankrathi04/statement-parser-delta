"""Evidence-based classification of purchases converted to EMI.

An issuer may print ``EMI`` beside a purchase merely to advertise that it is
eligible for conversion.  A converted purchase needs stronger evidence: a
matching conversion credit, or an issuer-specific first amortization record.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Hashable, Iterable


@dataclass(frozen=True)
class EmiEvidence:
    key: Hashable
    date: dt.date
    description: str
    amount: int | Decimal
    direction: str


@dataclass
class EmiClassification:
    active_purchases: set[Hashable] = field(default_factory=set)
    cancelled_purchases: set[Hashable] = field(default_factory=set)
    conversion_credits: set[Hashable] = field(default_factory=set)
    principal_rows: set[Hashable] = field(default_factory=set)
    cancellation_rows: set[Hashable] = field(default_factory=set)

    @property
    def excluded_from_spend(self) -> set[Hashable]:
        """Bookkeeping rows that must not become new purchase spending."""
        return (
            self.cancelled_purchases
            | self.conversion_credits
            | self.principal_rows
            | self.cancellation_rows
        )


_EXPLICIT_CONVERSION = re.compile(
    r"(?:\bTRANSACTION\s+CONVERSION\s+INTO\s+EMI\b|"
    r"\bAGGREGATOR\b.*\bEMI\b.*\bCREDIT\b)",
    re.I,
)
_CANCELLATION = re.compile(r"\bEMI\b.*\b(?:LOAN\s+CANCL|CANCELL?ATION|CANCELLED)\b", re.I)
_PRINCIPAL_AMORTIZATION = re.compile(
    r"\bPRINCIPAL\s+AMOUNT\s+AMORTIZATION\s*-\s*<(\d+)/(\d+)>\s*(.+)", re.I
)
_EMI_PRINCIPAL = re.compile(r"^EMI\s+PRINCIPAL$", re.I)
_PRINCIPAL_BOOKKEEPING = re.compile(r"\bEMI\b.*\b(?:PRIN|PRN|PRINCIPAL)\b", re.I)
_CANCELLATION_REVERSAL = re.compile(
    r"(?:REVERSAL\s+INTEREST\s+AMOUNT\s+AMORTIZATION|EMI\s+PROCESSING\s+FEE\s+REVERSAL)",
    re.I,
)


def _plain(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()


def _explicit_conversion(text: str) -> bool:
    return bool(_EXPLICIT_CONVERSION.search(_plain(text)))


def _cancellation(text: str) -> bool:
    return bool(_CANCELLATION.search(_plain(text)))


def _principal_amortization(text: str) -> tuple[int, int, str] | None:
    match = _PRINCIPAL_AMORTIZATION.search(text or "")
    return (
        int(match.group(1)), int(match.group(2)), _plain(match.group(3))
    ) if match else None


def _first_amortization_merchant(text: str) -> str | None:
    parsed = _principal_amortization(text)
    return parsed[2] if parsed and parsed[0] == 1 else None


def _same_merchant(merchant: str, description: str) -> bool:
    description_key = _plain(description)
    return bool(merchant) and (merchant in description_key or description_key in merchant)


def _principal_bookkeeping(text: str) -> bool:
    return bool(
        _principal_amortization(text)
        or _EMI_PRINCIPAL.match(_plain(text))
        or _PRINCIPAL_BOOKKEEPING.search(_plain(text))
    )


def _icici_cancellation_rows(
    conversion: EmiEvidence, records: list[EmiEvidence]
) -> set[Hashable]:
    """Recognize ICICI's discount-adjusted refund and foreclosure sequence.

    A no-cost EMI refund can be smaller than the original purchase, so amount
    equality alone cannot link it.  ICICI closes the loan by charging the exact
    unpaid principal and reversing prior interest/GST/processing fees.  Matching
    that remaining-principal equation identifies the cancelled loan even when
    several similar Amazon EMIs overlap.
    """
    first_rows: list[tuple[EmiEvidence, int, str]] = []
    for row in records:
        parsed = _principal_amortization(row.description)
        if (
            row.direction == "debit"
            and row.date == conversion.date
            and parsed
            and parsed[0] == 1
            and _same_merchant(parsed[2], conversion.description)
        ):
            first_rows.append((row, parsed[1], parsed[2]))
    if not first_rows:
        return set()

    # Several loans for the same merchant can start on one date.  The first
    # principal closest to purchase/tenure is the corresponding schedule.
    first, tenure, merchant = min(
        first_rows,
        key=lambda item: abs(conversion.amount / item[1] - item[0].amount),
    )

    refunds = [
        row for row in records
        if row.direction == "credit"
        and conversion.date < row.date <= conversion.date + dt.timedelta(days=120)
        and conversion.amount * Decimal("0.80") <= row.amount <= conversion.amount
        and _same_merchant(merchant, row.description)
    ]
    payoffs = [
        row for row in records
        if row.direction == "debit" and _EMI_PRINCIPAL.match(_plain(row.description))
    ]

    for refund in refunds:
        for payoff in payoffs:
            if not (refund.date <= payoff.date <= refund.date + dt.timedelta(days=7)):
                continue
            reversal_seen = any(
                row.direction == "credit"
                and refund.date <= row.date <= payoff.date + dt.timedelta(days=1)
                and _CANCELLATION_REVERSAL.search(_plain(row.description))
                for row in records
            )
            if not reversal_seen:
                continue

            paid = [first]
            previous = first
            for installment in range(2, tenure + 1):
                candidates = []
                for row in records:
                    parsed = _principal_amortization(row.description)
                    if not parsed or parsed[0] != installment or parsed[1] != tenure:
                        continue
                    if not _same_merchant(merchant, parsed[2]):
                        continue
                    gap = row.date - previous.date
                    if dt.timedelta(days=20) <= gap <= dt.timedelta(days=45) and row.date < payoff.date:
                        candidates.append(row)
                if not candidates:
                    break
                previous = min(candidates, key=lambda row: abs(row.amount - previous.amount))
                paid.append(previous)

            if conversion.amount - sum(row.amount for row in paid) == payoff.amount:
                return {refund.key, payoff.key}
    return set()


def classify_emi_rows(rows: Iterable[EmiEvidence]) -> EmiClassification:
    """Classify converted purchases and their non-spend bookkeeping rows.

    Explicit Axis/HDFC conversion credits are matched to the nearest preceding
    same-amount debit within 45 days.  ICICI uses an ordinary merchant credit;
    there we additionally require a same-day ``<1/n>`` principal-amortization
    row whose merchant text matches the credit.  A later same-amount loan
    cancellation invalidates the conversion.
    """
    records = sorted(rows, key=lambda row: (row.date, str(row.key)))
    credits = [row for row in records if row.direction == "credit"]
    debits = [row for row in records if row.direction == "debit"]
    cancellations = [row for row in debits if _cancellation(row.description)]
    result = EmiClassification(
        principal_rows={row.key for row in records if _principal_bookkeeping(row.description)}
    )

    conversions: list[EmiEvidence] = [
        row for row in credits if _explicit_conversion(row.description)
    ]

    # ICICI conversion credits retain the merchant description instead of
    # saying "conversion".  The first principal-amortization row on the same
    # date is the unambiguous evidence that distinguishes one from a refund.
    for debit in debits:
        merchant = _first_amortization_merchant(debit.description)
        if not merchant:
            continue
        merchant_key = _plain(merchant)
        for credit in credits:
            if credit.date != debit.date:
                continue
            credit_key = _plain(credit.description)
            if merchant_key and (merchant_key in credit_key or credit_key in merchant_key):
                conversions.append(credit)

    # A credit can be discovered from more than one amortization row.
    conversions = list({row.key: row for row in conversions}.values())
    result.conversion_credits = {row.key for row in conversions}

    for conversion in conversions:
        candidates = [
            row
            for row in debits
            if row.amount == conversion.amount
            and row.date <= conversion.date
            and conversion.date - row.date <= dt.timedelta(days=45)
            and not _cancellation(row.description)
            and not _first_amortization_merchant(row.description)
        ]
        if not candidates:
            continue

        # Conversion normally follows the purchase by two or three days.  When
        # duplicate amounts exist, the most recent debit is the defensible link.
        purchase = max(candidates, key=lambda row: (row.date, str(row.key)))

        explicit_cancellations = {
            cancel.key
            for cancel in cancellations
            if cancel.amount == conversion.amount
            and conversion.date <= cancel.date <= conversion.date + dt.timedelta(days=120)
        }
        icici_cancellations = _icici_cancellation_rows(conversion, records)
        cancellation_rows = explicit_cancellations | icici_cancellations
        if cancellation_rows:
            result.cancelled_purchases.add(purchase.key)
            result.cancellation_rows.update(cancellation_rows)
            continue
        result.active_purchases.add(purchase.key)

    return result


def converted_purchase_keys(rows: Iterable[EmiEvidence]) -> set[Hashable]:
    """Return purchases backed by conversion evidence and not later cancelled."""
    return classify_emi_rows(rows).active_purchases
