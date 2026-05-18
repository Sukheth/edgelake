from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import anthropic

from ..config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

_SYSTEM = """\
You extract structured data from receipt or invoice images and PDFs.
Return ONLY a JSON object — no markdown, no explanation — with these keys:
  merchant   : string  (store name, e.g. "Blinkit", "Swiggy Instamart", "Zepto")
  date       : string  (YYYY-MM-DD)
  amount     : number  (total amount paid, as a float)
  currency   : string  (ISO code, e.g. "INR")
  confidence : string  ("high" or "low")
Use null for any field you cannot determine.
"""

_MEDIA_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def extract_receipt(file_path: Path) -> dict:
    """Send a receipt file to Claude and return parsed fields as a dict.

    Supports PDFs (sent as document) and images (sent as image block).
    Raises ValueError if Claude returns something that can't be parsed as JSON.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or None)
    data = file_path.read_bytes()
    b64 = base64.standard_b64encode(data).decode()
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        content_block: dict = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    elif suffix in _MEDIA_TYPES:
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": _MEDIA_TYPES[suffix], "data": b64},
        }
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=_SYSTEM,
        messages=[{"role": "user", "content": [content_block, {"type": "text", "text": "Extract receipt data."}]}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if Claude wraps it anyway.
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned non-JSON: {raw[:200]}") from exc
