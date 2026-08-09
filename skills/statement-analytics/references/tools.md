# Tool reference

## Core filters

Dates use `YYYY-MM-DD`. `card_ids` come from `list_cards`. Omitted card/date filters mean all imported history. Amount filters and outputs are INR rupees unless `fcy_currency` is present.

## Tool selection

- `list_cards()` — coverage and valid card IDs.
- `get_overview(card_ids?, date_from?, date_to?)` — spend, payments, net, largest transaction, and monthly totals.
- `search_transactions(query?, card_ids?, date_from?, date_to?, direction?, ledger?, category?, minimum_amount?, maximum_amount?, limit?, offset?)` — row evidence. `ledger` can isolate `emi`, `rewards`, or `foreign_currency`. Maximum 500 rows per call.
- `special_ledger(ledger, card_ids?, date_from?, date_to?)` — monthly totals for `rewards`, `emi`, or `foreign_currency` without asking the model to sum rows.
- `spending_breakdown(group_by, card_ids?, date_from?, date_to?, limit?)` — debit groups. `group_by` is `month`, `category`, `merchant`, or `card`.
- `compare_periods(first_from, first_to, second_from, second_to, card_ids?)` — spend and category deltas between explicit ranges.
- `find_recurring_merchants(card_ids?, date_from?, date_to?, minimum_months?, limit?)` — merchants active in multiple distinct months. Repetition is not proof of a subscription.
- `find_unusual_transactions(card_ids?, date_from?, date_to?, limit?)` — robust median-absolute-deviation outliers among debits.
- `statement_health(card_ids?)` — confidence, analyzer versions, and failed reconciliation checks.

## Useful analysis patterns

### “Where did my spending increase?”

Call `compare_periods`, inspect the largest absolute category changes, then call `spending_breakdown(group_by="merchant")` for the period that increased.

### “What subscriptions do I have?”

Call `find_recurring_merchants`, then verify plausible candidates with `search_transactions(query=...)`. Say “recurring merchant” unless cadence and description strongly support subscription language.

### “What looks abnormal?”

Call `find_unusual_transactions`, then inspect the surrounding period with `search_transactions`. Explain the MAD method and sample size; never label an outlier fraudulent.

### “Can I trust these totals?”

Call `statement_health`. Distinguish parser reconciliation from merchant/category enrichment quality.
