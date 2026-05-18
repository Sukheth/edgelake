from __future__ import annotations

import json
import re
from pathlib import Path

from google import genai
from google.genai import types

from ..config import GEMINI_API_KEY, GEMINI_MODEL

_SYSTEM = """\
You extract structured data from receipt or invoice images and PDFs.
Return ONLY a JSON object — no markdown, no explanation — with these keys:
  merchant      : string  (store name, e.g. "Blinkit", "Swiggy Instamart", "Zepto", "Swiggy", restaurant name)
  date          : string  (YYYY-MM-DD)
  amount        : number  (total amount paid, as a float)
  currency      : string  (ISO code, e.g. "INR")
  receipt_type  : string  ("meal" or "snacks")
  confidence    : string  ("high" or "low")

For receipt_type:
  "meal"   — a restaurant bill, sit-down or delivery meal from a restaurant/food app (e.g. Swiggy food order, Zomato, any restaurant receipt)
  "snacks" — grocery delivery, convenience items, chocolates, drinks, snacks (e.g. Blinkit, Swiggy Instamart, Zepto grocery orders)

Use null for any field you cannot determine.
"""

_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def extract_receipt(file_path: Path) -> dict:
    """Send a receipt file to Gemini and return parsed fields as a dict.

    Supports PDFs and images (JPEG, PNG, WebP).
    Raises ValueError if the response can't be parsed as JSON.
    """
    suffix = file_path.suffix.lower()
    if suffix not in _MEDIA_TYPES:
        raise ValueError(f"Unsupported file type: {suffix}")

    client = genai.Client(api_key=GEMINI_API_KEY or None)
    data = file_path.read_bytes()
    mime = _MEDIA_TYPES[suffix]

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=data, mime_type=mime),
            "Extract receipt data.",
        ],
        config=types.GenerateContentConfig(system_instruction=_SYSTEM),
    )

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned non-JSON: {raw[:200]}") from exc
