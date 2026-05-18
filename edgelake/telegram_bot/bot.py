from __future__ import annotations

import asyncio
import ssl
import tempfile

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
from datetime import datetime, date as _Date
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from ..config import FAILED, INBOX, TELEGRAM_BOT_TOKEN
from ..ledger import hash_file, known_sha_set, set_failed, set_parsed, upsert_fetched
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
        sha = hash_file(tmp_path)
        if sha in known_sha_set():
            await status_msg.edit_text("Already queued — this receipt is in the ledger.")
            tmp_path.unlink(missing_ok=True)
            return

        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        order_id = f"TG{ts}_{sha[:6]}"

        # Try Gemini up to 3 times before giving up.
        fields = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                fields = extract_receipt(tmp_path)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2)

        if fields is None:
            FAILED.mkdir(parents=True, exist_ok=True)
            dest = FAILED / f"TG_{order_id}{suffix}"
            tmp_path.rename(dest)
            upsert_fetched(order_id=order_id, sha=sha, filename=dest.name,
                           merchant="Unknown", listing_amount=None)
            set_failed(order_id)
            await status_msg.edit_text(
                f"Could not read receipt after 3 attempts.\n"
                f"Error: {last_exc}\n"
                f"File saved to receipts/failed/ — check it manually."
            )
            return

        merchant = fields.get("merchant") or "Unknown"
        raw_date = fields.get("date")
        amount = fields.get("amount")
        currency = fields.get("currency") or "INR"
        receipt_type = fields.get("receipt_type") or "snacks"
        confidence = fields.get("confidence") or "low"

        date_obj = _parse_date(raw_date)
        date_str = date_obj.isoformat() if date_obj else None

        # If Gemini couldn't extract the critical fields, move to failed/.
        missing = [f for f, v in [("amount", amount), ("date", date_str)] if not v]
        if missing:
            FAILED.mkdir(parents=True, exist_ok=True)
            dest = FAILED / f"TG_{order_id}{suffix}"
            tmp_path.rename(dest)
            upsert_fetched(order_id=order_id, sha=sha, filename=dest.name,
                           merchant=merchant, listing_amount=amount)
            set_failed(order_id)
            await status_msg.edit_text(
                f"Gemini could not extract: {', '.join(missing)}\n"
                f"  Merchant   : {merchant}\n"
                f"  Confidence : {confidence}\n"
                f"File saved to receipts/failed/ — check it manually."
            )
            return

        # All fields present — queue normally.
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
        set_parsed(
            order_id=order_id,
            pdf_amount=amount,
            merchant=merchant,
            date=date_str,
            currency=currency,
            receipt_type=receipt_type,
        )

        lines = [
            f"Receipt queued ({confidence} confidence)",
            f"  Merchant : {merchant}",
            f"  Date     : {date_str}",
            f"  Amount   : {currency} {amount:.2f}",
            f"  Type     : {receipt_type}",
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
    async def _on_startup(application: Application) -> None:
        me = await application.bot.get_me()
        print(f"Bot ready: @{me.username} (id={me.id}) — send a receipt to start.")

    app = Application.builder().token(tok).post_init(_on_startup).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, _handle_document))
    app.run_polling(drop_pending_updates=False)
