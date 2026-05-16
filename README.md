# edgelake

Automates filing Swiggy Instamart / Blinkit receipts as expense drafts in Chrome River (Emburse Enterprise).

## Phase 1 (current): Chrome River uploader

Drop PDF receipts into `receipts/inbox/`, run the uploader, it creates **drafts** in Chrome River for you to review and submit manually.

### Setup

```bash
cd c:/BuildsAndScripts/edgelake
python -m venv .venv
.venv/Scripts/activate
pip install -e .
playwright install chromium
cp .env.example .env
```

### Run

```bash
# Test PDF parser on a single file
edgelake parse receipts/inbox/some_receipt.pdf

# Upload all PDFs in inbox as drafts
edgelake upload
```

First run will pause and ask you to complete Okta login in the Chromium window, then press Enter. Session persists across runs in `.playwright-profiles/chromeriver/`.

## Phase 2 (later): Auto-fetch from Swiggy/Blinkit

Not built yet. For now, manually download invoice PDFs from the apps into `receipts/inbox/`.
