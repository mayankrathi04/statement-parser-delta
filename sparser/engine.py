"""YAML-driven template engine.

Adding a bank should mean adding a YAML file, not editing Python. Everything
issuer-specific — section markers, row rules, summary anchors — lives in the
template; this module only interprets it.

The one thing templates deliberately do *not* pin is column x-positions. The
same statement renders its transaction table at several x-origins (HDFC insets
the page-1 block ~144pt relative to page 2) and labels the rewards column
differently per card product. So bands are measured from each block's own
header row at parse time, which is what makes one template cover the whole
product family instead of one PDF.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pdfplumber
import yaml

from . import geometry as geo
from .emi import EmiEvidence, converted_purchase_keys
from .normalize import (
    is_currency_amount,
    is_decimal_amount,
    is_credit_amount_in_row,
    is_credit_marker,
    parse_signed_amount,
    parse_amount,
    parse_date,
    parse_int,
    parse_time,
    squash,
)
from .schema import DocType, Statement, Summary, Transaction, TxnType

TEMPLATE_DIR = Path(__file__).parent / "templates"

_BAND_EXPR = re.compile(r"^\s*(\w+)\.(x0|x1)\s*(?:([+-])\s*([\d.]+))?\s*$")


class NoTemplateMatch(RuntimeError):
    pass


class TemplateError(RuntimeError):
    pass


def load_templates(directory: Path = TEMPLATE_DIR) -> list[dict[str, Any]]:
    return [yaml.safe_load(p.read_text()) for p in sorted(directory.glob("*.yaml"))]


def _template_statement_date(page1_text: str, template: dict) -> Optional[dt.date]:
    """Read the statement date cheaply, before committing to an analyzer.

    Issuers sometimes keep the same identifying text while changing the
    accounting treatment or table layout.  Templates can therefore declare an
    effective date range in addition to their content fingerprint.
    """
    pattern = template.get("statement_date_pattern")
    if not pattern:
        return None
    match = re.search(pattern, page1_text, re.MULTILINE)
    if not match:
        return None
    return parse_date(match.group(1), template.get("date_formats", ["%d/%m/%Y"]))


def _template_effective(template: dict, statement_date: Optional[dt.date]) -> bool:
    if statement_date is None:
        return not (template.get("valid_from") or template.get("valid_through"))

    def boundary(name: str) -> Optional[dt.date]:
        value = template.get(name)
        if not value:
            return None
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(value)

    valid_from = boundary("valid_from")
    valid_through = boundary("valid_through")
    return (valid_from is None or statement_date >= valid_from) and (
        valid_through is None or statement_date <= valid_through
    )


def match_template(page1_text: str, templates: list[dict]) -> dict:
    for t in templates:
        fp = t.get("fingerprint", {})
        if all(s in page1_text for s in fp.get("all", [])) and (
            not fp.get("any") or any(s in page1_text for s in fp["any"])
        ) and _template_effective(t, _template_statement_date(page1_text, t)):
            return t
    raise NoTemplateMatch("no template fingerprint matched this document")


Band = tuple[float, float]


class Engine:
    def __init__(self, template: dict):
        self.t = template
        self.glyph_fixes = template.get("glyph_fixes", {})
        self.date_formats: list[str] = template.get("date_formats", ["%d/%m/%Y"])

    # -------------------------------------------------------------- bands

    def _clusters(self, header: geo.Line) -> dict[str, Band]:
        """Group the header row's words into columns by horizontal gap."""
        tcfg = self.t["table"]
        gap = tcfg.get("cluster_gap", 8)
        groups: list[list[geo.Word]] = []
        for w in header.words:
            if groups and w.x0 - groups[-1][-1].x1 < gap:
                groups[-1].append(w)
            else:
                groups.append([w])

        # A role suffixed with "?" is optional: the Airtel Axis card prints the
        # same table without the cashback column, and one template should cover
        # both rather than forking into a near-duplicate file.
        declared = tcfg["header_clusters"]
        roles = [r.rstrip("?") for r in declared]
        optional = [r.endswith("?") for r in declared]
        # Drop optional roles right-to-left until the declaration matches what
        # this card actually prints. The HDFC Swiggy variant omits the rewards
        # column entirely, and it sits mid-table rather than at the end.
        while len(groups) < len(roles):
            idx = next((i for i in range(len(roles) - 1, -1, -1) if optional[i]), None)
            if idx is None:
                break
            roles.pop(idx)
            optional.pop(idx)
        # Some issuers print a sidebar caption on the same visual line as the
        # header ("SPENDS OVERVIEW" beside ICICI's Date column). Aligning the
        # roles to the right-hand clusters ignores that leading noise.
        if tcfg.get("header_align") == "right" and len(groups) > len(roles):
            groups = groups[-len(roles) :]
        if len(groups) != len(roles):
            raise TemplateError(
                f"header row has {len(groups)} columns, template expects {len(roles)}: "
                + " | ".join(" ".join(w.text for w in g) for g in groups)
            )
        return {r: (g[0].x0, g[-1].x1) for r, g in zip(roles, groups)}

    def _eval(self, expr: str, clusters: dict[str, Band]) -> float:
        """Evaluate a band edge, trying "||"-separated alternatives in order.

        An edge often wants to butt against an optional neighbour ("stop where
        the cashback column starts") and needs a fallback when that column is
        absent from this card's layout.
        """
        for alt in expr.split("||"):
            m = _BAND_EXPR.match(alt)
            if not m:
                raise TemplateError(f"bad band expression: {alt!r}")
            role, attr, sign, num = m.groups()
            if role not in clusters:
                continue
            base = clusters[role][0 if attr == "x0" else 1]
            if sign:
                base += float(num) * (1 if sign == "+" else -1)
            return base
        raise TemplateError(f"no resolvable column in band expression {expr!r}")

    def _bands_for(self, header: geo.Line, section: str) -> dict[str, Band]:
        tcfg = self.t["table"]
        clusters = self._clusters(header)
        specs = dict(tcfg["bands"])
        specs.update(tcfg.get("bands_by_section", {}).get(section, {}))
        bands: dict[str, Band] = {}
        for name, (lo, hi) in specs.items():
            try:
                bands[name] = (self._eval(lo, clusters), self._eval(hi, clusters))
            except TemplateError:
                continue  # a band for a column this layout does not have
        return bands

    # --------------------------------------------------------------- rows

    def _is_drop(self, text: str) -> bool:
        return any(re.search(p, text) for p in self.t["row"].get("drop_patterns", []))

    def _section_at(self, text: str) -> Optional[str]:
        return next(
            (v for k, v in self.t["sections"]["starts"].items() if text.startswith(k)), None
        )

    def _parse_tables(self, lines: list[geo.Line]) -> list[Transaction]:
        tcfg, secs = self.t["table"], self.t["sections"]
        txns: list[Transaction] = []
        i, n = 0, len(lines)

        # Some issuers repeat only the column header when a table spills onto the
        # next page, with no section title above it. Treating the header itself
        # as the block opener is what keeps page 2 onwards from being dropped.
        header_starts = tcfg.get("header_starts_block", False)
        default_section = tcfg.get("default_section", "domestic")
        # A combined statement's card headings carry across page breaks: the
        # table resumes overleaf under the same card, without repeating it.
        card: Optional[str] = None

        while i < n:
            if header_starts:
                if not all(tok in lines[i].text for tok in tcfg["header_tokens"]):
                    i += 1
                    continue
                section = self._section_at(lines[i].text) or default_section
                hdr = i
            else:
                section = self._section_at(lines[i].text)
                if section is None:
                    i += 1
                    continue
                # The header row follows the section title, sometimes after a blank.
                hdr = next(
                    (
                        j
                        for j in range(i + 1, min(i + 4, n))
                        if all(tok in lines[j].text for tok in tcfg["header_tokens"])
                    ),
                    None,
                )
                if hdr is None:
                    i += 1
                    continue

            bands = self._bands_for(lines[hdr], section)
            block, i = self._collect_block(lines, hdr + 1, secs["ends"])
            rows, card = self._rows_from_block(block, bands, section, card)
            txns += rows

        evidence = [
            EmiEvidence(
                key=index,
                date=txn.date,
                description=txn.description,
                amount=txn.amount,
                direction=txn.type.value,
            )
            for index, txn in enumerate(txns)
        ]
        converted = converted_purchase_keys(evidence)
        for index, txn in enumerate(txns):
            txn.is_emi = index in converted
        return txns

    def _collect_block(
        self, lines: list[geo.Line], start: int, ends: list[str]
    ) -> tuple[list[geo.Line], int]:
        """Lines belonging to one table, stopping at an end marker or new section."""
        out: list[geo.Line] = []
        i, n = start, len(lines)
        page = lines[start].page if start < n else 0
        while i < n:
            text = lines[i].text
            if not text:
                i += 1
                continue
            if any(e in text for e in ends) or lines[i].page != page:
                break
            if self._section_at(text) is not None:
                break
            out.append(lines[i])
            i += 1
        return out, i

    def _rows_from_block(
        self,
        block: list[geo.Line],
        bands: dict[str, Band],
        section: str,
        card: Optional[str] = None,
    ) -> tuple[list[Transaction], Optional[str]]:
        """The rows in one table block, and the card heading still in force after it.

        Issuers that bill several cards on one statement print the table grouped
        by card, each group opening with the card number on a line of its own.
        Those lines are tracked exactly like cardholder names — a row belongs to
        the last heading above it — so a row is attributed to the card it was
        actually printed under rather than to the statement's primary card.
        """
        rowcfg = self.t["row"]
        date_re = re.compile(rowcfg["date_pattern"])
        holder_re = re.compile(rowcfg["cardholder_pattern"])
        card_re = re.compile(rowcfg["card_pattern"]) if rowcfg.get("card_pattern") else None
        fcy_re = re.compile(rowcfg["fcy_pattern"])
        max_gap = float(rowcfg.get("max_orphan_gap", 20.0))

        dated: list[tuple[geo.Line, dict[str, str]]] = []
        orphans: list[tuple[geo.Line, str]] = []
        cardholder: Optional[str] = None
        holder_at: list[tuple[float, str]] = []
        card_at: list[tuple[float, str]] = []

        for ln in block:
            if self._is_drop(ln.text):
                continue
            # Match the card heading on the whole line: it is printed on its own,
            # and which column band it lands in is not something to rely on.
            if card_re and (cm := card_re.match(ln.text.strip())):
                card_at.append((ln.top, cm.group(1)))
                continue
            c = ln.cells(bands)
            if date_re.match(c["date"]):
                dated.append((ln, c))
                continue
            desc = c["description"]
            if not desc or c["amount"]:
                continue
            if holder_re.match(desc):
                holder_at.append((ln.top, squash(desc)))
            else:
                orphans.append((ln, desc))

        # Wrapped description fragments attach to the nearest dated row, above or
        # below, because the dated line sits vertically centred in its own block.
        prefix: dict[int, list[str]] = {}
        suffix: dict[int, list[str]] = {}
        for ln, desc in orphans:
            best = min(
                range(len(dated)), key=lambda k: abs(dated[k][0].top - ln.top), default=None
            )
            if best is None:
                continue
            if abs(dated[best][0].top - ln.top) > max_gap:
                continue
            (prefix if ln.top < dated[best][0].top else suffix).setdefault(best, []).append(desc)

        out: list[Transaction] = []
        for k, (ln, c) in enumerate(dated):
            amount = parse_amount(c["amount"])
            if amount is None:
                continue
            for top, name in holder_at:
                if top < ln.top:
                    cardholder = name
            for top, number in card_at:
                if top < ln.top:
                    card = number

            fcy_cur = fcy_amt = None
            if fm := fcy_re.search(c.get("fcy", "")):
                fcy_cur, fcy_amt = fm.group(1), parse_amount(fm.group(2))

            desc = squash(
                " ".join(prefix.get(k, []) + [c["description"]] + suffix.get(k, []))
            )
            out.append(
                Transaction(
                    date=parse_date(date_re.match(c["date"]).group(1), rowcfg["date_formats"]),
                    time=parse_time(c["date"]),
                    description=desc,
                    amount=amount,
                    type=(
                        TxnType.CREDIT
                        if is_credit_marker(c["amount"])
                        or is_credit_amount_in_row(ln.text, amount)
                        else TxnType.DEBIT
                    ),
                    section=section,
                    category=squash(c.get("category", "")) or None,
                    cardholder=cardholder,
                    card_masked=card,
                    # HDFC prints "EMI" here for purchases that are merely
                    # eligible.  Actual conversions are established from the
                    # matching credit after the complete table is parsed.
                    is_emi=False,
                    reward_points=parse_int(c.get("rewards", "")),
                    fcy_currency=fcy_cur,
                    fcy_amount=fcy_amt,
                    page=ln.page,
                    raw=ln.text,
                )
            )
        return out, card

    # ------------------------------------------------------------- header

    def _coerce(self, raw: Optional[str], kind: str):
        if raw is None:
            return None
        if kind == "amount":
            return parse_amount(raw)
        if kind == "signed_amount":
            return parse_signed_amount(raw)
        if kind == "date":
            return parse_date(raw, self.date_formats)
        return squash(raw)

    def _extract_header(self, p1: list[geo.Line], all_lines: list[geo.Line]) -> dict:
        out: dict[str, Any] = {}
        for field, spec in self.t.get("header", {}).items():
            kind = spec.get("kind", "text")
            # A few fields (ICICI's billing period) are printed as prose on a
            # later page rather than in the page-1 panel.
            scope = all_lines if spec.get("any_page") else p1
            if "regex" in spec:
                rx = re.compile(spec["regex"])
                raw = next((m.group(1) for ln in scope if (m := rx.match(ln.text))), None)
            elif "left_of" in spec:
                raw = geo.value_left_of(scope, spec["left_of"])
            elif "right_of" in spec:
                raw = geo.value_right_of(scope, spec["right_of"])
            elif "text_below" in spec:
                raw = geo.text_below(scope, spec["text_below"], dy=spec.get("dy", 40.0))
            elif "below" in spec:
                raw = geo.value_below(scope, spec["below"], dy=spec.get("dy", 40.0))
            else:
                raw = None

            if kind == "period" and raw:
                sep = self.t.get("period_separator", r"\s+-\s+")
                parts = [geo.first_date_run(p) for p in re.split(sep, raw)]
                if len(parts) == 2 and all(parts):
                    out["period_start"] = parse_date(parts[0], self.date_formats)
                    out["period_end"] = parse_date(parts[1], self.date_formats)
                continue
            out[field] = self._coerce(raw, kind)
            if out[field] and spec.get("compact"):
                out[field] = re.sub(r"\s+", "", out[field])
        return out

    @property
    def _amount_predicate(self):
        # Axis prints summary figures with no currency mark at all, so requiring
        # one would find nothing; there, two decimal places is the signal.
        if self.t.get("summary_amount") == "decimal":
            return is_decimal_amount
        return is_currency_amount

    def _locate(self, p1: list[geo.Line], spec: dict, kind: str) -> Optional[str]:
        """Resolve one anchored value. Anchors are interchangeable across fields."""
        if "right_of" in spec:
            return geo.value_right_of(p1, spec["right_of"])
        if "left_of" in spec:
            return geo.value_left_of(p1, spec["left_of"])
        if "row_below" in spec:
            return geo.amounts_row_below(
                p1,
                spec["row_below"],
                spec.get("index", 0),
                self._amount_predicate,
                dy=spec.get("dy", 40.0),
            )
        if "text_below" in spec:
            return geo.text_below(p1, spec["text_below"], dy=spec.get("dy", 40.0))
        if "below" in spec:
            if kind == "date":
                return geo.date_below(p1, spec["below"], dy=spec.get("dy", 70.0))
            return geo.value_below(
                p1,
                spec["below"],
                dy=spec.get("dy", 70.0),
                dx=spec.get("dx", 60.0),
                predicate=self._amount_predicate,
            )
        return None

    def _extract_summary(self, p1: list[geo.Line]) -> Summary:
        vals: dict[str, Any] = {}
        for field, spec in self.t.get("summary", {}).items():
            kind = spec.get("kind", "amount")
            if "sum" in spec:
                # Issuers split one concept across several boxes — Axis bills
                # "Payments" and "Credits" separately, ICICI splits purchases
                # from cash advances. The canonical schema keeps one field, so
                # the parts are added here rather than blurred in the checks.
                parts = [self._coerce(self._locate(p1, s, "amount"), "amount") for s in spec["sum"]]
                found = [p for p in parts if p is not None]
                vals[field] = sum(found, Decimal("0")) if found else None
            else:
                vals[field] = self._coerce(self._locate(p1, spec, kind), kind)
        return Summary(**vals)

    # ---------------------------------------------------------------- run

    def parse(self, pdf_path: str | Path) -> Statement:
        pdf_path = Path(pdf_path)
        ytol = self.t.get("layout", {}).get("y_tolerance", 2.0)

        all_lines: list[geo.Line] = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                all_lines += geo.extract_lines(page, i, ytol, self.glyph_fixes)

        p1 = [ln for ln in all_lines if ln.page == 0]
        head = self._extract_header(p1, all_lines)

        # A card's statement date is the day its cycle closes — true on every
        # issuer in the corpus. Some documents render the label in a way the word
        # extractor cannot see, so fall back rather than lose the field; the
        # billing period it is derived from is itself validated.
        if not head.get("statement_date") and head.get("period_end"):
            head["statement_date"] = head["period_end"]

        # Legacy HDFC statements print the cycle-closing date but not a billing
        # period.  Their monthly cycle begins on the following day of the prior
        # month (15 Jul closes a 16 Jun -> 15 Jul cycle).
        if self.t.get("period_from_statement_date") and head.get("statement_date"):
            end = head["statement_date"]
            previous_month = end.month - 1 or 12
            previous_year = end.year - (1 if end.month == 1 else 0)
            days_in_previous_month = calendar.monthrange(previous_year, previous_month)[1]
            start_day = min(end.day + 1, days_in_previous_month)
            head["period_start"] = dt.date(previous_year, previous_month, start_day)
            head["period_end"] = end

        transactions = self._parse_tables(all_lines)

        return Statement(
            template_id=self.t["id"],
            issuer=self.t["issuer"],
            doc_type=DocType(self.t["doc_type"]),
            currency=self.t.get("currency", "INR"),
            product=head.get("product"),
            account_holder=head.get("account_holder"),
            account_masked=_primary_card(transactions) or head.get("account_masked"),
            statement_date=head.get("statement_date"),
            period_start=head.get("period_start"),
            period_end=head.get("period_end"),
            summary=self._extract_summary(p1),
            transactions=transactions,
            source_file=pdf_path.name,
        )


def _primary_card(transactions: list[Transaction]) -> Optional[str]:
    """Which card a statement grouped by card is *for*, or ``None`` if it is not.

    The header regex cannot answer this: on a combined statement it matches every
    card heading in the table and takes whichever is printed first, which is not
    reliably the card the summary belongs to. One ICICI statement opens with a
    zero-prefixed pseudo-card carrying a single cashback adjustment, and naming
    the statement after that invents a card that does not exist.

    The busiest group is the answer instead — the primary card is the one the
    statement is mostly about — with ties going to whichever was printed first,
    which is the order issuers list a relationship's cards in.
    """
    counts: dict[str, int] = {}
    for txn in transactions:
        if txn.card_masked:
            counts[txn.card_masked] = counts.get(txn.card_masked, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda card: counts[card])


def parse_pdf(
    pdf_path: str | Path,
    template_dir: Path = TEMPLATE_DIR,
    *,
    generic_fallback: bool = True,
) -> Statement:
    """Fingerprint, parse, validate.

    Falls back to template-free extraction when nothing matches, so an unseen
    issuer still produces rows. Fallback output is explicitly flagged: it has no
    issuer totals to reconcile against, so it cannot be trusted the way a
    template-matched parse can.
    """
    from .schema import Check
    from .validate import run_checks

    with pdfplumber.open(pdf_path) as pdf:
        page1 = pdf.pages[0].extract_text() or ""

    try:
        tmpl = match_template(page1, load_templates(template_dir))
    except NoTemplateMatch:
        if not generic_fallback:
            raise
        from .generic import parse_generic

        stmt = parse_generic(pdf_path)
        stmt.checks = [
            Check(
                name="template_matched",
                passed=False,
                severity="warning",
                detail="no template matched; parsed generically — totals are unverified",
            ),
            Check(
                name="found_transactions",
                passed=bool(stmt.transactions),
                severity="error",
                detail=f"{len(stmt.transactions)} rows recovered without a template",
            ),
        ]
        return stmt

    try:
        stmt = Engine(tmpl).parse(pdf_path)
    except TemplateError as exc:
        # The document fingerprinted as a known issuer but its table does not fit
        # the template. Falling back beats failing: the rows are still recoverable
        # and the flag tells the caller the totals were never reconciled.
        if not generic_fallback:
            raise
        from .generic import parse_generic

        stmt = parse_generic(pdf_path)
        stmt.checks = [
            Check(
                name="template_matched",
                passed=False,
                severity="warning",
                detail=f"{tmpl['id']} fingerprinted but did not fit ({exc}); parsed generically",
            ),
            Check(
                name="found_transactions",
                passed=bool(stmt.transactions),
                severity="error",
                detail=f"{len(stmt.transactions)} rows recovered without a template",
            ),
        ]
        return stmt
    stmt.checks = run_checks(stmt, tmpl.get("validate", []))
    return stmt
