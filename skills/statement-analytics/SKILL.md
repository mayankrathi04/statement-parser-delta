---
name: statement-analytics
description: Analyze imported credit-card statement history through the read-only Statement Analytics MCP server. Use for spending trends, merchant/category/card comparisons, recurring charges, unusual transactions, period-over-period changes, reward/EMI/foreign-currency investigation, statement quality checks, and financial questions that need evidence beyond the dashboard UI.
---

# Statement Analytics

Use the `statement-analytics` MCP tools to answer questions from the canonical statement database. Keep conclusions numerical, scoped, and traceable to returned evidence.

## Workflow

1. Call `list_cards` to learn available card IDs and date coverage unless the request already supplies both.
2. Translate relative periods such as “last three months” into explicit ISO dates based on database coverage, not the wall-clock date.
3. Choose the narrowest tool:
   - `get_overview` for totals and monthly movement.
   - `spending_breakdown` for category, merchant, card, or monthly composition.
   - `search_transactions` for named merchants, categories, amount bands, EMIs, rewards, or foreign-currency evidence.
   - `special_ledger` for monthly reward, EMI, or foreign-currency totals.
   - `compare_periods` for before/after questions.
   - `find_recurring_merchants` for repeated activity across months.
   - `find_unusual_transactions` for statistically large debits.
   - `statement_health` for parser confidence and reconciliation quality.
4. Narrow broad results with card IDs and dates. Paginate transaction searches with `limit` and `offset`; do not request all rows without a reason.
5. Cross-check surprising claims with a second tool when practical.
6. Report the date range, cards, totals, transaction counts, and method used.

## Interpretation rules

- Treat debit as spend and credit as payment/refund. Do not combine them without naming the result “net movement.”
- Treat INR, original foreign-currency values, and reward points as separate units.
- Treat `confidence=1` as arithmetic reconciliation, not proof that a merchant categorization is perfect.
- Describe unusual transactions as statistical outliers, not fraud.
- Do not infer intent, necessity, identity, or financial advice from merchant names alone.
- State gaps or sparse samples explicitly.

Read [references/tools.md](references/tools.md) when selecting parameters or composing multi-tool analysis.
