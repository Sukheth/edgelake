from __future__ import annotations

import tempfile
from datetime import datetime, date as _Date
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from ..config import INBOX, TELEGRAM_BOT_TOKEN
from ..ledger import hash_file, known_sha_set, set_parsed, upsert_fetched
from .vision import extract_receipt

_SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

_HELP = (
    "Send me a receipt PDF or photo and I'll queue it for Chrome River.\n\n"
    "Supported: PDF, JPEG, PNG.\n"
    "After sending, run `edgelake run --no-fetch` to file it."
)


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP)


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]  # highest resolution
    tg_file = await photo.get_file()
    suffix = ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await tg_file.download_to_drive(tmp_path)
    await _process(update, tmp_path, suffix)


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    fname = doc.file_name or "receipt"
    suffix = Path(fname).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported file type '{suffix}'. Send a PDF, JPEG, or PNG."
        )
        return
    tg_file = await doc.get_file()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await tg_file.download_to_drive(tmp_path)
    await _process(update, tmp_path, suffix)


async def _process(update: Update, tmp_path: Path, suffix: str) -> None:
    status_msg = await update.message.reply_text("Extracting receipt data...")
    try:
        # Dedup by SHA before doing expensive Claude call.
        sha = hash_file(tmp_path)
        if sha in known_sha_set():
            await status_msg.edit_text("Already queued — this receipt is in the ledger.")
            tmp_path.unlink(missing_ok=True)
            return

        try:
            fields = extract_receipt(tmp_path)
        except Exception as exc:
            await status_msg.edit_text(f"Could not read receipt: {exc}")
            tmp_path.unlink(missing_ok=True)
            return

        merchant = fields.get("merchant") or "Unknown"
        raw_date = fields.get("date")
        amount = fields.get("amount")
        currency = fields.get("currency") or "INR"
        confidence = fields.get("confidence") or "low"

        date_obj = _parse_date(raw_date)
        date_str = date_obj.isoformat() if date_obj else None

        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        order_id = f"TG{ts}_{sha[:6]}"

        # Save file into inbox with a stable name.
        INBOX.mkdir(parents=True, exist_ok=True)
        dest = INBOX / f"TG_{order_id}{suffix}"
        tmp_path.rename(dest)

        upsert_fetched(
            order_id=order_id,
            sha=sha,
            filename=dest.name,
            merchant=merchant,
            listing_amount=amount,
            date=date_str,
        )
        if amount is not None and date_str:
            set_parsed(
                order_id=order_id,
                pdf_amount=amount,
                merchant=merchant,
                date=date_str,
                currency=currency,
            )
            ledger_status = "parsed"
        else:
            ledger_status = "fetched (incomplete — run parse-pending)"

        lines = [
            f"Receipt queued ({confidence} confidence)",
            f"  Merchant : {merchant}",
            f"  Date     : {date_str or '—'}",
            f"  Amount   : {currency} {amount:.2f}" if amount else "  Amount   : —",
            f"  Status   : {ledger_status}",
            f"  Order ID : {order_id}",
            "",
            "Run `edgelake run --no-fetch` to file to Chrome River.",
        ]
        await status_msg.edit_text("\n".join(lines))

    except Exception as exc:
        await status_msg.edit_text(f"Unexpected error: {exc}")
        tmp_path.unlink(missing_ok=True)


def _parse_date(raw: str | None) -> _Date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def run_bot(token: str | None = None) -> None:
    tok = token or TELEGRAM_BOT_TOKEN
    if not tok:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN not set. Add it to .env or pass via --token."
        )
    app = Application.builder().token(tok).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, _handle_document))
    app.run_polling(drop_pending_updates=True)
