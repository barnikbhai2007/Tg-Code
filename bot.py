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
from telegram.error import BadRequest
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


async def safe_edit_message_text(query, text, **kwargs):
    """
    Wraps query.edit_message_text() to swallow Telegram's harmless
    "Message is not modified" error.

    This happens when the new text+buttons are IDENTICAL to what's
    already on screen — e.g. tapping "Get Code" twice in a row when
    there's still no new code, or navigating back to a menu that looks
    the same as before. Telegram correctly refuses the no-op edit, but
    without this it looks like a crash. Any OTHER BadRequest (a genuine
    problem) still propagates normally instead of being hidden.
    """
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass  # harmless — the screen already shows this exact content
        else:
            raise


# ---------- Menu button layouts ----------
# Centralized here so every screen builds its buttons the same way,
# rather than each handler hand-rolling its own keyboard.

def main_menu_keyboard(subscription_active: bool) -> InlineKeyboardMarkup:
    if subscription_active:
        # One button per configured code source (Website 1, Website 2, ...)
        rows = [
            [InlineKeyboardButton(f"🔑 {source['name']}", callback_data=f"menu:getcode:{i}")]
            for i, source in enumerate(config.CODE_SOURCES)
        ]
        rows.append([InlineKeyboardButton("📊 My Status", callback_data="menu:status")])
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


async def deliver_code(user_id: int, source_index: int) -> tuple[str, str | None]:
    """
    Returns (text, parse_mode). Shared by /getcode command and the
    per-site 'Get Code' buttons — actually fetching from IMAP takes a
    moment, so this is called AFTER an initial 'checking...' message.

    source_index selects WHICH configured site (config.CODE_SOURCES)
    to fetch a code for, since each site's codes are independent.
    """
    if not db.is_subscription_active(user_id):
        return "❌ You don't have an active subscription. Tap below to subscribe.", None

    if source_index < 0 or source_index >= len(config.CODE_SOURCES):
        logger.error(f"Invalid source_index {source_index}, only {len(config.CODE_SOURCES)} configured")
        return "⚠️ That code source isn't configured correctly. Contact the admin.", None

    source = config.CODE_SOURCES[source_index]

    try:
        code, message_id = email_fetcher.fetch_latest_code(sender=source["sender"])
        db.log_code_sent(user_id, message_id)
        return f"🔑 Your {source['name']} code: `{code}`", "Markdown"
    except email_fetcher.NoCodeAvailable:
        return (
            f"No new {source['name']} code found yet. Make sure you've just "
            f"triggered a code there, then try again in a few seconds."
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

    if len(config.CODE_SOURCES) == 1:
        source_index = 0
    elif context.args:
        # Allow /getcode 1 or /getcode website 1 style input by taking
        # the first argument as a 1-based index into CODE_SOURCES.
        try:
            source_index = int(context.args[0]) - 1
        except ValueError:
            source_index = -1
        if source_index < 0 or source_index >= len(config.CODE_SOURCES):
            names = "\n".join(f"{i+1}. {s['name']}" for i, s in enumerate(config.CODE_SOURCES))
            await update.message.reply_text(
                f"Please specify which site, e.g. /getcode 1\n\n{names}"
            )
            return
    else:
        names = "\n".join(f"{i+1}. {s['name']}" for i, s in enumerate(config.CODE_SOURCES))
        await update.message.reply_text(
            f"Multiple sites configured. Please specify one, e.g. /getcode 1\n\n{names}\n\n"
            f"Or just use /start and tap the button for the site you want."
        )
        return

    checking_msg = await update.message.reply_text("🔍 Checking for your code...")
    text, parse_mode = await deliver_code(user.id, source_index)
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
    parts = query.data.split(":")
    action = parts[1]  # e.g. "menu:getcode:0" -> "getcode"

    if action in ("home", "subscribe"):
        # "subscribe" reuses the home/start screen, since that's where
        # the payment instructions live — no separate screen needed.
        await query.answer()
        text, keyboard, parse_mode = build_start_content(user.id)
        await safe_edit_message_text(query, text, reply_markup=keyboard, parse_mode=parse_mode)

    elif action == "status":
        await query.answer()
        text, keyboard, parse_mode = build_status_content(user.id)
        await safe_edit_message_text(query, text, reply_markup=keyboard, parse_mode=parse_mode)

    elif action == "getcode":
        source_index = int(parts[2])
        await query.answer()
        await safe_edit_message_text(query, "🔍 Checking for your code...")
        text, parse_mode = await deliver_code(user.id, source_index)
        await safe_edit_message_text(query, text, reply_markup=back_to_menu_keyboard(), parse_mode=parse_mode)

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


# ---------- Admin: subscriber list & cancellation ----------
# Only usable from ADMIN_CHAT_ID — this is account-management power
# (viewing every subscriber, cutting off their access), not something
# to expose to trusted-approver-token holders the way approve/reject is.

def _is_admin_chat(chat_id) -> bool:
    return str(chat_id) == str(config.ADMIN_CHAT_ID)


def build_subscriber_list_content() -> tuple[str, InlineKeyboardMarkup]:
    subscribers = db.get_all_subscribers()

    if not subscribers:
        return "No subscribers yet.", InlineKeyboardMarkup([])

    lines = ["📋 Subscribers:\n"]
    rows = []
    for row in subscribers:
        expiry = datetime.fromisoformat(row["subscription_expires_at"])
        active = expiry > datetime.now()
        status_icon = "✅" if active else "❌"
        label = f"@{row['username']}" if row["username"] else f"ID {row['telegram_id']}"
        lines.append(f"{status_icon} {label} — until {expiry.strftime('%d %b %Y')}")
        rows.append([InlineKeyboardButton(
            f"{status_icon} {label}",
            callback_data=f"admin:view:{row['telegram_id']}",
        )])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_subscriber_detail_content(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user_row = db.get_user(telegram_id)
    if not user_row:
        return "User not found.", InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to list", callback_data="admin:list")]
        ])

    label = f"@{user_row['username']}" if user_row["username"] else f"ID {telegram_id}"
    expiry = datetime.fromisoformat(user_row["subscription_expires_at"])
    active = expiry > datetime.now()
    status_text = "Active" if active else "Expired"

    text = (
        f"👤 {label}\n"
        f"Telegram ID: {telegram_id}\n"
        f"Status: {status_text}\n"
        f"Expires: {expiry.strftime('%d %b %Y, %I:%M %p')}"
    )

    rows = []
    if active:
        rows.append([InlineKeyboardButton("❌ Cancel Subscription", callback_data=f"admin:cancel:{telegram_id}")])
    rows.append([InlineKeyboardButton("⬅️ Back to list", callback_data="admin:list")])

    return text, InlineKeyboardMarkup(rows)


async def subscribers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_chat(update.effective_chat.id):
        await update.message.reply_text("This command is only available to the admin.")
        return
    text, keyboard = build_subscriber_list_content()
    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not _is_admin_chat(query.message.chat_id):
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "list":
        await query.answer()
        text, keyboard = build_subscriber_list_content()
        await safe_edit_message_text(query, text, reply_markup=keyboard)

    elif action == "view":
        telegram_id = int(parts[2])
        await query.answer()
        text, keyboard = build_subscriber_detail_content(telegram_id)
        await safe_edit_message_text(query, text, reply_markup=keyboard)

    elif action == "cancel":
        telegram_id = int(parts[2])
        db.cancel_subscription(telegram_id)
        await query.answer("Subscription cancelled.")

        # Notify the affected user directly, so they're not left
        # thinking they still have access when they don't.
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text="Your subscription has been cancelled by the admin. "
                     "Contact support if you believe this is a mistake.",
            )
        except Exception:
            logger.exception(f"Could not notify user {telegram_id} of cancellation")

        text, keyboard = build_subscriber_detail_content(telegram_id)
        await safe_edit_message_text(query, text, reply_markup=keyboard)

    else:
        await query.answer("Unknown option.", show_alert=True)


# ---------- Callback router ----------
# Telegram sends ALL button taps through one handler; we dispatch by
# the callback_data prefix so user-menu taps, admin approve/reject
# taps, and admin subscriber-management taps each reach the right
# logic above.

async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("menu:"):
        await handle_menu_callback(update, context)
    elif data.startswith("approve:") or data.startswith("reject:"):
        await handle_approval_callback(update, context)
    elif data.startswith("admin:"):
        await handle_admin_callback(update, context)
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
    app.add_handler(CommandHandler("subscribers", subscribers_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    app.add_handler(CallbackQueryHandler(route_callback))

    return app


if __name__ == "__main__":
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling()
