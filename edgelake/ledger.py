from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import LEDGER_PATH, INBOX, PROCESSED


# Status lifecycle for a receipts row:
#   fetched   -> just downloaded, listing_amount populated, no pdf parse yet
#   parsed    -> pdf_amount populated; ready for verify
#   verified  -> chosen_amount + amount_source set; ready for rename + upload
#   skipped   -> user opted to skip during verify; never drafted
#   drafted   -> Chrome River draft created
#
# order_id is the canonical identity (Blinkit ORD<digits>). For files without
# a real order_id we synthesize one from the suggested filename or sha.
STATUSES = ("fetched", "parsed", "verified", "skipped", "needs_approval", "drafted")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(LEDGER_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts (
            order_id        TEXT PRIMARY KEY,
            sha256          TEXT,
            filename        TEXT,
            merchant        TEXT,
            date            TEXT,
            order_time      TEXT,
            currency        TEXT,
            listing_amount  REAL,
            pdf_amount      REAL,
            chosen_amount   REAL,
            amount_source   TEXT,
            receipt_type    TEXT DEFAULT 'snacks',
            status          TEXT NOT NULL,
            draft_url       TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_sha ON receipts(sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status)")
    try:
        conn.execute("ALTER TABLE receipts ADD COLUMN receipt_type TEXT DEFAULT 'snacks'")
    except Exception:
        pass  # column already exists
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


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- fetch_state watermark -------------------------------------------------

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


# --- receipts: lifecycle helpers ------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


def upsert_fetched(
    order_id: str,
    sha: str | None,
    filename: str,
    merchant: str,
    listing_amount: float | None,
    date: str | None = None,
    order_time: str | None = None,
) -> None:
    """Record a freshly-downloaded receipt. Idempotent on order_id.
    order_time is HHMM 24-hour, from the listing row (Blinkit displays it)."""
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT status FROM receipts WHERE order_id = ?", (order_id,)
        ).fetchone()
        if existing:
            # Already known. Only update listing_amount / filename / sha if
            # we have better info. Don't downgrade status.
            conn.execute(
                """
                UPDATE receipts
                SET sha256         = COALESCE(?, sha256),
                    filename       = COALESCE(?, filename),
                    merchant       = COALESCE(?, merchant),
                    date           = COALESCE(?, date),
                    order_time     = COALESCE(?, order_time),
                    listing_amount = COALESCE(?, listing_amount),
                    updated_at     = ?
                WHERE order_id = ?
                """,
                (sha, filename, merchant, date, order_time, listing_amount, now, order_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO receipts
                  (order_id, sha256, filename, merchant, date, order_time, listing_amount,
                   status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'fetched', ?, ?)
                """,
                (order_id, sha, filename, merchant, date, order_time, listing_amount, now, now),
            )


def set_parsed(
    order_id: str,
    pdf_amount: float | None,
    merchant: str | None,
    date: str | None,
    currency: str | None,
    receipt_type: str | None = None,
) -> None:
    """Mark a row as parsed. Does not advance from later statuses."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE receipts
            SET pdf_amount    = ?,
                merchant      = COALESCE(?, merchant),
                date          = COALESCE(?, date),
                currency      = COALESCE(?, currency),
                receipt_type  = COALESCE(?, receipt_type),
                status        = CASE WHEN status = 'fetched' THEN 'parsed' ELSE status END,
                updated_at    = ?
            WHERE order_id = ?
            """,
            (pdf_amount, merchant, date, currency, receipt_type, now, order_id),
        )


def set_verified(order_id: str, chosen_amount: float, amount_source: str) -> None:
    """Record the user's (or auto) decision on which amount to use."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE receipts
            SET chosen_amount = ?,
                amount_source = ?,
                status        = 'verified',
                updated_at    = ?
            WHERE order_id = ?
            """,
            (chosen_amount, amount_source, now, order_id),
        )


def set_skipped(order_id: str) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE receipts SET status = 'skipped', updated_at = ? WHERE order_id = ?",
            (now, order_id),
        )


def set_needs_approval(order_id: str, chosen_amount: float, amount_source: str) -> None:
    """Mark a receipt as over the auto-process threshold. The ledger still
    records what amount triggered the flag (chosen_amount) and which source
    that came from, so a manual reviewer has full context."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE receipts
            SET chosen_amount = ?,
                amount_source = ?,
                status        = 'needs_approval',
                updated_at    = ?
            WHERE order_id = ?
            """,
            (chosen_amount, amount_source, now, order_id),
        )


def set_drafted(order_id: str, draft_url: str | None = None, new_filename: str | None = None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE receipts
            SET status     = 'drafted',
                draft_url  = COALESCE(?, draft_url),
                filename   = COALESCE(?, filename),
                updated_at = ?
            WHERE order_id = ?
            """,
            (draft_url, new_filename, now, order_id),
        )


def set_filename(order_id: str, new_filename: str) -> None:
    """Used after rename to keep the ledger row in sync with disk."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE receipts SET filename = ?, updated_at = ? WHERE order_id = ?",
            (new_filename, now, order_id),
        )


# --- receipts: lookups ----------------------------------------------------

def get_by_order_id(order_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE order_id = ?", (order_id,)
        ).fetchone()
    return dict(row) if row else None


def get_by_sha(sha: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE sha256 = ?", (sha,)
        ).fetchone()
    return dict(row) if row else None


def get_by_filename(filename: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE filename = ?", (filename,)
        ).fetchone()
    return dict(row) if row else None


def list_by_status(*statuses: str) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM receipts WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def known_order_ids() -> set[str]:
    with _conn() as conn:
        return {row[0] for row in conn.execute("SELECT order_id FROM receipts")}


def known_sha_set() -> set[str]:
    with _conn() as conn:
        return {row[0] for row in conn.execute(
            "SELECT sha256 FROM receipts WHERE sha256 IS NOT NULL"
        )}


# --- legacy compat (kept so older call sites don't break during phase rollout)

def already_drafted(sha: str) -> bool:
    row = get_by_sha(sha)
    return bool(row) and row.get("status") == "drafted"


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
    """Legacy single-shot writer kept for the existing upload flow until
    Phase 6 rewires it. Synthesizes an order_id from the filename when
    possible, else from the sha."""
    # Try to pull an ORD token out of the filename.
    order_id = _order_id_from_filename(filename) or f"sha_{sha[:16]}"
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT order_id FROM receipts WHERE order_id = ?", (order_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE receipts
                SET sha256        = ?,
                    filename      = ?,
                    merchant      = ?,
                    date          = ?,
                    chosen_amount = COALESCE(?, chosen_amount),
                    currency      = ?,
                    status        = ?,
                    draft_url     = COALESCE(?, draft_url),
                    updated_at    = ?
                WHERE order_id = ?
                """,
                (sha, filename, merchant, date, amount, currency, status, draft_url, now, order_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO receipts
                  (order_id, sha256, filename, merchant, date, currency,
                   chosen_amount, status, draft_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, sha, filename, merchant, date, currency, amount, status, draft_url, now, now),
            )


# --- reconcile ------------------------------------------------------------

def _order_id_from_filename(name: str) -> str | None:
    """Pull ORD<digits> out of a filename like ForwardInvoice_ORD123.pdf or
    Blinkit_ORD123_2026-05-14_1430_1006.pdf. Returns None if not found."""
    stem = Path(name).stem
    if "ORD" not in stem:
        return None
    token = stem[stem.index("ORD"):].split("_")[0]
    return token if token.startswith("ORD") and token[3:].isdigit() else None


def reconcile_inbox(verbose: bool = False) -> dict:
    """Walk inbox/ + processed/, ensure every PDF has a ledger row, and flag
    rows whose file is missing from disk. Returns a summary dict for the
    caller to print/log.

    Files without a recoverable order_id are added with a synthetic
    'manual_<sha[:12]>' id so the ledger still tracks them.

    This is cheap and idempotent — safe to run at the start of any command.
    """
    summary = {
        "added": 0,
        "already_tracked": 0,
        "missing_files": 0,  # ledger rows whose file is gone
        "added_files": [],
        "missing_rows": [],
    }
    INBOX.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)

    # Build a quick lookup of every file on disk.
    on_disk: dict[str, Path] = {}
    for d in (INBOX, PROCESSED):
        for p in d.glob("*.pdf"):
            on_disk[p.name] = p

    known_ids = known_order_ids()

    # 1. Files on disk that aren't in the ledger.
    for name, path in on_disk.items():
        order_id = _order_id_from_filename(name)
        if order_id and order_id in known_ids:
            summary["already_tracked"] += 1
            continue
        try:
            sha = hash_file(path)
        except Exception:
            sha = None
        # If we already have a row with this sha, update it instead of adding.
        if sha:
            existing = get_by_sha(sha)
            if existing:
                # File present on disk under (maybe) a different name. Sync name.
                set_filename(existing["order_id"], name)
                summary["already_tracked"] += 1
                continue
        if not order_id:
            order_id = f"manual_{(sha or 'unknown')[:12]}"
        if order_id in known_ids:
            summary["already_tracked"] += 1
            continue
        # Adopt the orphan file as a 'parsed' row with no listing data.
        # Caller may want to parse it; we don't do that here to stay cheap.
        upsert_fetched(
            order_id=order_id,
            sha=sha,
            filename=name,
            merchant="Blinkit" if "ORD" in name else "Unknown",
            listing_amount=None,
        )
        summary["added"] += 1
        summary["added_files"].append(name)
        if verbose:
            print(f"  reconcile: adopted {name} as {order_id}")

    # 2. Ledger rows whose file is gone from both inbox and processed.
    with _conn() as conn:
        rows = conn.execute(
            "SELECT order_id, filename, status FROM receipts WHERE filename IS NOT NULL"
        ).fetchall()
    for row in rows:
        fn = row["filename"]
        if fn and fn not in on_disk:
            # 'drafted' rows are expected to have their file moved to processed,
            # which IS scanned, so missing here means truly gone.
            summary["missing_files"] += 1
            summary["missing_rows"].append({"order_id": row["order_id"], "filename": fn, "status": row["status"]})
            if verbose:
                print(f"  reconcile: orphan ledger row {row['order_id']} -> {fn} (file missing)")

    return summary
