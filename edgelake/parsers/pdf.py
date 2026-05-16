from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from pathlib import Path

import pdfplumber


@dataclass
class Receipt:
    merchant: str
    date: Date
    amount: float
    currency: str
    raw_text: str
    source_path: Path


MERCHANT_PATTERNS = [
    (re.compile(r"\b(swiggy\s*instamart|instamart)\b", re.I), "Swiggy Instamart"),
    (re.compile(r"\bswiggy\b", re.I), "Swiggy"),
    (re.compile(r"\bblink\s*it\b|\bblinkit\b|\bgrofers\b|blink\s*commerce", re.I), "Blinkit"),
    (re.compile(r"\bzepto\b", re.I), "Zepto"),
]

# Blinkit per-seller invoices each end with "Amount in Words: <words> Rupees ...".
# The numeric grand-total for that seller is the LAST number on the line immediately
# above the "Amount in Words" line (the "Total ... <grand_total>" row).
# We sum these across all seller pages.
BLINKIT_AMOUNT_IN_WORDS_BLOCK = re.compile(
    r"^Total\b[^\n]*?([0-9]+(?:,[0-9]{3})*\.[0-9]{2,3})\s*\n\s*Amount\s+in\b",
    re.M | re.I,
)

# Generic single-total fallbacks.
SINGLE_TOTAL_PATTERNS = [
    re.compile(r"(?:grand\s*total|total\s*payable|amount\s*paid|net\s*payable|order\s*total|bill\s*total|total\s*amount)\s*[:\-]?\s*(?:inr|rs\.?|₹)?\s*([0-9,]+(?:\.[0-9]{1,2})?)", re.I),
    re.compile(r"(?:inr|rs\.?|₹)\s*([0-9,]+\.[0-9]{2})\s*(?:\(?incl|only|paid)", re.I),
]
AMOUNT_FALLBACK = re.compile(r"(?:inr|rs\.?|₹)\s*([0-9,]+\.[0-9]{2})", re.I)

DATE_PATTERNS = [
    (re.compile(r"Invoice\s*Date\s*[:\-]?\s*(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", re.I), "dmy"),
    (re.compile(r"Invoice\s*Date\s*[:\-]?\s*(\d{1,2})[-\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s](\d{4})", re.I), "dmonthy"),
    (re.compile(r"Invoice\s*[:\-]?\s*(\d{1,2})[-\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s](\d{4})", re.I), "dmonthy"),
    (re.compile(r"\b(\d{1,2})[-\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s](\d{4})\b", re.I), "dmonthy"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"), "dmy"),
    (re.compile(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b"), "ymd"),
]


def _parse_amount(text: str, merchant: str) -> float | None:
    if merchant == "Blinkit":
        totals = [float(m.replace(",", "")) for m in BLINKIT_AMOUNT_IN_WORDS_BLOCK.findall(text)]
        if totals:
            return round(sum(totals), 2)
    for pat in SINGLE_TOTAL_PATTERNS:
        m = pat.search(text)
        if m:
            return float(m.group(1).replace(",", ""))
    matches = AMOUNT_FALLBACK.findall(text)
    if matches:
        return max(float(m.replace(",", "")) for m in matches)
    return None


def _parse_date(text: str) -> Date | None:
    months = {m: i for i, m in enumerate(
        ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1)}
    for pat, kind in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if kind == "dmy":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:
                    y += 2000
                return Date(y, mo, d)
            if kind == "ymd":
                return Date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "dmonthy":
                return Date(int(m.group(3)), months[m.group(2).lower()[:3]], int(m.group(1)))
        except ValueError:
            continue
    return None


def _parse_merchant(text: str) -> str:
    for pat, name in MERCHANT_PATTERNS:
        if pat.search(text):
            return name
    return "Unknown"


def parse_pdf(path: Path) -> Receipt:
    with pdfplumber.open(path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    merchant = _parse_merchant(text)
    amount = _parse_amount(text, merchant)
    date = _parse_date(text) or datetime.now().date()

    if amount is None:
        raise ValueError(f"Could not parse amount from {path.name}")

    return Receipt(
        merchant=merchant,
        date=date,
        amount=amount,
        currency="INR",
        raw_text=text,
        source_path=path,
    )
