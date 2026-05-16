from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import LEDGER_PATH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(LEDGER_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts (
            sha256 TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            merchant TEXT,
            date TEXT,
            amount REAL,
            currency TEXT,
            status TEXT NOT NULL,
            draft_url TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_state (
            merchant TEXT PRIMARY KEY,
            last_fetched_at TEXT NOT NULL,
            last_order_id TEXT
        )
        """
    )
    return conn


def get_last_fetched(merchant: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_fetched_at FROM fetch_state WHERE merchant = ?",
            (merchant,),
        ).fetchone()
    return row[0] if row else None


def set_last_fetched(merchant: str, ts_iso: str, last_order_id: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO fetch_state (merchant, last_fetched_at, last_order_id)
            VALUES (?, ?, ?)
            """,
            (merchant, ts_iso, last_order_id),
        )


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def already_drafted(sha: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM receipts WHERE sha256 = ?", (sha,)
        ).fetchone()
    return row is not None and row[0] == "drafted"


def record(
    sha: str,
    filename: str,
    merchant: str | None,
    date: str | None,
    amount: float | None,
    currency: str | None,
    status: str,
    draft_url: str | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO receipts
            (sha256, filename, merchant, date, amount, currency, status, draft_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha,
                filename,
                merchant,
                date,
                amount,
                currency,
                status,
                draft_url,
                datetime.utcnow().isoformat(),
            ),
        )
