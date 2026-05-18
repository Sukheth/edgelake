from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CHROMERIVER_URL = os.getenv("CHROMERIVER_URL", "https://app.eu1.chromeriver.com/")
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "Meals - Chocolate/Dessert/Snacks")
INDIVIDUAL_MEALS_CATEGORY = os.getenv("INDIVIDUAL_MEALS_CATEGORY", "Individual Meals only (around Client Site or While Travelling)")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "INR")
# Reused across every line item in a run. Set these in .env.
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "India")
DEFAULT_PROJECT_CODE = os.getenv("DEFAULT_PROJECT_CODE", "")

BLINKIT_URL = os.getenv("BLINKIT_URL", "https://blinkit.com/account/orders")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Flash by default — fast and cheap for receipt OCR
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

INBOX = ROOT / "receipts" / "inbox"
PROCESSED = ROOT / "receipts" / "processed"
NEEDS_APPROVAL = ROOT / "receipts" / "needs-approval"
FAILED = ROOT / "receipts" / "failed"

# Expense policy thresholds (INR).
#   amount <= POLICY_EXACT_MAX            -> use exact amount
#   POLICY_EXACT_MAX < amount < POLICY_APPROVAL_MIN -> cap to POLICY_EXACT_MAX
#   amount >= POLICY_APPROVAL_MIN         -> needs human approval, do not upload
POLICY_EXACT_MAX = float(os.getenv("POLICY_EXACT_MAX", "1000"))
POLICY_APPROVAL_MIN = float(os.getenv("POLICY_APPROVAL_MIN", "1100"))
# Meal receipts (restaurant bills) have a higher approval threshold.
POLICY_MEAL_APPROVAL_MIN = float(os.getenv("POLICY_MEAL_APPROVAL_MIN", "3000"))
PROFILE_DIR = ROOT / ".playwright-profiles" / "chromeriver"
BLINKIT_PROFILE_DIR = ROOT / ".playwright-profiles" / "blinkit"
DEBUG_DIR = ROOT / "debug"
LEDGER_PATH = ROOT / "ledger.sqlite"
