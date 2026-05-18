# edgelake

CLI tool that turns Indian grocery-delivery invoice PDFs (Blinkit, Swiggy Instamart) into expense **drafts** in Chrome River (Emburse Enterprise).

Two ways to get receipts in:

- **Telegram bot** — forward any receipt photo or PDF to your bot anytime. Gemini extracts merchant, date, and amount immediately. Works offline (Telegram buffers for 24 h).
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

```
setup.bat
```

That's it. The script creates a virtualenv, installs dependencies, installs Playwright Chromium, and walks you through filling in `.env` interactively (bot token, Gemini key, location, project code).

After it finishes, activate the venv once per terminal session:

```
.venv\Scripts\activate
```

First run on each merchant opens a Chromium window — complete Okta SSO (Chrome River) or phone OTP (Blinkit) once. Sessions persist in `.playwright-profiles/`.

## Commands

### Telegram bot

```bash
edgelake telegram          # start the bot (blocks; Ctrl-C to stop)
```

Send any receipt photo or PDF to your bot. It replies with the extracted fields and queues the file in `receipts/inbox/`. Then run `edgelake run --no-fetch` to file everything queued.

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

## Creating your Telegram bot and Gemini key

### Telegram bot (one-time)

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts — choose a name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:ABCdef...`. Copy it.
4. Add it to `.env`: `TELEGRAM_BOT_TOKEN=123456789:ABCdef...`
5. Start a chat with your new bot (search for its username) and send `/start` — this is required before the bot can message you back.
6. Find your personal chat ID: start a chat with [@userinfobot](https://t.me/userinfobot) and it will reply with your numeric ID. You don't need this in `.env`, but it's useful to know for debugging.

### Gemini API key (one-time)

1. Go to [Google AI Studio](https://aistudio.google.com/) and sign in with a Google account.
2. Click **Get API key** → **Create API key**.
3. Copy the key.
4. Add it to `.env`: `GEMINI_API_KEY=AIza...`

> **Note:** Use an AI Studio key, not a Google Cloud Console key. Cloud Console keys require billing to be enabled for the free-tier quota to work.

## Environment variables

Set these in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From [@BotFather](https://t.me/BotFather) — see above |
| `GEMINI_API_KEY` | — | From [Google AI Studio](https://aistudio.google.com/) — see above |
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
