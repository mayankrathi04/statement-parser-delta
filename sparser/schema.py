"""Canonical output schema. Every template must produce these types."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TxnType(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class DocType(str, Enum):
    CREDIT_CARD = "credit_card"
    BANK_ACCOUNT = "bank_account"


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt.date
    time: Optional[dt.time] = None
    description: str
    amount: Decimal = Field(description="Positive magnitude; direction lives in `type`")
    type: TxnType
    balance: Optional[Decimal] = Field(
        default=None, description="Running balance where the document prints one (bank statements)"
    )
    section: str = "domestic"
    category: Optional[str] = Field(
        default=None, description="Issuer-printed merchant category where the statement has one"
    )
    merchant: Optional[str] = Field(
        default=None, description="Description reduced to a comparable merchant name"
    )
    cardholder: Optional[str] = None
    card_masked: Optional[str] = Field(
        default=None,
        description="The card this row was printed under, where the statement groups "
                    "its table by card. None when the document covers a single card.",
    )
    is_emi: bool = False
    reward_points: Optional[int] = None
    fcy_currency: Optional[str] = None
    fcy_amount: Optional[Decimal] = None
    page: int = 0
    raw: str = ""

    @property
    def signed(self) -> Decimal:
        """Debits positive, credits negative — matches card-issuer dues arithmetic."""
        return self.amount if self.type is TxnType.DEBIT else -self.amount


class Summary(BaseModel):
    """The issuer's own totals box. This is the ground truth we reconcile against."""

    model_config = ConfigDict(extra="forbid")

    previous_dues: Optional[Decimal] = None
    payments_credits: Optional[Decimal] = None
    purchases_debits: Optional[Decimal] = None
    finance_charges: Optional[Decimal] = None
    total_dues: Optional[Decimal] = None
    minimum_due: Optional[Decimal] = None
    due_date: Optional[dt.date] = None
    credit_limit: Optional[Decimal] = None
    available_credit: Optional[Decimal] = None
    available_cash: Optional[Decimal] = None


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    severity: str = "error"  # error | warning


class Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    issuer: str
    doc_type: DocType
    product: Optional[str] = None
    account_holder: Optional[str] = None
    account_masked: Optional[str] = None
    statement_date: Optional[dt.date] = None
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    currency: str = "INR"
    summary: Summary = Field(default_factory=Summary)
    transactions: list[Transaction] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    source_file: str = ""

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def confidence(self) -> float:
        """Blunt but honest: fraction of checks passed, weighted to errors."""
        if not self.checks:
            return 0.0
        weights = {"error": 1.0, "warning": 0.3}
        total = sum(weights[c.severity] for c in self.checks)
        got = sum(weights[c.severity] for c in self.checks if c.passed)
        return round(got / total, 4) if total else 0.0
