"""Writers: Excel, CSV, JSON.

Excel is what users actually want; CSV is what their accountant wants; JSON is
what the next system wants. All three emit the same columns in the same order.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .schema import Statement, TxnType

COLUMNS = [
    ("date", "Date"),
    ("time", "Time"),
    ("description", "Description"),
    ("debit", "Debit"),
    ("credit", "Credit"),
    ("section", "Section"),
    ("cardholder", "Cardholder"),
    ("is_emi", "EMI"),
    ("reward_points", "Reward Points"),
    ("fcy_currency", "FCY"),
    ("fcy_amount", "FCY Amount"),
    ("page", "Page"),
]


def _rows(stmt: Statement) -> list[dict]:
    out = []
    for t in stmt.transactions:
        out.append(
            {
                "date": t.date.isoformat(),
                "time": t.time.strftime("%H:%M") if t.time else "",
                "description": t.description,
                "debit": t.amount if t.type is TxnType.DEBIT else None,
                "credit": t.amount if t.type is TxnType.CREDIT else None,
                "section": t.section,
                "cardholder": t.cardholder or "",
                "is_emi": "Y" if t.is_emi else "",
                "reward_points": t.reward_points,
                "fcy_currency": t.fcy_currency or "",
                "fcy_amount": t.fcy_amount,
                "page": t.page + 1,
            }
        )
    return out


def to_json(stmt: Statement, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(stmt.model_dump_json(indent=2))
    return path


def to_csv(stmt: Statement, path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[k for k, _ in COLUMNS])
        w.writerow({k: label for k, label in COLUMNS})
        for r in _rows(stmt):
            w.writerow({k: ("" if r[k] is None else r[k]) for k, _ in COLUMNS})
    return path


def to_xlsx(stmt: Statement, path: str | Path) -> Path:
    path = Path(path)
    wb = Workbook()

    ws = wb.active
    ws.title = "Transactions"
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3864")
    ws.append([label for _, label in COLUMNS])
    for c in ws[1]:
        c.font, c.fill = head, fill
        c.alignment = Alignment(horizontal="center")

    for r in _rows(stmt):
        ws.append([r[k] for k, _ in COLUMNS])

    for i, (key, label) in enumerate(COLUMNS, start=1):
        letter = get_column_letter(i)
        width = max([len(label)] + [len(str(r[key] or "")) for r in _rows(stmt)] or [10])
        ws.column_dimensions[letter].width = min(max(width + 2, 10), 60)
        if key in ("debit", "credit", "fcy_amount"):
            for cell in ws[letter][1:]:
                cell.number_format = "#,##0.00"
    ws.freeze_panes = "A2"

    meta = wb.create_sheet("Summary")
    s = stmt.summary
    for k, v in [
        ("Issuer", stmt.issuer),
        ("Product", stmt.product),
        ("Card", stmt.account_masked),
        ("Account Holder", stmt.account_holder),
        ("Statement Date", stmt.statement_date),
        ("Billing Period", f"{stmt.period_start} to {stmt.period_end}"),
        ("", ""),
        ("Previous Dues", s.previous_dues),
        ("Payments / Credits", s.payments_credits),
        ("Purchases / Debits", s.purchases_debits),
        ("Finance Charges", s.finance_charges),
        ("Total Amount Due", s.total_dues),
        ("Minimum Due", s.minimum_due),
        ("Due Date", s.due_date),
        ("Credit Limit", s.credit_limit),
        ("Available Credit", s.available_credit),
        ("", ""),
        ("Template", stmt.template_id),
        ("Transactions", len(stmt.transactions)),
        ("Confidence", stmt.confidence),
    ]:
        meta.append([k, v if v is not None else ""])
    for c in meta["A"]:
        c.font = Font(bold=True)
    meta.column_dimensions["A"].width = 22
    meta.column_dimensions["B"].width = 40

    checks = wb.create_sheet("Validation")
    checks.append(["Check", "Result", "Severity", "Detail"])
    for c in checks[1]:
        c.font, c.fill = head, fill
    for ck in stmt.checks:
        checks.append([ck.name, "PASS" if ck.passed else "FAIL", ck.severity, ck.detail])
    for col, w in zip("ABCD", (32, 10, 10, 90)):
        checks.column_dimensions[col].width = w

    wb.save(path)
    return path


WRITERS = {"json": to_json, "csv": to_csv, "xlsx": to_xlsx}
