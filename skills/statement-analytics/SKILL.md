---
name: statement-analytics
description: Analyze imported credit-card and bank-account statement history through the Statement Analytics MCP server. Use for card spending, bank cash flow, merchant/counterparty and category comparisons, recurring charges, unusual transactions, period changes, reward/EMI/foreign-currency investigation, statement quality checks, account coverage, or explicit correction of one bank transaction category.
---

# Statement Analytics

Use the `statement-analytics` MCP tools to answer questions from the canonical statement database. Keep card and bank ledgers separate. Keep conclusions numerical, scoped, and traceable to returned evidence.

## Workflow

1. Identify the ledger from the request. Call `list_cards` for card questions or `list_bank_accounts` for bank questions unless the request already supplies the required IDs and coverage.
2. Translate relative periods such as “last three months” into explicit ISO dates based on database coverage, not the wall-clock date.
3. Choose the narrowest card tool:
   - `get_overview` for totals and monthly movement.
   - `spending_breakdown` for category, merchant, card, or monthly composition.
   - `search_transactions` for named merchants, categories, amount bands, EMIs, rewards, or foreign-currency evidence.
   - `special_ledger` for monthly reward, EMI, or foreign-currency totals.
   - `compare_periods` for before/after questions.
   - `find_recurring_merchants` for repeated activity across months.
   - `find_unusual_transactions` for statistically large debits.
   - `statement_health` for parser confidence and reconciliation quality.
4. Choose the narrowest bank tool:
   - `get_bank_overview` for withdrawals, deposits, net cash flow, balances, and monthly movement.
   - `search_bank_transactions` for narration, reference, counterparty, direction, category, date, or amount evidence.
   - `bank_cashflow_breakdown` for withdrawals or deposits grouped by month, effective category, counterparty, or account.
   - `bank_statement_health` for bank parser confidence and reconciliation checks.
   - `bank_pipeline_status` for recent bank import runs and statements awaiting dashboard approval.
   - `set_bank_transaction_category` only for an explicit user-requested correction to a known bank transaction ID. Pass null or blank only when explicitly asked to restore automatic categorization.
5. Narrow broad results with matching ledger IDs and dates. Paginate transaction searches with `limit` and `offset`; do not request all rows without a reason.
6. Cross-check surprising claims with a second tool when practical.
7. Report the ledger, date range, selected cards/accounts, totals, transaction counts, and method used.

## Interpretation rules

- Treat debit as spend and credit as payment/refund. Do not combine them without naming the result “net movement.”
- For bank rows, treat debit as withdrawal and credit as deposit. Name deposits minus withdrawals “net cash flow.” Do not apply credit-card spend semantics to bank rows.
- Treat INR, original foreign-currency values, and reward points as separate units.
- Treat `confidence=1` as arithmetic reconciliation, not proof that a merchant categorization is perfect.
- Treat bank categories as narration-derived unless `category_is_override` is true. Use the effective `category` for analysis and retain `derived_category` when explaining an override.
- Never change a bank category based on inference alone. Require an explicit correction request and a specific transaction. Report the old/effective category and the saved category after the tool returns.
- Treat `bank_pipeline_status` as inspection only. Direct the user to Bank Pipeline for upload, re-evaluation, approval, or discard actions.
- Describe unusual transactions as statistical outliers, not fraud.
- Do not infer intent, necessity, identity, or financial advice from merchant names alone.
- State gaps or sparse samples explicitly.

Read [references/tools.md](references/tools.md) when selecting parameters or composing multi-tool analysis.
