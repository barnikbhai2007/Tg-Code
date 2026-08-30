"""
SQLite database layer.

Tables:
  users            - one row per Telegram user who has interacted with the bot
  pending_requests - screenshot submissions awaiting admin approval
"""
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager

import config


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                subscription_expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                screenshot_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at TEXT,
                resolved_by TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS code_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                email_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


def upsert_user(telegram_id: int, username: str | None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (telegram_id, username)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
        """, (telegram_id, username))


def get_user(telegram_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def is_subscription_active(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    if not user or not user["subscription_expires_at"]:
        return False
    expires = datetime.fromisoformat(user["subscription_expires_at"])
    return expires > datetime.now()


def extend_subscription(telegram_id: int, days: int) -> datetime:
    """
    Extends from the LATER of (now, current expiry) so early renewals
    stack on top of remaining time instead of wasting it.
    """
    user = get_user(telegram_id)
    now = datetime.now()
    base = now
    if user and user["subscription_expires_at"]:
        current_expiry = datetime.fromisoformat(user["subscription_expires_at"])
        if current_expiry > now:
            base = current_expiry
    new_expiry = base + timedelta(days=days)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET subscription_expires_at = ? WHERE telegram_id = ?",
            (new_expiry.isoformat(), telegram_id),
        )
    return new_expiry


def create_pending_request(telegram_id: int, username: str | None, screenshot_file_id: str) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO pending_requests (telegram_id, username, screenshot_file_id)
            VALUES (?, ?, ?)
        """, (telegram_id, username, screenshot_file_id))
        return cur.lastrowid


def get_pending_request(request_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM pending_requests WHERE id = ?", (request_id,)
        ).fetchone()


def resolve_pending_request(request_id: int, status: str, resolved_by: str):
    with get_db() as conn:
        conn.execute("""
            UPDATE pending_requests
            SET status = ?, resolved_at = datetime('now'), resolved_by = ?
            WHERE id = ?
        """, (status, resolved_by, request_id))


def has_unresolved_request(telegram_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_requests WHERE telegram_id = ? AND status = 'pending'",
            (telegram_id,),
        ).fetchone()
        return row is not None


def log_code_sent(telegram_id: int, email_message_id: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO code_log (telegram_id, email_message_id) VALUES (?, ?)",
            (telegram_id, email_message_id),
        )


def get_expiring_soon(hours: int = 24) -> list[sqlite3.Row]:
    """Users whose subscription expires within the given window — for renewal reminders."""
    cutoff = datetime.now() + timedelta(hours=hours)
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM users
            WHERE subscription_expires_at IS NOT NULL
              AND subscription_expires_at > datetime('now')
              AND subscription_expires_at <= ?
        """, (cutoff.isoformat(),)).fetchall()


def get_all_subscribers() -> list[sqlite3.Row]:
    """
    Every user who has EVER had a subscription (active or expired),
    newest activity first. Users who only ever hit /start without
    subscribing are excluded, since they're not really "subscribers".
    """
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM users
            WHERE subscription_expires_at IS NOT NULL
            ORDER BY subscription_expires_at DESC
        """).fetchall()


def cancel_subscription(telegram_id: int):
    """
    Immediately expires a user's subscription by setting expiry to now.
    Kept as "expire now" rather than deleting the row, so the user's
    history (they WERE a subscriber) isn't lost.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET subscription_expires_at = datetime('now') WHERE telegram_id = ?",
            (telegram_id,),
        )
