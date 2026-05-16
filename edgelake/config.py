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

INBOX = ROOT / "receipts" / "inbox"
PROCESSED = ROOT / "receipts" / "processed"
PROFILE_DIR = ROOT / ".playwright-profiles" / "chromeriver"
BLINKIT_PROFILE_DIR = ROOT / ".playwright-profiles" / "blinkit"
DEBUG_DIR = ROOT / "debug"
LEDGER_PATH = ROOT / "ledger.sqlite"
