# edgelake

**Automatically file your grocery delivery receipts as expenses in Chrome River (Emburse Enterprise) — no manual data entry.**

edgelake watches for your Blinkit and Swiggy Instamart invoices, reads the amounts and dates, and creates draft expense line items in Chrome River for you. You just review and submit. The tool never submits on your behalf.

---

## What it does

Every time you order from Blinkit or Swiggy Instamart, you get an invoice. Normally you'd have to download it, open Chrome River, type in the amount, date, and merchant, upload the PDF, and repeat for every order. edgelake does all of that automatically.

**It works in two ways:**

### 1. Telegram bot (recommended for day-to-day use)

You forward a photo or PDF of any receipt to a private Telegram bot you set up. The bot reads the receipt using Google's Gemini AI, extracts the merchant name, date, and amount, and saves it to a queue on your laptop. The next time you run `edgelake run`, it files everything queued straight into Chrome River as draft expenses.

This works even when your laptop is off — Telegram holds messages for 24 hours. So you can forward receipts from your phone immediately after an order, and file them in bulk whenever you're ready.

### 2. Blinkit auto-fetch

edgelake can log into your Blinkit account and automatically download all recent invoices. It opens a real Chrome browser window, navigates through your order history, and downloads each invoice PDF — the same thing you'd do manually, just automated.

---

## Day-to-day usage

Once set up, your routine is:

**Filing receipts sent via Telegram:**
1. Forward receipt photos or PDFs to your Telegram bot throughout the day/week.
2. When ready to file expenses, open a terminal and run:
   ```
   edgelake run --no-fetch
   ```
3. Chrome River opens in a browser window. Log in with Okta if prompted. edgelake creates all the draft expense line items automatically.
4. Open Chrome River yourself, review the drafts, and submit.

**Filing Blinkit receipts automatically:**
1. Run:
   ```
   edgelake run
   ```
2. A Chrome browser opens to Blinkit. Log in with your phone OTP if prompted (first time only — sessions are saved).
3. edgelake downloads all recent invoices, then opens Chrome River and files them as drafts.
4. Review and submit in Chrome River.

---

## Setup

> **Prerequisites:** You need Python installed on your computer. Download it from [python.org](https://www.python.org/downloads/) — choose the latest version and check "Add Python to PATH" during installation.

### Step 1 — Install edgelake

Open a terminal (search for "Command Prompt" or "PowerShell" in the Start menu), navigate to where you cloned this repository, and run:

```
pip install git+https://github.com/Sukheth/edgelake.git
```

Or if you have `pipx` installed (recommended — keeps edgelake isolated):

```
pipx install git+https://github.com/Sukheth/edgelake.git
```

> **What is pipx?** It's a tool for installing Python command-line apps cleanly. Install it once with `pip install pipx`, then use it like `brew install` on Mac. It handles everything for you.

### Step 2 — Run setup

```
edgelake setup
```

This will:
- Install the browser automation engine (Playwright Chromium)
- Ask you for your settings and API keys one by one
- Write everything to a `.env` file in the project folder

You'll be prompted for four things:
- **Telegram bot token** — see [Creating a Telegram bot](#creating-a-telegram-bot) below
- **Gemini API key** — see [Getting a Gemini API key](#getting-a-gemini-api-key) below
- **Default location** — your office location, e.g. `India`
- **Default project code** — your billing/project code for Chrome River (ask your manager if unsure)

### Step 3 — First-time logins

edgelake uses a real browser to drive Chrome River and Blinkit. The first time you run it, it will open a Chromium browser window and wait for you to log in. After you log in once, your session is saved and future runs won't ask again.

**Chrome River (Okta login):**  
The first time you run `edgelake run` or `edgelake upload`, a Chromium window opens to Chrome River. You'll see the Okta login page — sign in as you normally would (username, password, MFA). Once you're on the Chrome River dashboard, edgelake takes over automatically. You have up to 15 minutes to complete the login.

**Blinkit (phone OTP):**  
The first time you run `edgelake fetch` or `edgelake run`, a Chromium window opens to Blinkit. Enter your phone number and the OTP sent to you. Once the orders page loads, edgelake takes over. Again, this only happens once — sessions are saved.

---

## Creating a Telegram bot

You need your own private Telegram bot. This takes about 2 minutes and is completely free.

1. Open Telegram on your phone or desktop.
2. Search for **@BotFather** and open the chat.
3. Send the message: `/newbot`
4. BotFather will ask for a **name** — this is the display name, e.g. `My Expense Bot`.
5. It will then ask for a **username** — this must end in `bot`, e.g. `myexpense_bot`. It must be unique across all of Telegram.
6. BotFather will reply with a message containing your **bot token** — a long string that looks like `7123456789:AAFdef1234...`. **Copy this token — you'll need it during `edgelake setup`.**
7. Search for your bot's username in Telegram and open a chat with it.
8. Send `/start` to your bot. This is required — Telegram bots cannot message you until you initiate contact first.

That's it. Your bot is ready. Only people you share the username with can find it, and it only responds to files/photos — it won't respond to random messages.

---

## Getting a Gemini API key

edgelake uses Google's Gemini AI to read receipt photos and extract the date, merchant, and amount. The free tier is more than enough for personal expense use — **1,500 receipt reads per day at no cost**.

> **Important:** Get your key from Google AI Studio, not Google Cloud Console. Cloud Console keys don't have the free tier enabled by default.

1. Go to **[aistudio.google.com](https://aistudio.google.com/)** and sign in with any Google account.
2. Click **"Get API key"** in the left sidebar.
3. Click **"Create API key"**.
4. Select **"Create API key in new project"** (or pick an existing project — doesn't matter).
5. Your key will appear — it starts with `AIza`. **Copy it — you'll need it during `edgelake setup`.**

The free tier includes 1,500 requests/day and 1 million tokens/minute on Gemini 2.0 Flash. Each receipt scan uses roughly 1 request, so you'd have to process 1,500 receipts in a single day to hit the limit — effectively unlimited for personal use.

---

## Expense categories

edgelake automatically picks the right Chrome River expense category for each receipt:

| Receipt type | Chrome River category | Approval threshold |
|---|---|---|
| Grocery delivery / snacks (Blinkit, Swiggy Instamart, Zepto) | Meals - Chocolate/Dessert/Snacks | ₹1,100 |
| Restaurant meal / food delivery (Swiggy food, Zomato, any restaurant bill) | Individual Meals only (around Client Site or While Travelling) | ₹3,000 |

For receipts sent via Telegram, Google Gemini reads the receipt and decides which type it is — a grocery/snack order or a proper meal. Blinkit invoices are always treated as snacks.

## Expense policy

edgelake automatically applies expense policy rules before filing. The thresholds are set during `edgelake setup` and can be changed anytime by running `edgelake setup` again.

The defaults are:

**Snacks (Blinkit, Instamart, etc.):**

| Amount | What happens |
|---|---|
| ₹1,000 or less | Filed at the exact amount |
| ₹1,001 – ₹1,099 | Capped to ₹1,000 automatically |
| ₹1,100 or more | **Not filed** — moved to `receipts/needs-approval/` for manual review |

**Meals (restaurant bills):**

| Amount | What happens |
|---|---|
| Under ₹3,000 | Filed at the exact amount |
| ₹3,000 or more | **Not filed** — moved to `receipts/needs-approval/` for manual review |

Receipts that exceed the approval threshold are never uploaded automatically. They sit in `receipts/needs-approval/` on your laptop until you handle them manually in Chrome River.

To change the thresholds, run `edgelake setup` — it will show your current values in brackets and let you update them.

---

## Commands reference

```
edgelake setup                  configure API keys and settings
edgelake telegram               start the Telegram bot (keep running in background)
edgelake run                    full pipeline: fetch from Blinkit + upload to Chrome River
edgelake run --no-fetch         skip Blinkit fetch, just file what's already queued
edgelake run --auto             run without any interactive prompts
edgelake upload --dry-run       show what would be filed without opening Chrome River
```

---

---

## Technical reference

This section is for developers or people who want to understand how edgelake works under the hood.

### Architecture

```
[Telegram bot]  ─┐
                  ├→  receipts/inbox/  →  verify  →  rename  →  upload  →  Chrome River
[edgelake fetch] ─┘
```

| Stage | What it does | Ledger status |
|---|---|---|
| **telegram** | Bot receives receipt photo/PDF, calls Gemini to extract fields, saves to `inbox/` | `parsed` |
| **fetch** | Drives Blinkit in a real browser, downloads invoice PDFs into `receipts/inbox/` | `fetched` |
| **reconcile** | SHA-256 hashes every PDF in `inbox/` and `processed/`, indexes in ledger | `fetched` |
| **parse-pending** | Extracts amount + date from each PDF using pdfplumber | `parsed` |
| **verify** | Compares listing-UI amount vs PDF amount, applies expense policy thresholds | `verified` / `needs_approval` |
| **rename** | Renames files to `Blinkit_ORDID_YYYY-MM-DD_HHMM_AMOUNT.pdf` | — |
| **upload** | Drives Chrome River via Playwright, creates one draft report per run | `drafted` |

Telegram receipts enter at `parsed` (Gemini extracts fields immediately) and skip reconcile/parse-pending. Blinkit receipts enter at `fetched` and go through the full pipeline.

### Module layout

```
edgelake/
  cli.py                  # Click entry point — one command per verb
  config.py               # All paths + env vars (single source of truth)
  ledger.py               # SQLite at ledger.sqlite — order_id PK, dedup via sha256
  parsers/pdf.py          # PDF → Receipt(merchant, date, amount, currency, raw_text)
  fetchers/blinkit.py     # Blinkit browser automation
  telegram_bot/
    bot.py                # Telegram polling bot (python-telegram-bot)
    vision.py             # Gemini multimodal receipt extraction
  emburse/
    session.py            # Persistent Chromium context + SSO wait (up to 15 min)
    uploader.py           # Chrome River form automation
receipts/
  inbox/                  # Incoming PDFs waiting to be filed
  processed/              # Successfully uploaded receipts
  needs-approval/         # Over policy threshold, awaiting manual review
.playwright-profiles/
  chromeriver/            # Saved Okta/SAML session
  blinkit/                # Saved phone OTP session
ledger.sqlite             # Source of truth for dedup and status tracking
debug/                    # Debug screenshots (gitignored)
```

### Environment variables

All variables are set in a `.env` file at the project root. Run `edgelake setup` to configure them interactively.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token from @BotFather |
| `GEMINI_API_KEY` | — | API key from Google AI Studio |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model to use for receipt extraction |
| `CHROMERIVER_URL` | `https://app.eu1.chromeriver.com/` | Your Chrome River instance URL |
| `DEFAULT_CATEGORY` | `Meals - Chocolate/Dessert/Snacks` | Category for snack/grocery receipts |
| `INDIVIDUAL_MEALS_CATEGORY` | `Individual Meals only (around Client Site or While Travelling)` | Category for restaurant/meal receipts |
| `DEFAULT_CURRENCY` | `INR` | Currency for all expenses |
| `DEFAULT_LOCATION` | `India` | Location field in Chrome River |
| `DEFAULT_PROJECT_CODE` | — | Project/billing code in Chrome River |
| `BLINKIT_URL` | `https://blinkit.com/account/orders` | Blinkit orders page URL |
| `POLICY_EXACT_MAX` | `1000` | Snacks: amounts at or below this are filed exactly |
| `POLICY_APPROVAL_MIN` | `1100` | Snacks: amounts at or above this are held for manual review |
| `POLICY_MEAL_APPROVAL_MIN` | `3000` | Meals: amounts at or above this are held for manual review |

### Persistent browser sessions

Two separate Chromium profiles are maintained:

- `.playwright-profiles/chromeriver/` — stores your Okta/SAML session for Chrome River. Valid until your Okta session expires (typically days to weeks). When it expires, edgelake opens the browser and waits up to 15 minutes for you to log in again.
- `.playwright-profiles/blinkit/` — stores your Blinkit phone OTP session. Re-login required when this expires.

Both directories are gitignored. They are never shared or uploaded anywhere.

### Deduplication

Every receipt file is SHA-256 hashed before processing. The hash is stored in `ledger.sqlite`. If the same file is sent to the Telegram bot twice, or appears in the inbox after already being processed, it is silently skipped. The ledger is the single source of truth for "has this been filed in Chrome River."

### Corporate network / TLS

On corporate networks with MITM TLS inspection (common with Zscaler, BlueCoat, etc.), Python's default SSL library rejects the proxy's certificate. edgelake uses `truststore` to pull certificates from the Windows certificate store instead, which already trusts your corporate CA. No configuration needed — it works automatically.

### Telegram offline buffering

Telegram holds undelivered messages for 24 hours. If you forward a receipt while the bot isn't running, it will be delivered and processed the next time you start the bot. SHA-256 deduplication means restarting the bot frequently is safe — previously processed receipts are never double-filed.
