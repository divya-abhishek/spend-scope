# SpendScope — Universal CSV Spend Analyzer
<img src="https://i.postimg.cc/R0kn7fwg/HTT.png" alt="website preview">

SpendScope is a local-first personal finance dashboard for importing transaction CSV files from different finance apps, banks, cards, wallets, or manually maintained exports and turning them into one normalized ledger for trend analysis.

The v2 importer **does not require Money Pro's fixed schema**. Accounts, categories, transaction types, column names, and currencies are discovered from the files you import.

## What changed in v2

- Universal CSV schema inference instead of a fixed `TIME, TYPE, AMOUNT, CATEGORY, ACCOUNT, NOTES` requirement.
- Dynamic account/category/type/currency filters generated from the database — no hardcoded account or category lists.
- Large native account/category dropdowns were replaced with a cleaner filter drawer.
- If a CSV is ambiguous, SpendScope shows a one-time **column mapping screen** before importing it.
- Transfer route strings are excluded from the normal account filter so values such as `Savings->Salary Account` do not clutter expense-account filtering.
- Currency-aware transaction storage and formatting.
- Cleaner desktop UI with a simplified top navigation, summary hero, denser charts, dynamic category ranking, signal cards, and improved transaction explorer.
- Imported-file manager now records how new files were mapped.

## CSV formats supported

SpendScope automatically tries to identify the following roles:

- Date / time
- Signed amount
- Debit / money-out amount
- Credit / money-in amount
- Transaction type / direction
- Description / merchant / memo / narration
- Category
- Account / wallet / card
- Currency

Only a **date** and a **monetary value** are required. Category and account columns are optional.

### Examples that import automatically

Signed amount:

```csv
Date,Merchant,Amount,Category
2026-08-01,Coffee,-250,Food
2026-08-02,Salary,50000,Income
```

Separate debit and credit:

```csv
Posted Date,Description,Debit,Credit,Account Name
13/08/2026,Coffee,250,,Card
14/08/2026,Salary,,50000,Bank
```

Minimal spending export:

```csv
when,details,value
2026-08-01,coffee,250
2026-08-02,taxi,400
```

For a minimal positive-only file like the third example, SpendScope treats the values as expenses and records an import warning because no debit/credit direction was available.

## Auto-detection details

The parser handles:

- Common header aliases such as `Date`, `Posted Date`, `Transaction Date`, `Merchant`, `Payee`, `Memo`, `Narration`, `Wallet`, `Card`, `Debit`, `Credit`, `Outflow`, `Inflow`, and many others.
- UTF-8, UTF-8 BOM, Windows-1252, and Latin-1 encodings.
- Comma, semicolon, tab, pipe, and automatically sniffed delimiters.
- `1,234.56` and `1.234,56` numeric formats.
- Negative numbers, accounting parentheses, and trailing minus signs.
- Text dates, ISO dates, common numeric day/month formats, and Excel serial dates.
- Signed amount exports where negatives are expenses and positives are income.
- Debit/credit split exports.
- Explicit transfer rows when the source file has a transaction-type field.

When automatic confidence is too low, the UI asks you to map the columns instead of guessing silently.

## Dynamic filtering

There are no fixed account or category options in the frontend.

The filter drawer is populated from the values actually stored in your imported transactions:

- Transaction type
- Category
- Account / wallet
- Currency

If a future CSV contains completely different category or account names, they appear automatically after import.

## Analytics

- Total expenses excluding transfers
- Income and net cash flow
- Daily average and median expense
- Top-five spending concentration
- Daily spend with a 7-day rolling average
- Month-over-month spend trajectory
- Weekday and time-of-day patterns
- Category ranking and category movement by month
- IQR statistical outlier detection
- Recurring description / merchant-like pattern extraction
- Automated observations
- Full searchable transaction ledger
- Multi-file and multi-month analysis

## Run on Windows

1. Install Python 3.11+.
2. Extract the project ZIP.
3. Double-click `start_windows.bat`.
4. Open `http://127.0.0.1:8000` if it does not open automatically.

The first run creates a virtual environment, installs Python dependencies, and initializes `data/spend_analyzer.db`.

## Run manually

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python run.py
```

Then visit `http://127.0.0.1:8000`.

## Importing future months

Use **Import CSV** and select one or many files. SpendScope will:

1. Read the file and detect its delimiter/encoding.
2. Infer the transaction columns.
3. Normalize rows into the common SQLite ledger.
4. Detect exact duplicate files using SHA-256.
5. Ask for column mapping only if required fields are ambiguous.
6. Refresh all account/category/month filters and analytics from the newly imported data.

The CSV filename does not determine the month. Months are derived from the transaction dates inside each file.

## API

- `GET /api/health`
- `GET /api/meta`
- `POST /api/preview-file`
- `POST /api/import-file`
- `POST /api/import` — backward-compatible bulk auto-import
- `GET /api/dashboard`
- `GET /api/transactions`
- `DELETE /api/imports/{id}`

Dashboard filters can be repeated query parameters, for example:

```text
/api/dashboard?months=2026-07&categories=Food&accounts=Salary%20Account
```

## Storage and privacy

SpendScope is designed to run on your own PC. Imported transactions are stored in:

`data/spend_analyzer.db`

The backend and database are local. Chart.js is currently loaded by the frontend from jsDelivr, so chart rendering requires network access unless you vendor that JavaScript file into the project.

## Classification rules

- Explicit source transaction types are preferred when recognized.
- Signed amount exports: negative values become expenses; positive values become income when both signs exist.
- Debit columns become expenses.
- Credit columns become income.
- Positive-only amount files with no direction field are treated as expenses and get an import warning.
- Transfers are excluded from spend and net-cash-flow calculations when the source data identifies them as transfers.
- Rows that cannot be confidently classified are retained as `other` instead of being silently discarded.

## Your supplied July export

The existing July sample remains seeded in this build for immediate testing. The generalized parser still resolves it to the same core result: ₹61,387.09 of expenses from 118 expense transactions, while keeping income and transfers separate.
