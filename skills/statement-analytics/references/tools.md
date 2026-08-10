# Tool reference

## Core filters

Dates use `YYYY-MM-DD`. `card_ids` come from `list_cards`; `account_ids` come from `list_bank_accounts`. Omitted ID/date filters mean all history in that tool's ledger. Amount filters and outputs are INR rupees unless `fcy_currency` is present. Never pass bank account IDs to card tools or card IDs to bank tools.

## Card tool selection

- `list_cards()` — coverage and valid card IDs.
- `get_overview(card_ids?, date_from?, date_to?)` — spend, payments, net, largest transaction, and monthly totals.
- `search_transactions(query?, card_ids?, date_from?, date_to?, direction?, ledger?, category?, minimum_amount?, maximum_amount?, limit?, offset?)` — row evidence. `ledger` can isolate `emi`, `rewards`, or `foreign_currency`. Maximum 500 rows per call.
- `special_ledger(ledger, card_ids?, date_from?, date_to?)` — monthly totals for `rewards`, `emi`, or `foreign_currency` without asking the model to sum rows.
- `spending_breakdown(group_by, card_ids?, date_from?, date_to?, limit?)` — debit groups. `group_by` is `month`, `category`, `merchant`, or `card`.
- `compare_periods(first_from, first_to, second_from, second_to, card_ids?)` — spend and category deltas between explicit ranges.
- `find_recurring_merchants(card_ids?, date_from?, date_to?, minimum_months?, limit?)` — merchants active in multiple distinct months. Repetition is not proof of a subscription.
- `find_unusual_transactions(card_ids?, date_from?, date_to?, limit?)` — robust median-absolute-deviation outliers among debits.
- `statement_health(card_ids?)` — confidence, analyzer versions, and failed reconciliation checks.

## Bank tool selection

- `list_bank_accounts()` — valid account IDs, transaction coverage, and every imported statement/coverage period. Identity fingerprints are not exposed.
- `get_bank_overview(account_ids?, date_from?, date_to?)` — withdrawals, deposits, net cash flow, opening/closing balances, largest withdrawal, and monthly totals.
- `search_bank_transactions(query?, account_ids?, date_from?, date_to?, direction?, category?, minimum_amount?, maximum_amount?, limit?, offset?)` — row evidence including narration, reference, counterparty, effective/derived category, override state, amount, and post-transaction balance. Maximum 500 rows per call.
- `bank_cashflow_breakdown(group_by, direction, account_ids?, date_from?, date_to?, limit?)` — withdrawals (`debit`) or deposits (`credit`) grouped by `month`, `category`, `counterparty`, or `account`.
- `bank_statement_health(account_ids?)` — parser confidence, parser IDs, statement and transaction coverage, and failed reconciliation checks.
- `bank_pipeline_status(limit?)` — recent bank-only import runs and sanitized pending-review details. It does not expose file paths and cannot upload, approve, discard, or re-evaluate; use the Bank Pipeline page for those actions.
- `set_bank_transaction_category(transaction_id, category?)` — the only write tool. Set one manual category override, or pass null/blank to restore its parser-derived category. Use only after the user explicitly requests that exact correction.

Bank `category` is always the effective category. `derived_category` remains the parser result, `category_override` is the nullable manual value, and `category_is_override` states which one won. Category changes affect bank category aggregates but never amounts, balances, parser output, or the card ledger.

## Useful analysis patterns

### “Where did my spending increase?”

Call `compare_periods`, inspect the largest absolute category changes, then call `spending_breakdown(group_by="merchant")` for the period that increased.

### “What subscriptions do I have?”

Call `find_recurring_merchants`, then verify plausible candidates with `search_transactions(query=...)`. Say “recurring merchant” unless cadence and description strongly support subscription language.

### “What looks abnormal?”

Call `find_unusual_transactions`, then inspect the surrounding period with `search_transactions`. Explain the MAD method and sample size; never label an outlier fraudulent.

### “Can I trust these totals?”

Call `statement_health`. Distinguish parser reconciliation from merchant/category enrichment quality.

### “Where did my bank money go?”

Call `get_bank_overview`, then `bank_cashflow_breakdown(group_by="category", direction="debit")`. Drill into a large category with `search_bank_transactions(category=...)`.

### “What money came in?”

Call `bank_cashflow_breakdown(group_by="category", direction="credit")`. Use `search_bank_transactions(direction="credit", category=...)` for narration-level evidence.

### “Change this transaction to Medical”

Resolve the intended bank transaction with `search_bank_transactions` if its ID is not supplied. If multiple rows match, do not guess. Once the request identifies one row and explicitly asks for the change, call `set_bank_transaction_category(transaction_id=..., category="Medical")`, then report the returned effective and derived categories. Use `category=null` only for an explicit request to restore automatic categorization.
