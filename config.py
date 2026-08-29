"""
Configuration loaded from environment variables.
Never hardcode secrets directly in code — set these in your .env file
on the VPS instead (see .env.example).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # your personal Telegram numeric ID

# --- Admin approval token ---
# A shared secret that lets a trusted person approve payments without
# being the primary admin. Treat this like a password: only share it
# with people you trust to verify screenshots correctly.
ADMIN_APPROVAL_TOKEN = os.getenv("ADMIN_APPROVAL_TOKEN")

# --- Email (Gmail via IMAP) ---
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")  # 16-char Google App Password, NOT your real password
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")

# Only pull codes from mail matching this sender, so the bot can't be
# tricked into relaying an unrelated email that happens to land in the inbox.
CODE_SENDER_FILTER = os.getenv("CODE_SENDER_FILTER")  # e.g. "noreply@yoursite.com"

# --- Subscription ---
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
SUBSCRIPTION_PRICE_INR = os.getenv("SUBSCRIPTION_PRICE_INR", "499")
UPI_ID = os.getenv("UPI_ID")  # your UPI ID shown to users, e.g. "yourname@okhdfcbank"

# --- Database ---
DB_PATH = os.getenv("DB_PATH", "subscriptions.db")

# --- Validation on startup ---
def validate_config():
    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_CHAT_ID": ADMIN_CHAT_ID,
        "ADMIN_APPROVAL_TOKEN": ADMIN_APPROVAL_TOKEN,
        "EMAIL_ADDRESS": EMAIL_ADDRESS,
        "EMAIL_APP_PASSWORD": EMAIL_APP_PASSWORD,
        "UPI_ID": UPI_ID,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    if len(ADMIN_APPROVAL_TOKEN) < 12:
        raise RuntimeError(
            "ADMIN_APPROVAL_TOKEN is too short. Use a long random string "
            "(e.g. generate one with: python3 -c \"import secrets; print(secrets.token_urlsafe(24))\")"
        )
