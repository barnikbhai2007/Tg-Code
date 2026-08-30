r"""
Fetches the latest verification code email via IMAP.

Design notes:
- Only reads mail matching CODE_SENDER_FILTER, so an unrelated email
  landing in the inbox can never accidentally get relayed to a user.
- Marks the email as \Seen once served, so the SAME code is never sent
  to two different people who both hit "Get Code" in quick succession.
  If your site emails one code at a time, the second requester will
  correctly get "no new code yet" until your site sends a fresh one.
"""
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
import re
import logging

import config

logger = logging.getLogger(__name__)


class NoCodeAvailable(Exception):
    pass


def _decode(value) -> str:
    if value is None:
        return ""
    decoded, encoding = decode_header(value)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(encoding or "utf-8", errors="replace")
    return decoded


def extract_code(text: str) -> str | None:
    """
    Pulls the sign-in code out of email body text.

    Matches your site's actual format:
        "Enter this code to sign in
        8233
        Enter the code above on your device to sign in to [App]. This
        code will expire in 15 minutes. ..."

    The code sits on its OWN LINE directly after "Enter this code to
    sign in" — so this looks for a standalone digit line right after
    that phrase, rather than scanning the rest of the email. This is
    deliberately narrower than "first number after the word code",
    because your email's trailing sentence ("expire in 15 minutes")
    also contains a number after the keyword — searching forward
    through the whole email would eventually risk grabbing a number
    from THAT sentence instead of the real code, if the wording ever
    shifts (e.g. a longer expiry value, a ticket number, etc).

    If your site's exact phrasing changes, update SIGN_IN_PHRASE below
    to match the new wording.
    """
    SIGN_IN_PHRASE = r"enter this code to sign in"

    phrase_match = re.search(SIGN_IN_PHRASE, text, re.IGNORECASE)
    if not phrase_match:
        return None

    # Look at only the next ~3 lines after the phrase, and take the
    # first one that's ENTIRELY digits (not just contains digits) —
    # this is what "its own line" means, and excludes sentences like
    # "This code will expire in 15 minutes" which contain a number
    # but aren't just a number.
    remaining_lines = text[phrase_match.end():].strip().splitlines()
    for line in remaining_lines[:3]:
        stripped = line.strip()
        if re.fullmatch(r"\d{4,8}", stripped):
            return stripped

    return None


def fetch_latest_code(sender: str, max_age_minutes: int = 15) -> tuple[str, str]:
    """
    Returns (code, message_id) for the newest unread email FROM THE GIVEN
    SENDER, but ONLY if it actually arrived within the last `max_age_minutes`.

    `sender` is required (not read from a global) so this can be called
    once per configured code source (config.CODE_SOURCES) — each site's
    codes are fetched independently and never cross-matched with another
    site's sender address.

    This matters because IMAP's UNSEEN just means "never opened" —
    an old code email nobody ever read (e.g. from testing weeks ago)
    is still UNSEEN and would otherwise get served as if it were a
    code just triggered right now. Filtering by actual timestamp closes
    that gap. IMAP's SINCE only supports day-level granularity, so the
    precise recency check happens here in Python, not in the IMAP query.

    Raises NoCodeAvailable if there's no matching email within the window.
    """
    conn = imaplib.IMAP4_SSL(config.IMAP_SERVER)
    try:
        conn.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        conn.select("INBOX")

        search_criteria = f'(UNSEEN FROM "{sender}")'

        status, data = conn.search(None, search_criteria)
        if status != "OK" or not data[0]:
            raise NoCodeAvailable("No new code email found.")

        # Newest-first, so we check the most recent candidates first
        # and can stop as soon as we find one inside the time window.
        candidate_ids = data[0].split()[::-1]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)

        for msg_id in candidate_ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            date_header = msg.get("Date")
            if not date_header:
                continue
            sent_at = parsedate_to_datetime(date_header)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)

            if sent_at < cutoff:
                # This candidate is too old. Since we're going
                # newest-first, everything after this is even older —
                # no point checking further.
                break

            message_id = msg.get("Message-ID", msg_id.decode())
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode(errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace")

            code = extract_code(body)
            if not code:
                continue  # matched sender + recency, but no code pattern inside — try next

            # Mark as seen so it's never served twice
            conn.store(msg_id, "+FLAGS", "\\Seen")
            return code, message_id

        raise NoCodeAvailable(
            f"No code email found within the last {max_age_minutes} minutes."
        )
    finally:
        conn.logout()
