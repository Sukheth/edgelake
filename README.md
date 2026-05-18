# edgelake

CLI tool that turns Indian grocery-delivery invoice PDFs (Blinkit, Swiggy Instamart) into expense **drafts** in Chrome River (Emburse Enterprise).

Two ways to get receipts in:

- **Telegram bot** — forward any receipt photo or PDF to [@edgelakebot](https://t.me/edgelakebot) anytime. Gemini extracts merchant, date, and amount immediately. Works offline (Telegram buffers for 24 h).
- **Blinkit fetcher** — drives the Blinkit orders UI in a real browser to download invoice PDFs automatically.

## Pipeline

```
[Telegram bot]  ─┐
                  ├→  inbox/  →  verify  →  rename  →  upload  →  Chrome River
[edgelake fetch] ─┘
```

| Stage | What it does | Ledger status |
|---|---|---|
| **telegram** | Bot receives receipt photo/PDF, calls Gemini to extract fields, saves to `inbox/` | `parsed` |
| **fetch** | Drives Blinkit in a browser, downloads invoice PDFs into `receipts/inbox/` | `fetched` |
| **reconcile** | Hashes every PDF in `inbox/` and `processed/` and indexes it in the ledger | `fetched` |
| **parse-pending** | Extracts amount + date from each PDF | `parsed` |
| **verify** | Compares listing-UI amount vs PDF amount, applies expense policy | `verified` / `skipped` / `needs_approval` |
| **rename** | Renames files to `Blinkit_ORDID_YYYY-MM-DD_HHMM_AMOUNT.pdf` | — |
| **upload** | Drives Chrome River, creates one draft report with one line item per receipt | `drafted` |

Orchestrate the Blinkit → Chrome River pipeline with `edgelake run`. Telegram receipts enter directly at `parsed` and skip straight to `verify`.

## Setup

```bash
cd c:/BuildsAndScripts/edgelake
python -m venv .venv
.venv/Scripts/activate
pip install -e .
playwright install chromium
cp .env.example .env   # edit DEFAULT_LOCATION, DEFAULT_PROJECT_CODE, TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
```

First run on each merchant opens a Chromium window — complete Okta SSO (Chrome River) or phone OTP (Blinkit) once. Sessions persist in `.playwright-profiles/`.

## Commands

### Telegram bot

```bash
edgelake telegram          # start the bot (blocks; Ctrl-C to stop)
```

Send any receipt photo or PDF to [@edgelakebot](https://t.me/edgelakebot). The bot replies with the extracted fields and queues the file in `receipts/inbox/`. Then run `edgelake run --no-fetch` to file everything queued.

### End-to-end (Blinkit)

```bash
edgelake run                    # interactive: fetch → … → upload, prompts on verify
edgelake run --auto             # unattended, no interactive prompts
edgelake run --no-fetch         # start from existing inbox (use after Telegram ingestion)
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

## Environment variables

Set these in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From [@BotFather](https://t.me/BotFather) |
| `GEMINI_API_KEY` | — | From [Google AI Studio](https://aistudio.google.com/) (not Google Cloud Console) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Use `gemini-2.0-flash` or `gemini-1.5-flash` |
| `CHROMERIVER_URL` | `https://app.eu1.chromeriver.com/` | |
| `DEFAULT_CATEGORY` | `Meals - Chocolate/Dessert/Snacks` | |
| `DEFAULT_CURRENCY` | `INR` | |
| `DEFAULT_LOCATION` | `India` | |
| `DEFAULT_PROJECT_CODE` | — | |
| `BLINKIT_URL` | `https://blinkit.com/account/orders` | |

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
  cli.py                  # Click entry point (one verb per command)
  config.py               # all paths + env vars (single source of truth)
  ledger.py               # SQLite at ledger.sqlite — order_id PK, sha column
  parsers/pdf.py          # PDF -> Receipt(merchant, date, amount, ...)
  fetchers/blinkit.py     # Blinkit browser automation
  telegram_bot/
    bot.py                # Telegram polling bot
    vision.py             # Gemini receipt extraction
  emburse/
    session.py            # persistent Chromium context + SSO wait
    uploader.py           # drives Chrome River form
receipts/
  inbox/                  # incoming PDFs (from bot or fetcher)
  processed/              # uploaded receipts
  needs-approval/         # over policy, awaiting human
.playwright-profiles/
  chromeriver/            # Okta/SAML session
  blinkit/                # OTP session
ledger.sqlite             # source of truth for "already drafted"
debug/                    # screenshots (gitignored)
```

## Notes

- The ledger (`order_id` PK) is the source of truth for "already drafted." If upload fails partway, the inbox is left untouched so the next run retries the same set.
- Telegram receipts use synthetic IDs (`TG<timestamp>_<sha6>`); Blinkit receipts use `ORD<digits>`.
- On a corporate network with MITM TLS inspection, `truststore` is used so Python trusts the Windows certificate store automatically.
- For Blinkit fetcher quirks (no listing-page invoice URLs, address modal, ID enumeration), see [`CLAUDE.md`](CLAUDE.md).
