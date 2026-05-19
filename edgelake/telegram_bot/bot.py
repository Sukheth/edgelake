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
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from ..config import FAILED, INBOX, TELEGRAM_BOT_TOKEN
from ..ledger import hash_file, known_sha_set, set_failed, set_parsed, upsert_fetched
from .vision import extract_receipt

_SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

_HELP = (
    "Send me a receipt PDF or photo and I'll queue it for Chrome River.\n\n"
    "Supported: PDF, JPEG, PNG.\n"
    "After sending I'll ask if you want to override the amount or date.\n"
    "Run `edgelake run --no-fetch` to file queued receipts."
)

# Conversation states
_OVERRIDE_CHOICE, _OVERRIDE_AMOUNT, _OVERRIDE_DATE = range(3)


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP)


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1]  # highest resolution
    tg_file = await photo.get_file()
    suffix = ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await tg_file.download_to_drive(tmp_path)
    return await _start_receipt_flow(update, context, tmp_path, suffix)


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    fname = doc.file_name or "receipt"
    suffix = Path(fname).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported file type '{suffix}'. Send a PDF, JPEG, or PNG."
        )
        return ConversationHandler.END
    tg_file = await doc.get_file()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await tg_file.download_to_drive(tmp_path)
    return await _start_receipt_flow(update, context, tmp_path, suffix)


async def _start_receipt_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tmp_path: Path, suffix: str
) -> int:
    """Dedup check and ask the user about overrides. Returns the next state."""
    # Clean up any stale temp file from a previous abandoned conversation.
    old = context.user_data.get("tmp_path")
    if old:
        Path(old).unlink(missing_ok=True)

    sha = hash_file(tmp_path)
    if sha in known_sha_set():
        await update.message.reply_text("Already queued — this receipt is in the ledger.")
        tmp_path.unlink(missing_ok=True)
        return ConversationHandler.END

    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    order_id = f"TG{ts}_{sha[:6]}"

    context.user_data.update({
        "tmp_path": str(tmp_path),
        "suffix": suffix,
        "sha": sha,
        "order_id": order_id,
        "override_amount": None,
        "override_date": None,
    })

    await update.message.reply_text(
        "Receipt received!\n\n"
        "Do you want to manually set the amount or date?\n"
        "Reply *Y* to override, or *N* to let Gemini handle it.",
        parse_mode="Markdown",
    )
    return _OVERRIDE_CHOICE


async def _got_override_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if text in ("y", "yes"):
        await update.message.reply_text("Enter the amount (numbers only, e.g. *450.00*):", parse_mode="Markdown")
        return _OVERRIDE_AMOUNT
    if text in ("n", "no"):
        await _finalize(update, context)
        return ConversationHandler.END
    await update.message.reply_text("Please reply *Y* or *N*.", parse_mode="Markdown")
    return _OVERRIDE_CHOICE


async def _got_override_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Not a valid amount. Enter a positive number (e.g. *450.00*):", parse_mode="Markdown")
        return _OVERRIDE_AMOUNT
    context.user_data["override_amount"] = amount
    await update.message.reply_text(
        "Got it! Now enter the date (*DD-MM-YYYY*, e.g. *14-05-2026*):",
        parse_mode="Markdown",
    )
    return _OVERRIDE_DATE


async def _got_override_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        d = datetime.strptime(raw, "%d-%m-%Y").date()
        context.user_data["override_date"] = d.isoformat()
    except ValueError:
        await update.message.reply_text(
            "Invalid date. Use *DD-MM-YYYY*, e.g. *14-05-2026*:",
            parse_mode="Markdown",
        )
        return _OVERRIDE_DATE
    await _finalize(update, context)
    return ConversationHandler.END


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run Gemini, apply any overrides, and write to ledger."""
    tmp_path = Path(context.user_data["tmp_path"])
    suffix = context.user_data["suffix"]
    sha = context.user_data["sha"]
    order_id = context.user_data["order_id"]
    override_amount: float | None = context.user_data.get("override_amount")
    override_date: str | None = context.user_data.get("override_date")

    status_msg = await update.message.reply_text("Extracting receipt data...")
    try:
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
        gemini_amount = fields.get("amount")
        currency = fields.get("currency") or "INR"
        receipt_type = fields.get("receipt_type") or "snacks"
        confidence = fields.get("confidence") or "low"

        # Apply overrides: user-supplied values win over Gemini.
        amount = override_amount if override_amount is not None else gemini_amount
        if override_date:
            date_obj = _parse_date(override_date)
        else:
            date_obj = _parse_date(raw_date)
        date_str = date_obj.isoformat() if date_obj else None

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

        overridden = []
        if override_amount is not None:
            overridden.append("amount")
        if override_date is not None:
            overridden.append("date")

        lines = [
            f"Receipt queued ({confidence} confidence)",
            f"  Merchant : {merchant}",
            f"  Date     : {date_str}",
            f"  Amount   : {currency} {amount:.2f}",
            f"  Type     : {receipt_type}",
            f"  Order ID : {order_id}",
        ]
        if overridden:
            lines.append(f"  Overridden: {', '.join(overridden)}")
        lines += ["", "Run `edgelake run --no-fetch` to file to Chrome River."]
        await status_msg.edit_text("\n".join(lines))

    except Exception as exc:
        await status_msg.edit_text(f"Unexpected error: {exc}")
        tmp_path.unlink(missing_ok=True)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    old = context.user_data.get("tmp_path")
    if old:
        Path(old).unlink(missing_ok=True)
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send a receipt any time.")
    return ConversationHandler.END


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

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, _handle_photo),
            MessageHandler(filters.Document.ALL, _handle_document),
        ],
        states={
            _OVERRIDE_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_override_choice),
            ],
            _OVERRIDE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_override_amount),
            ],
            _OVERRIDE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_override_date),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        allow_reentry=True,   # new receipt always restarts the flow
        conversation_timeout=300,  # 5 min to complete, then state discarded
    )
    app.add_handler(conv)
    app.run_polling(drop_pending_updates=False)
