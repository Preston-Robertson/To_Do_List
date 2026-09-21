# Finance

Finance has a recorded-finances overview, a records workspace, and local
cash-flow, wealth, and housing scenarios. The implementation is inspired by the
local `Finance_tester` reference source, not by its account data. It does not
import that application's records, connect to banks, or reuse its tax assumptions.

## Overview and records

`/finance` shows current balances alongside a selected calendar month's recorded
inflows, outflows, and net flow. Prior-month comparisons and a 12-month flow chart
use recorded transactions, not projected income or spending. Balances are current,
not reconstructed month-end balances; inflows and outflows are signed ledger
totals, not automatically classified salary and consumption.

The overview includes liquid cash, credit liabilities, investment cash, holdings,
net worth, saved net-worth snapshots, holdings allocation by account and symbol,
category spending, and budget remaining/overspend. Holdings older than seven days
are marked stale. Snapshot history is not interpolated; legacy snapshots lack
currency provenance. Net worth adds active base-currency account balances and
holdings, so investment account balances should represent cash, not repeat the
same holdings' value.

Upcoming recurring items cover today through the next 29 days, independently of
the selected ledger month. Overdue anchors are identified separately; viewing
this schedule neither records payments nor advances due dates.

Ledger filters and pagination are submitted by POST, keeping month, account,
category, search, and inflow/outflow/zero selection out of URLs. Search matches
account aliases and categories; memo search is opt-in. The default page size is
50, capped at 200. Filtered totals cover all matching rows, not just the visible
page; headline monthly totals remain unfiltered.

`/finance/records` retains account, transaction, budget, holding, and recurring-item
CRUD, reports, alerts, audit history, CSV preview/import/export, and JSON
backup/restore. Planning does not replace these recorded-data workflows.

## Cash flow and wealth

`/finance/planning` calculates scenarios without writing transactions, changing
account balances, or advancing recurring schedules. Opening cash is initially
seeded from active base-currency checking, savings, and cash accounts; opening
investments come from holdings on active base-currency accounts. These are
editable scenario inputs, not a complete reconstruction of every asset and
liability.

Choose exactly one take-home income source:

- **Recurring:** positive active recurring items supply net income.
- **Fixed:** an explicit monthly net-income amount replaces positive recurring
  income; it is not added to it.
- **Streams:** active dated income streams replace both other income sources.
  Each stream has an alias, net amount per occurrence, cadence, start date,
  optional inclusive end date, annual growth, active flag, and base currency.
  Streams have no account link and never create ledger entries.

Negative recurring items supply bills in every income mode. Variable spending is
additional to those bills; do not enter the same expense in both. Budgets and
actual transactions are not automatically added as forecast expenses. Inputs are
net pay: there is no gross-to-net calculator or Massachusetts/state-tax model
carried over from the reference.

Recurring schedules linked to inactive or other-currency accounts are excluded
from projections. Historical transactions on inactive accounts remain visible in
recorded monthly flows; deactivating an account does not erase its history.

Cash flow covers 1-60 complete calendar months, defaulting to 12 starting next
month. Wealth uses the same monthly calculation for 1-50 years and includes a
year-zero opening position. Weekly, monthly, quarterly, and yearly schedules
retain their original date anchor; streams also support biweekly pay. Short
months clamp the original day without permanently moving the anchor. Stream
start/end windows constrain occurrences, and inactive entries are excluded.

General income and expense growth step every 12 projected months. Streams instead
apply their own growth on start-date anniversaries. Investment return and inflation
use effective monthly factors. Return assumptions default to zero; cash earns no
interest. Decimal calculations round half up to integer minor units.

Monthly contributions move cash into investments at month end. They are transfers,
not consumption or new income: surplus is income minus bills and variable
spending, before contributions. Contributions and the cash reserve stay fixed.
Negative cash is flagged as a funding shortfall, not silently financed; borrowing
costs are not modeled. Reserve/shortfall checks use opening and month-end cash as
applicable, not intramonth payment timing.

Nominal and inflation-adjusted wealth plots describe independent assumptions,
not probability bands. Inflation-adjusted values use opening-month purchasing
power. These are scenario tools, not financial advice or guaranteed forecasts.

## Housing comparison

Housing inputs are monthly rent and annual rent growth, purchase price, down
payment, fixed mortgage APR and term, annual property-tax and maintenance rates,
annual insurance, monthly HOA, closing costs, appreciation, and comparison years.
The fixed payment covers principal and interest only; tax, insurance, maintenance,
and HOA are additional. Zero-interest and fully paid purchases are supported.

Annual rows separate principal, interest, remaining mortgage, home value, equity,
rent, ownership cash outflow, and ownership consumption. Principal builds equity
rather than counting as consumption. Total ownership cash out includes the initial
down payment and closing costs. Tax and maintenance use beginning-of-year scenario
home value; insurance and HOA stay fixed. Unspecified costs are zero assumptions.

This is a cash-cost and equity comparison, not a complete rent-versus-buy financial
verdict. Equity is not sale proceeds. PMI, tax deductions, selling costs/taxes,
sale proceeds, and opportunity returns on a renter's portfolio are not modeled.
`rental_portfolio_modeled` is false; the optional `investment_return_bps` assumption
is disclosed but does not affect housing costs. Appreciation is not guaranteed.

## Saved plans and limits

Save cash-flow or housing assumptions with a short alias; wealth reuses a
cash-flow plan. Compare two or three saved plans of the same kind. Cash-flow
comparisons require matching start months and horizons; housing horizons must
match. Plans store assumptions, not immutable copies of the current recurring
items or income streams: recalculation uses those schedules' current active state.

The database allows 30 plans total and 100 income streams, including inactive
streams. Updates and deletes require the loaded version; stale or missing records
return a conflict and must be reloaded. Writes are verified transactionally before
success. Saving assumptions or streams never posts actual income, contributions,
or expenses to the ledger.

Planning requests and saved configurations use these bounds:

| Input | Accepted values |
|---|---|
| Money | Integer minor units, absolute maximum `100000000000000`; nonnegative except opening cash |
| Home price / stream payment | Positive; down payment cannot exceed home price |
| Cash-flow horizon / start | 1-60 months; `YYYY-MM`, default next month |
| Wealth / housing / mortgage term | 1-50 years; mortgage term defaults to 30 |
| Income, expense, stream, and rent growth; mortgage APR | 0-3000 basis points (0-30%) |
| Investment return / appreciation | -5000 to 3000 basis points (-50% to 30%); default zero |
| Inflation; housing property-tax / maintenance rates | 0-2000 basis points (0-20%) |
| Plan / stream alias | 1-60 characters, subject to identifier/PII screening |

One basis point is 0.01 percentage point. No floating-point money or rates are
persisted. Planning JSON is limited to 32 KiB and canonical integer numeric fields;
unknown fields and duplicate keys are rejected. Overview filter bodies are limited
to 8 KiB. These bounds are validation limits, not recommended assumptions.

## Storage and privacy

`LUIGI_WEB_FINANCE_DB` selects the isolated app-owned SQLite database.
`LUIGI_WEB_FINANCE_BASE_CURRENCY` is the existing ISO currency setting; accounts,
plans, and streams use it without exchange-rate conversion. Current overview
totals exclude inactive or other-currency accounts; recorded monthly flows retain
historical transactions on inactive base-currency accounts. Other-currency data is
marked excluded, not converted.

`finance_plans` and `finance_income_streams` live beside the existing Finance
tables, never in LuigiBot or another module's database. Both are included in
`luigi-finance-backup-v1`. Older v1 backups may omit them. Restore validates rows,
currencies, configuration versions, and merged record limits, then merges by ID
in one transaction; it does not replace the whole database or delete unrelated
existing records. Keep exports and backups private and outside the repository.

The main `LUIGI_WEB_UI_TOKEN` session and separate `LUIGI_WEB_FINANCE_TOKEN` unlock
both protect Finance. Both tokens are deployment-managed secrets, excluded from
the Admin editor. Browser POSTs retain same-origin CSRF checks. Finance responses
use `Cache-Control: no-store`; the new routes also reject query strings and use
generic errors with `Referrer-Policy: no-referrer`.

The host also replaces legacy Finance validation responses with a generic error,
so malformed uploads cannot echo submitted fields. Overview and Planning clear
their private rendered content when leaving or locking; restored browser-history
pages reload through authentication before displaying Finance again.

Plan values, IDs, and ledger filters travel in request bodies, not URLs. Financial
values are not persisted in browser storage or sent to LLM tools, global search,
telemetry, or logs. CSV uploads are previewed locally in memory and not retained.
Use aliases such as `Everyday account` and `Brokerage`, never account identifiers
or personal details. Charts use locally served Chart.js, with tables when charts
are unavailable; there is no CDN, external calculation service, or bank linking.
See [../SECURITY.md](../SECURITY.md) for security and privacy requirements.

## New routes

All of these routes require the main session and Finance unlock. POSTs retain
browser CSRF protection even when they only calculate or filter.

| Method | Route | Purpose |
|---|---|---|
| GET | `/finance`, `/finance/overview` | Recorded-finances overview |
| POST | `/finance/overview/filter` | Body-only ledger filters and pagination |
| GET | `/finance/planning`, `/finance/planning/state` | Planning page and current private state |
| POST | `/finance/planning/calculate`, `/finance/planning/compare` | Calculate or compare without ledger writes |
| POST | `/finance/planning/save`, `/finance/planning/delete` | Version-checked saved-plan mutations |
| POST | `/finance/planning/income/save`, `/finance/planning/income/delete` | Version-checked income-stream mutations |

## Synthetic preview and checks

[../scripts/preview_finance.py](../scripts/preview_finance.py) creates disposable
Finance storage and generated preview sessions in a fresh process. It does not
read personal records, load production environment files, or import data from
`Finance_tester`. Database access is restricted to temporary storage and outbound
network access is blocked. Tokens are not printed or put in the preview URL, and
access logging is disabled. Use only generated synthetic records for screenshots.

From the repository root with dependencies installed:

```powershell
python scripts/preview_finance.py --check
python scripts/preview_finance.py --port 58111
```

The second command serves `http://127.0.0.1:58111/`; opening the root starts a
synthetic preview session. Close the process to discard its temporary database.
Validation commands, not a report of completed test runs:

```powershell
python -m unittest discover -s tests -p "test_finance*.py" -v
python scripts/validate_repo.py
git diff --check
```