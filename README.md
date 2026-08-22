# statement-parser-delta

Credit-card and bank-account statement PDFs → separate canonical ledgers → analysis
you can keep adding to. Every extraction is **proved by arithmetic** before it is stored.

Every statement in the regression corpus parses at 100% confidence, with every
reconciliation check passing to the paisa.

**`inbox/` is the source of truth for documents**, and holds each one exactly once:
every PDF as the mailbox delivered it, encrypted as the issuer sent it, named
`YYYYMM_<mailbox>_<issuer's own attachment name>.pdf`. It is organised by instrument,
one folder per card and per account:

```
inbox/
  cards/
    hdfc-bank-regalia-1111/       one folder per card, named for the card
    icici-bank-2222/
    _unsorted/                    downloaded or uploaded, not parsed yet
  bank/
    hdfc-bank-savings-3333/       one folder per account
    indusind-bank-indus-classic-4444/
    _unsorted/
```

Which card or account a statement belongs to is only known once it has been parsed,
so every PDF lands in `_unsorted` and is moved into place the moment the parser
identifies it. A file that never parsed stays visibly unsorted rather than being
filed under a guess. `sparser/inbox.py` is the only module that decides any of this.

**`samples/` is a staging area, not a second archive**, and is normally empty. Drop
a PDF there while designing a parser for a statement the inbox does not have yet;
once it parses it belongs in the inbox like everything else.

The regression corpus therefore reads from the inbox, decrypting on the fly with
the passwords already in your database (`tests/corpus.py`), so no statement is
stored in two places. The two ledgers earn corpus membership differently, because
their tests differ. A card test freezes the exact parse against a golden file, so
that set is deliberate: `python scripts/make_golden.py <name>` is how a statement
joins. A bank test only asserts that the arithmetic reconciles, so every statement
filed under an account is in and next month's joins automatically. On a clean
checkout with no inbox and no database, the corpus tests simply skip.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
cd webapp && npm install && npm run build && cd ..     # build the UI once

.venv/bin/python -m sparser import samples --db data/statements.db
.venv/bin/python -m sparser serve  --db data/statements.db   # → http://127.0.0.1:8770
```

## Running it

There are **two processes in development, one in production.**

| | Backend (FastAPI) | Frontend |
|---|---|---|
| **Production** | `python -m sparser serve` on **:8770** | served by FastAPI from `sparser/web/dist` |
| **Development** | `python -m sparser serve --reload` on **:8770** | `npm run dev` on **:5173** |

`npm run dev` starts **only** Vite. It proxies `/api` to :8770, so every API call
404s until the Python side is running too. `./scripts/dev.sh` starts both and stops
both together — open **:5173** in dev (hot reload), **:8770** in production.

API docs are always at `/docs`.

## The screens

**Card Analysis** — multi-select card filter, date range with 1M/4M/6M/9M/1Y/All
shortcuts, six stat tiles, monthly spend-vs-payments, spend by category, by card,
top merchants, and the full transaction table (sortable, searchable). Reward
points, EMIs and foreign-currency legs each get their own section — they are
separate ledgers, and folding points into a rupee total would be nonsense.

**Cards** — one row per card, discovered from the statements themselves. Shows how
many statements and transactions each has, lets you store that card's PDF
password, and — in its own panel — the **sender and subject lists a scan searches for
unrecognized cards**. Those lists are the filter behind the *Unrecognized cards* option:
they apply to every card no import has introduced yet, so when a scan comes back empty
they are the first thing to widen. Editable, with the shipped lists one click away. Passwords are tried in order: supplied → stored → derived from issuer
conventions. When a derived one works it is **saved against that card
automatically** and marked `learned`, so next month it opens on the first attempt.
Values are encrypted and only sent to the page when you click *show*.

**Card Pipeline** — scan-then-approve. Pick *this month*, a **specific month**, the last
12 months, or a folder on disk. A scan downloads and fully parses each PDF but
**stores nothing**; you get a review list showing card, period, transaction count,
confidence, and whether it is already in the database — then tick what to import,
or discard. Every PDF's journey is recorded step by step:

```
202501_mailbox_Credit_Card_Statement.pdf
  Example Bank Card   hdfc_cc_v1   42 txns   100% confidence
  1. download     skipped  already on disk
  2. decrypt      skipped  not password protected
  3. classify     ok       3 pages, 7,168 chars — digital text layer
  4. fingerprint  ok       matched hdfc_cc_v1
  5. extract      ok       42 transactions, period 2024-12-16 → 2025-01-15
  6. validate     ok       5/5 passed — computed 12345.67 vs stated 12345.00 …
  7. store        ok       inserted statement, 42 rows
```

Runs are kept, so "why is this statement missing?" is answerable months later.

**Bank Analysis** — account and date filters, cash-flow totals, opening/closing
balances, monthly withdrawals vs deposits, debit categories, counterparties, and
the complete bank transaction table with reference, value date and running balance.
Click a category to set a manual label (including `Tax`) or restore the automatic
label; overrides stay separate from parser-derived enrichment.

**Bank Accounts** — one expandable row per account. The full account number is not
copied into account records: identity uses a one-way fingerprint and display uses
the last four digits. Expanded rows show every imported statement's declared period,
transaction coverage, totals, balances and source file — plus that account's mailbox
scan rules (sender and subject phrases that narrow a bank scan) and its statement
password, stored encrypted and, like a card's, saved automatically and marked `learned`
when a derived one turns out to work. The same **unrecognized accounts** search panel as
the Cards tab sits below the list, holding the sender and subject lists a bank scan falls
back to.

**Bank Pipeline** — scan-then-approve, the same three sources as cards: **a mailbox
connection**, an **upload**, or a **folder on disk**. Digital-text account statements from
**HDFC, ICICI, IndusInd and IDFC FIRST** are classified, reconstructed from PDF geometry,
checked against every adjacent running balance, then held for review with confidence,
checks, period, row count and duplicate status. Only selected statements are written to
the bank ledger. Its run history is separate from card runs.

A mail scan takes the same windows as a card scan (this month, a specific month, a
range, the last 12), filters by **account** — including an *unrecognized accounts* option
for accounts no statement has introduced yet — and by **connection**. It searches only
mailboxes ticked for bank statements, and it stops at the review queue: nothing a sweep
downloads reaches the ledger without approval.

**HDFC mails a link, not a file.** Its smart statement is a password gate, so the sweep
follows it: read the form, fetch the one-shot token, post the scrambled password, then
pull the PDF — one cookie jar across the whole exchange, because the gate rejects every
call that does not return the session it opened. The password is the same one the PDF
would have wanted, so a stored account password is tried first and the name/DOB
convention only after; attempts are capped, since these are real logins against a bank.

**Nothing ever duplicates.** A card statement is keyed by card and billing cycle;
a bank statement by account fingerprint and statement period. Re-fetching a cycle
*replaces* that statement. The review list tells you this before you commit —
verified by re-importing the whole corpus and watching every count stay put.

**Connections** — add each Gmail account with an app password, test it, see status
and last sync. Two checkboxes per mailbox say what it actually receives — **card
statements**, **bank statements**, or both. Each pipeline sweeps only the mailboxes ticked
for it, so an account that never gets card mail is not searched on every card scan;
searching is the slow part of a sweep. Untick both to pause a mailbox without
disconnecting it. App passwords are encrypted before touching the database; the key
lives at `~/.config/sparser/secret.key` (0600, outside the project), so the
database alone leaks nothing usable.

## Why the extraction is trustworthy

**Geometry, never whitespace.** Text is never split on spaces. Cells are assigned by
x-overlap against column bands measured from *each table block's own header row*.
This is not a preference — the same HDFC statement prints its table at several
x-origins (the page-1 block is inset ~144pt from page 2) and labels the same column
differently per product (`REWARDS` on Regalia, `Base NeuCoins*` on Tata Neu, absent
on Swiggy). Fixed coordinates cannot survive that.

**Arithmetic validation.** Rows are reconciled against figures the issuer printed
independently of them:

```
cc_summary_reconcile             previous − payments + purchases + charges == total due
debits_match_purchases           Σ debit rows                             == stated purchases
credits_match_payments           Σ credit rows                            == stated payments
transactions_reconcile_to_total  previous + Σ(every parsed row) + charges  == total due
transactions_within_period       every date inside the billing cycle
```

The fourth proves the row list is *complete* — it ties parsed rows to two numbers
printed nowhere near them. A statement failing these is never stored (`--force`
overrides, deliberately).

## Real-world details handled

- **Broken ToUnicode CMaps** — ₹ decodes as `C` on HDFC, a backtick on ICICI. The
  glyph is its own word, so repair happens after joining, never per word.
- **Descriptions wrapping above *and* below their dated line** — the amount is
  centred against a 3-line description; orphans attach to the nearest dated row.
- **Credit balances** — `6,627.00 CR` means you are owed money. Balance fields are
  signed; as magnitudes every downstream sum is off by twice the balance.
- **Split summary concepts** — Axis bills "Payments" and "Credits" separately, ICICI
  splits purchases from cash advances; templates sum the parts into one field.
- **Equation-row summaries** — Axis prints labels on one line and figures on the
  next, compressed differently, so they are matched by *order*, not x-position.
- **Optional columns** — `"rewards?"` plus `||` band fallbacks let one template cover
  a card family whose members print different columns.
- **Settlement lag** — a card bills on posting date but prints transaction date.
- **Hard-wrapped narrations** — IndusInd cuts a UPI string at the column edge,
  mid-token; joining those lines with a space invents a handle nobody typed. A line
  that stops short of the edge ended on its own, and only that break was a space.
- **Permanently masked account numbers** — IndusInd never prints more than
  `50XXXXXXX123`. Stripping the mask out would splice the leading digits onto the
  trailing ones and show an ending the customer has never seen, so only the visible
  tail is used.
- **Consolidated statements** — one IDFC FIRST PDF can carry several accounts; rows
  are read only from the section belonging to the account being imported.
- **Remarks that start above their own date** — ICICI sizes each row to its remark and
  prints the dated line five points below the band's top, so proximity to a date hands
  the first line to the row above. Its ruled row boxes settle it instead.
- **A balance of exactly 0.00** — a real balance on an account emptied to the paisa, so
  a row is judged on whether the figure was *printed*, never on its value.
- **Marketing copy stealing anchors** — label matching is case-sensitive, or "For
  hassle free *payments*…" hijacks the `Payments` anchor.

## Architecture

```
sparser/
├── decrypt.py    pikepdf; issuer password-pattern derivation
├── mailbox.py    Gmail over IMAP, read-only, multi-account; one sweep, two products
├── accounts.py   connected mailboxes, secrets encrypted at rest
├── geometry.py   words → lines → column cells; label/value anchoring
├── normalize.py  glyph repair, amounts, dates, Dr/Cr direction
├── engine.py     YAML template interpreter
├── generic.py    template-free fallback for unseen issuers
├── validate.py   the reconciliation checks
├── enrich.py     merchant names and categories
├── pipeline.py   ingest orchestration + recorded audit trail
├── store.py      SQLite; money as integer paise
├── banks/        one module per bank + shared bank models and geometry
│   ├── base.py       canonical bank row, the checks, ruled-table primitives
│   ├── hdfc.py       columns measured from the header row (no rules drawn)
│   ├── indusind.py   ruled columns; masked account number; hard-wrapped narrations
│   ├── idfc.py       fully ruled table; consolidated multi-account statements
│   └── icici.py      fully ruled table; remarks start above their own dated line
├── smartstatement.py HDFC's linked statement: its password gate, its two ciphers
├── bank_pipeline.py bank ingest — upload, disk and mail — separate from card templates
├── bank_store.py    bank accounts/statements/transactions + cash-flow analytics
├── api.py        FastAPI
└── templates/    one YAML per issuer — hdfc, icici, axis, yes_bank
webapp/           React + TypeScript + Vite
```

**Money is stored as integer paise, never float.** A REAL column turns ₹1,234.05
+ ₹2,345.10 into 3579.1499999999996; summed over a few thousand rows that drifts off the very
reconciliations the parser works to prove.

**Imports are idempotent.** Statements are keyed by their account/card identity and
declared period, so re-running the same input replaces rather than doubles it.

Card and bank configuration are deliberately independent. Bank rows live only in
`bank_*` tables and `/api/bank/*` endpoints; card templates, parsing and analytics
never query those tables. They share only neutral infrastructure such as exact-paise
conversion, SQLite connection setup and ingest audit rows.

### Adding an issuer

Write a YAML file — no Python. Declare a fingerprint, section markers, header roles
left-to-right, band expressions relative to those headers, row rules, and summary
anchors:

```yaml
header_clusters: [date, description, "rewards?", amount, pi]
bands:
  description: ["description.x0 - 4", "rewards.x0 - 6 || amount.x0 - 30"]
  amount:      ["rewards.x1 + 8 || amount.x0 - 30", "pi.x0 - 4"]
```

### Adding a bank

Deposit accounts do **not** go through the YAML engine, and that is deliberate: a
card statement is a summary table the issuer reconciles for you, while a bank
statement is a running ledger whose only proof is that every row moves the printed
balance exactly as stated. So each bank gets a module in `sparser/banks/`, exporting
one `parse(path)` that recognises its own statements and raises
`UnsupportedBankStatement` for everything else — then it is added to `BANK_PARSERS`,
which tries each parser in turn. Nothing else in the app changes; the models, the
five checks and the ruled-table geometry come from `banks/base.py`.

The layouts differ more than card templates do. HDFC draws no column rules, so its
columns are measured from the table's header. IndusInd, IDFC FIRST and ICICI draw
them, so their cells are cut on the bank's own lines — exact where a midpoint between
two headings is not, since right-aligned amounts drift left as they gain digits. Where
a bank rules its rows too, a row's cell *is* the transaction; columns are named by the
header word printed inside them, so a layout can add a serial-number or cheque column
without renumbering everything after it.

### The fallback ladder

```
template match → template-free extraction → flagged as unverified
```

`generic.py` finds tables with no template at all: runs of date+amount lines,
columns recovered by whitespace projection, named by content. It even auto-detects
broken currency glyphs. On this corpus it independently reproduces the templated
result — identical counts and totals — on all but one statement, told nothing about any
issuer. Its output is always flagged, since it has no issuer totals to reconcile.

## CLI

```bash
python -m sparser parse  stmt.pdf -o out.xlsx        # xlsx / csv / json
python -m sparser import samples --db data/statements.db  # into the store
python -m sparser fetch  --months 1                  # Gmail → parse → store (cards)
python -m sparser bank-fetch --months 1              # Gmail → parse → review (accounts)
python -m sparser serve  --db data/statements.db          # dashboard + API
```

Encrypted statements: `--password SECRET`, or derive them with
`--name "<your name>" --dob DD/MM/YYYY --card-last4 1234` (tries the documented issuer
conventions: `first4name+DDMM`, `FIRST4+DDMMYYYY`, `DDMMYYYY`, …).

## Ask an LLM with MCP

The project includes a curated MCP server and a `statement-analytics` Agent Skill
for questions that do not fit a fixed dashboard: card period comparisons,
merchant/category trends, bank cash flow, account/statement coverage,
counterparties, unusual debits, and parser-health audits. Card and bank tools and
IDs stay separate. The server exposes no arbitrary SQL, passwords, or PDF paths;
its only write is an explicit category override for one bank transaction.

Install the project once, using absolute paths in client configuration:

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

LM Studio (Program → Install → Edit `mcp.json`) and Claude Desktop/Claude Code
accept this stdio server shape:

```json
{
  "mcpServers": {
    "statement-analytics": {
      "command": "/ABSOLUTE/PATH/statement-parser-delta/.venv/bin/python",
      "args": ["-m", "sparser.mcp_server"],
      "env": {
        "SPARSER_DB": "/ABSOLUTE/PATH/statement-parser-delta/data/statements.db"
      }
    }
  }
}
```

For Codex, add the equivalent to `~/.codex/config.toml`:

```toml
[mcp_servers.statement-analytics]
command = "/ABSOLUTE/PATH/statement-parser-delta/.venv/bin/python"
args = ["-m", "sparser.mcp_server"]

[mcp_servers.statement-analytics.env]
SPARSER_DB = "/ABSOLUTE/PATH/statement-parser-delta/data/statements.db"
```

The repository's [.mcp.json](.mcp.json) is ready for clients launched from the
project root. The portable skill lives at
[`skills/statement-analytics/`](skills/statement-analytics/); clients supporting
Agent Skills can load that folder directly or copy it into their skill directory.
Restart the MCP client after changing its configuration.

## Tests

```bash
.venv/bin/python -m pytest -q      # all green
python scripts/make_golden.py      # refresh golden corpus after intended changes
```

Unit tests always run and need nothing but the repo — a clean checkout with no
inbox and no database passes in under two seconds. The corpus tests parse real
statements out of your inbox, assert every arithmetic check reconciles, and diff
the card parses against frozen JSON; with no statements to read they skip, so a
clean checkout stays green. No statement, golden file or database is ever
committed — see `.gitignore`.

## Known limitations

- **Scanned statements are not supported** — everything assumes a text layer. The
  pipeline detects and reports them; OCR is designed for, not built.
- **Bank-account support currently covers HDFC digital-text statements only.** Bank
  parsing is isolated so another bank can be added without changing card templates.
- **Generic mode picks up auxiliary tables** (an EMI schedule is structurally
  identical to a transaction list) and under-recovers multi-page tables lacking a
  repeated header. Template mode is unaffected.
- **The server is loopback-only and unauthenticated.** It serves unredacted
  financial history — do not expose it without auth in front.
