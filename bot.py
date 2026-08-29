"""
Main Telegram bot.

User flow:
  /start          -> welcome + subscribe instructions, with menu buttons
  (sends photo)    -> treated as payment screenshot, forwarded to admin
  /getcode         -> if subscription active, fetch + send latest code
  /status          -> show subscription expiry
  (all of the above are also reachable via inline buttons, which call
   the same underlying functions as the slash commands do)

Admin flow (in the ADMIN_CHAT_ID chat, or by any trusted holder of the token):
  Tap "Approve" / "Reject" inline buttons on forwarded screenshot, OR
  /approve <request_id> <token>
  /reject <request_id> <token>
"""
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import config
import database as db
import email_fetcher

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------- Menu button layouts ----------
# Centralized here so every screen builds its buttons the same way,
# rather than each handler hand-rolling its own keyboard.

def main_menu_keyboard(subscription_active: bool) -> InlineKeyboardMarkup:
    if subscription_active:
        rows = [
            [InlineKeyboardButton("🔑 Get Code", callback_data="menu:getcode")],
            [InlineKeyboardButton("📊 My Status", callback_data="menu:status")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("💳 Subscribe", callback_data="menu:subscribe")],
            [InlineKeyboardButton("📊 My Status", callback_data="menu:status")],
        ]
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]
    ])


# ---------- Shared content builders ----------
# These return (text, keyboard, parse_mode) and know nothing about
# whether they were triggered by a slash command or a button tap.
# Both entry points below call these, so the actual bot behavior only
# ever lives in one place.

def build_start_content(user_id: int) -> tuple[str, InlineKeyboardMarkup, str | None]:
    if db.is_subscription_active(user_id):
        expiry = db.get_user(user_id)["subscription_expires_at"]
        expiry_str = datetime.fromisoformat(expiry).strftime("%d %b %Y, %I:%M %p")
        text = (
            f"Welcome back! Your subscription is active until {expiry_str}.\n\n"
            f"Tap below to get your code or check your status."
        )
        return text, main_menu_keyboard(subscription_active=True), None

    text = (
        f"👋 Welcome!\n\n"
        f"Subscription: ₹{config.SUBSCRIPTION_PRICE_INR} for {config.SUBSCRIPTION_DAYS} days\n\n"
        f"To subscribe:\n"
        f"1️⃣ Pay ₹{config.SUBSCRIPTION_PRICE_INR} to this UPI ID:\n"
        f"`{config.UPI_ID}`\n\n"
        f"2️⃣ Send me a screenshot of the payment confirmation right here in this chat.\n\n"
        f"3️⃣ Once verified, you'll get access automatically."
    )
    return text, main_menu_keyboard(subscription_active=False), "Markdown"


def build_status_content(user_id: int) -> tuple[str, InlineKeyboardMarkup, str | None]:
    user_row = db.get_user(user_id)

    if not user_row or not user_row["subscription_expires_at"]:
        text = "You don't have a subscription yet. Tap below to subscribe."
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Subscribe", callback_data="menu:subscribe")]
        ]), None

    expiry = datetime.fromisoformat(user_row["subscription_expires_at"])
    if expiry > datetime.now():
        remaining = expiry - datetime.now()
        text = (
            f"✅ Active until {expiry.strftime('%d %b %Y, %I:%M %p')} "
            f"({remaining.days} days remaining)."
        )
    else:
        text = (
            f"❌ Your subscription expired on {expiry.strftime('%d %b %Y')}.\n"
            f"Tap below to renew."
        )
    return text, back_to_menu_keyboard(), None


async def deliver_code(user_id: int) -> tuple[str, str | None]:
    """
    Returns (text, parse_mode). Shared by /getcode command and the
    'Get Code' button — actually fetching from IMAP takes a moment,
    so this is called AFTER an initial 'checking...' message is shown.
    """
    if not db.is_subscription_active(user_id):
        return "❌ You don't have an active subscription. Tap below to subscribe.", None

    try:
        code, message_id = email_fetcher.fetch_latest_code()
        db.log_code_sent(user_id, message_id)
        return f"🔑 Your code: `{code}`", "Markdown"
    except email_fetcher.NoCodeAvailable:
        return (
            "No new code found yet. Make sure you've just triggered a code "
            "on the website, then try again in a few seconds."
        ), None
    except Exception:
        logger.exception("Error fetching code")
        return "⚠️ Something went wrong fetching your code. Please try again shortly.", None


# ---------- User-facing slash commands (thin wrappers around the builders above) ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username)
    text, keyboard, parse_mode = build_start_content(user.id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text, keyboard, parse_mode = build_status_content(user.id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)


async def getcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    checking_msg = await update.message.reply_text("🔍 Checking for your code...")
    text, parse_mode = await deliver_code(user.id)
    await checking_msg.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode=parse_mode)


async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    if db.has_unresolved_request(user.id):
        await update.message.reply_text(
            "You already have a screenshot pending review. Please wait for it to be approved or rejected."
        )
        return

    photo = update.message.photo[-1]  # highest resolution
    request_id = db.create_pending_request(user.id, user.username, photo.file_id)

    await update.message.reply_text(
        "📸 Screenshot received! It's been sent for verification. "
        "You'll be notified here once it's approved.",
        reply_markup=back_to_menu_keyboard(),
    )

    # Forward to admin with inline approve/reject buttons
    caption = (
        f"💰 New payment claim\n"
        f"Request ID: {request_id}\n"
        f"From: @{user.username or 'no_username'} (ID: {user.id})"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{request_id}"),
        ]
    ])
    await context.bot.send_photo(
        chat_id=config.ADMIN_CHAT_ID,
        photo=photo.file_id,
        caption=caption,
        reply_markup=keyboard,
    )


# ---------- User menu button taps ----------

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    action = query.data.split(":", 1)[1]  # e.g. "menu:getcode" -> "getcode"

    if action in ("home", "subscribe"):
        # "subscribe" reuses the home/start screen, since that's where
        # the payment instructions live — no separate screen needed.
        await query.answer()
        text, keyboard, parse_mode = build_start_content(user.id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=parse_mode)

    elif action == "status":
        await query.answer()
        text, keyboard, parse_mode = build_status_content(user.id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=parse_mode)

    elif action == "getcode":
        await query.answer()
        await query.edit_message_text("🔍 Checking for your code...")
        text, parse_mode = await deliver_code(user.id)
        await query.edit_message_text(text, reply_markup=back_to_menu_keyboard(), parse_mode=parse_mode)

    else:
        await query.answer("Unknown option.", show_alert=True)


# ---------- Admin approval: inline buttons ----------

async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, request_id_str = query.data.split(":")
    request_id = int(request_id_str)

    # Only the configured admin chat can approve via button.
    # (Trusted approvers without access to this chat use /approve with the token instead.)
    if str(query.message.chat_id) != str(config.ADMIN_CHAT_ID):
        await query.answer("Not authorized.", show_alert=True)
        return

    await _resolve_request(request_id, action, resolved_by="admin_button", context=context, query=query)


# ---------- Callback router ----------
# Telegram sends ALL button taps through one handler; we dispatch by
# the callback_data prefix so user-menu taps and admin approve/reject
# taps each reach the right logic above.

async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("menu:"):
        await handle_menu_callback(update, context)
    elif data.startswith("approve:") or data.startswith("reject:"):
        await handle_approval_callback(update, context)
    else:
        await update.callback_query.answer("Unrecognized action.", show_alert=True)


# ---------- Admin approval: token command (for trusted non-primary approvers) ----------

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_token_command(update, context, action="approve")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_token_command(update, context, action="reject")


async def _handle_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(f"Usage: /{action} <request_id> <token>")
        return

    request_id_str, token = args
    if token != config.ADMIN_APPROVAL_TOKEN:
        await update.message.reply_text("❌ Invalid token.")
        logger.warning(f"Failed approval token attempt by {update.effective_user.id}")
        return

    try:
        request_id = int(request_id_str)
    except ValueError:
        await update.message.reply_text("Invalid request ID.")
        return

    await _resolve_request(
        request_id, action,
        resolved_by=f"token:{update.effective_user.id}",
        context=context,
    )
    await update.message.reply_text(f"Request {request_id} {action}d.")


# ---------- Shared resolution logic ----------

async def _resolve_request(request_id: int, action: str, resolved_by: str, context: ContextTypes.DEFAULT_TYPE, query=None):
    req = db.get_pending_request(request_id)
    if not req:
        msg = f"Request {request_id} not found."
        if query:
            await query.edit_message_caption(caption=msg)
        return

    if req["status"] != "pending":
        msg = f"Request {request_id} was already {req['status']}."
        if query:
            await query.edit_message_caption(caption=msg)
        return

    status = "approved" if action == "approve" else "rejected"
    db.resolve_pending_request(request_id, status, resolved_by)

    telegram_id = req["telegram_id"]

    if status == "approved":
        new_expiry = db.extend_subscription(telegram_id, config.SUBSCRIPTION_DAYS)
        expiry_str = new_expiry.strftime("%d %b %Y, %I:%M %p")
        await context.bot.send_message(
            chat_id=telegram_id,
            text=f"✅ Payment approved! Your subscription is active until {expiry_str}.\n\nUse /getcode to fetch your code.",
        )
        result_caption = f"✅ Request {request_id} APPROVED by {resolved_by}\nActive until {expiry_str}"
    else:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="❌ Your payment screenshot was not approved. Please contact support if you believe this is an error.",
        )
        result_caption = f"❌ Request {request_id} REJECTED by {resolved_by}"

    if query:
        await query.edit_message_caption(caption=result_caption)


# ---------- App setup ----------

def build_app() -> Application:
    config.validate_config()
    db.init_db()

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("getcode", getcode))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    app.add_handler(CallbackQueryHandler(route_callback))

    return app


if __name__ == "__main__":
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling()
