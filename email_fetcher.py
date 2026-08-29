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


def fetch_latest_code() -> tuple[str, str]:
    """
    Returns (code, message_id) for the newest unread matching email.
    Raises NoCodeAvailable if there's nothing new.
    """
    conn = imaplib.IMAP4_SSL(config.IMAP_SERVER)
    try:
        conn.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        conn.select("INBOX")

        search_criteria = "UNSEEN"
        if config.CODE_SENDER_FILTER:
            search_criteria = f'(UNSEEN FROM "{config.CODE_SENDER_FILTER}")'

        logger.info(f"[DEBUG] IMAP search criteria: {search_criteria}")

        status, data = conn.search(None, search_criteria)
        if status != "OK" or not data[0]:
            logger.info(f"[DEBUG] Search returned no matches. status={status} data={data}")
            raise NoCodeAvailable("No new code email found.")

        matched_ids = data[0].split()
        logger.info(f"[DEBUG] Search matched {len(matched_ids)} email(s): {matched_ids}")

        # Newest email = last in the returned ID list
        latest_id = matched_ids[-1]

        status, msg_data = conn.fetch(latest_id, "(RFC822)")
        if status != "OK":
            raise NoCodeAvailable("Failed to fetch email content.")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        message_id = msg.get("Message-ID", latest_id.decode())

        logger.info(f"[DEBUG] Fetched email - From: {_decode(msg.get('From'))!r} Subject: {_decode(msg.get('Subject'))!r} Date: {msg.get('Date')!r}")

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

        logger.info(f"[DEBUG] Email body (first 300 chars): {body[:300]!r}")

        code = extract_code(body)
        if not code:
            logger.info(f"[DEBUG] extract_code() found no match in this body.")
            raise NoCodeAvailable("Email found but no code pattern matched inside it.")

        logger.info(f"[DEBUG] Extracted code: {code!r}")

        # Mark as seen so it's never served twice
        conn.store(latest_id, "+FLAGS", "\\Seen")

        return code, message_id
    finally:
        conn.logout()
