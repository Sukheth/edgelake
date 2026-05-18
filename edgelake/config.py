from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CHROMERIVER_URL = os.getenv("CHROMERIVER_URL", "https://app.eu1.chromeriver.com/")
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "Meals - Chocolate/Dessert/Snacks")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "INR")
# Reused across every line item in a run. Set these in .env.
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "India")
DEFAULT_PROJECT_CODE = os.getenv("DEFAULT_PROJECT_CODE", "")

BLINKIT_URL = os.getenv("BLINKIT_URL", "https://blinkit.com/account/orders")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Haiku by default — fast and cheap for receipt OCR
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

INBOX = ROOT / "receipts" / "inbox"
PROCESSED = ROOT / "receipts" / "processed"
NEEDS_APPROVAL = ROOT / "receipts" / "needs-approval"

# Expense policy thresholds (INR).
#   amount <= POLICY_EXACT_MAX            -> use exact amount
#   POLICY_EXACT_MAX < amount < POLICY_APPROVAL_MIN -> cap to POLICY_EXACT_MAX
#   amount >= POLICY_APPROVAL_MIN         -> needs human approval, do not upload
POLICY_EXACT_MAX = 1000.0
POLICY_APPROVAL_MIN = 1100.0
PROFILE_DIR = ROOT / ".playwright-profiles" / "chromeriver"
BLINKIT_PROFILE_DIR = ROOT / ".playwright-profiles" / "blinkit"
DEBUG_DIR = ROOT / "debug"
LEDGER_PATH = ROOT / "ledger.sqlite"
