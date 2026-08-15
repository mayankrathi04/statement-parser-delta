"""Deposit-account statement parsers, one module per bank.

Bank dispatch is intentionally independent of the credit-card YAML engine. Every
bank prints its own table geometry, so each gets a parser that recognises its own
statements and refuses everything else; register the next bank in ``BANK_PARSERS``
and no card configuration or extraction code changes.
"""
from __future__ import annotations

from pathlib import Path

from . import hdfc, icici, idfc, indusind
from .base import (
    BankStatement,
    BankTransaction,
    UnsupportedBankStatement,
)

#: Tried in order. A parser that does not recognise the statement raises
#: UnsupportedBankStatement and the next one gets its turn.
BANK_PARSERS = (hdfc.parse, indusind.parse, idfc.parse, icici.parse)

__all__ = [
    "BANK_PARSERS",
    "BankStatement",
    "BankTransaction",
    "UnsupportedBankStatement",
    "parse_bank_pdf",
]


def parse_bank_pdf(path: str | Path) -> BankStatement:
    errors: list[str] = []
    for parser in BANK_PARSERS:
        try:
            return parser(path)
        except UnsupportedBankStatement as exc:
            errors.append(str(exc))
    raise UnsupportedBankStatement("; ".join(errors) or "no bank statement parser matched")
