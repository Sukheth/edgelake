# edgelake

CLI tool that turns Indian grocery-delivery invoice PDFs (Blinkit, Swiggy Instamart) into expense **drafts** in Chrome River (Emburse Enterprise). Never submits — human reviews and submits in Chrome River.

## Pipeline

```
fetch  →  reconcile  →  parse-pending  →  verify  →  rename  →  upload
```

| Stage | What it does | Ledger status |
|---|---|---|
| **fetch** | Drives Blinkit in a browser, downloads invoice PDFs into `receipts/inbox/` | `fetched` |
| **reconcile** | Hashes every PDF in `inbox/` and `processed/` and indexes it in the ledger | `fetched` |
| **parse-pending** | Extracts amount + date from each PDF | `parsed` |
| **verify** | Compares listing-UI amount vs PDF amount, applies expense policy | `verified` / `skipped` / `needs_approval` |
| **rename** | Renames files to `Blinkit_ORDID_YYYY-MM-DD_HHMM_AMOUNT.pdf` | — |
| **upload** | Drives Chrome River, creates one draft report with one line item per receipt | `drafted` |

Orchestrate the whole pipeline with `edgelake run`.

## Setup

```bash
cd c:/BuildsAndScripts/edgelake
python -m venv .venv
.venv/Scripts/activate
pip install -e .
playwright install chromium
cp .env.example .env   # edit DEFAULT_LOCATION, DEFAULT_PROJECT_CODE, etc.
```

First run on each merchant opens a Chromium window — complete Okta SSO (Chrome River) or phone OTP (Blinkit) once. Sessions persist in `.playwright-profiles/`.

## Commands

### End-to-end

```bash
edgelake run                    # interactive: fetch → … → upload, prompts on verify
edgelake run --auto             # unattended, no interactive prompts
edgelake run --no-fetch         # start from existing inbox
edgelake run --no-upload        # everything except the Chrome River draft
```

### Per-stage

```bash
edgelake fetch --merchant blinkit
edgelake fetch --merchant blinkit --since 2026-05-01T00:00:00   # override watermark

edgelake reconcile              # index inbox + processed into the ledger
edgelake parse-pending          # extract amount + date for unparsed receipts
edgelake verify                 # interactive amount review (default)
edgelake verify --auto          # apply policy without prompts
edgelake rename                 # canonical filenames
edgelake upload                 # create Chrome River draft
edgelake upload --dry-run       # parse + report only, no browser
```

### Debug

```bash
edgelake parse receipts/inbox/<file>.pdf   # one PDF, dump fields
edgelake parse-all                         # every PDF in inbox/, summary table
```

## Expense policy

Set in [`edgelake/config.py`](edgelake/config.py):

| Amount (INR) | Action |
|---|---|
| ≤ ₹1,000 | Use exact amount |
| ₹1,000 – ₹1,100 | Cap to ₹1,000 |
| ≥ ₹1,100 | Mark `needs_approval`, move to `receipts/needs-approval/`, **do not upload** |

## Layout

```
edgelake/
  cli.py             # Click entry point (one verb per command)
  config.py          # all paths + env vars (single source of truth)
  ledger.py          # SQLite at ledger.sqlite — order_id PK, sha column
  parsers/pdf.py     # PDF -> Receipt(merchant, date, amount, ...)
  fetchers/blinkit.py
  emburse/
    session.py       # persistent Chromium context + SSO wait
    uploader.py      # drives Chrome River form
receipts/
  inbox/             # incoming PDFs
  processed/         # uploaded receipts
  needs-approval/    # over policy, awaiting human
.playwright-profiles/
  chromeriver/       # Okta/SAML session
  blinkit/           # OTP session
ledger.sqlite        # source of truth for "already drafted"
debug/               # screenshots (gitignored)
```

## Conventions

- All paths derive from `ROOT` in [`config.py`](edgelake/config.py). Don't hardcode paths elsewhere.
- Env vars: `CHROMERIVER_URL`, `DEFAULT_CATEGORY`, `DEFAULT_CURRENCY`, `DEFAULT_LOCATION`, `DEFAULT_PROJECT_CODE`, `BLINKIT_URL` — defaults in `config.py`.
- The ledger (`order_id` PK) is the source of truth for "already drafted." If the upload fails partway, the inbox is left untouched so the next run retries the same set.
- For Blinkit fetcher quirks (no listing-page invoice URLs, address modal, ID enumeration), see [`CLAUDE.md`](CLAUDE.md).

## Status

Phase 1 (uploader) and Phase 2 (Blinkit fetcher) both shipped. End-to-end `edgelake run` works.
