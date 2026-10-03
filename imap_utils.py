"""
imap_utils.py - OPTIONAL: check your own mailbox for the test email.

If IMAP is not configured the rest of the program works unchanged; you just
inspect the inbox yourself. The password is never printed or logged.
"""
from __future__ import annotations

import email
import imaplib
import logging
import socket
import time
from email.header import decode_header, make_header
from typing import Callable

from config import Settings
from errors import ConfigError, IMAPError

log = logging.getLogger("sft.imap")
_CONNECT_TIMEOUT = 20


def _connect(settings: Settings) -> imaplib.IMAP4_SSL:
    problems = settings.imap_problems()
    if problems:
        raise ConfigError(" ".join(problems), "Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD in .env.")
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=_CONNECT_TIMEOUT)
    except socket.gaierror:
        raise IMAPError(f"IMAP server '{settings.imap_host}' was not found.",
                        "Check IMAP_HOST for typos.") from None
    except (socket.timeout, TimeoutError):
        raise IMAPError(f"Timed out connecting to {settings.imap_host}:{settings.imap_port}.",
                        "Check IMAP_HOST/IMAP_PORT and your firewall or VPN.") from None
    except OSError as exc:
        raise IMAPError(f"Could not connect to {settings.imap_host}:{settings.imap_port} "
                        f"({exc.strerror or exc}).", "Check IMAP_HOST and IMAP_PORT (usually 993).") from None
    try:
        mail.login(settings.imap_user, settings.imap_password)
    except imaplib.IMAP4.error:
        try:
            mail.shutdown()
        except Exception:
            pass
        raise IMAPError(
            "The mail server rejected the IMAP login.",
            "Check IMAP_USER/IMAP_PASSWORD. Gmail and Outlook need an app password "
            "(your normal password will not work).") from None
    status, _ = mail.select(settings.imap_folder, readonly=True)
    if status != "OK":
        mail.logout()
        raise IMAPError(f"IMAP folder '{settings.imap_folder}' could not be opened.",
                        "Check IMAP_FOLDER (Gmail: \"[Gmail]/All Mail\" also catches spam/promotions).")
    return mail


def test_connection(settings: Settings) -> int:
    """Log in and return the number of messages in the folder."""
    mail = _connect(settings)
    try:
        _, data = mail.search(None, "ALL")
        return len(data[0].split()) if data and data[0] else 0
    finally:
        mail.logout()


def snapshot_ids(settings: Settings) -> set[bytes]:
    """IDs currently in the folder (taken BEFORE the test, so old mail is ignored)."""
    mail = _connect(settings)
    try:
        _, data = mail.search(None, "ALL")
        return set(data[0].split()) if data and data[0] else set()
    finally:
        mail.logout()


def matches_hints(from_header: str, subject: str, sender_hint: str, subject_hint: str) -> bool:
    """True when the message matches the optional sender/subject substrings."""
    if sender_hint and sender_hint.lower() not in (from_header or "").lower():
        return False
    if subject_hint and subject_hint.lower() not in (subject or "").lower():
        return False
    return True


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def wait_for_new_email(settings: Settings, known: set[bytes], sender_hint: str = "",
                       subject_hint: str = "", poll_interval: int = 10,
                       on_wait: Callable[[int], None] | None = None) -> dict | None:
    """Poll until a new matching message arrives. Returns {from, subject} or None."""
    deadline = time.monotonic() + settings.imap_timeout
    while True:
        mail = _connect(settings)
        try:
            _, data = mail.search(None, "ALL")
            current = set(data[0].split()) if data and data[0] else set()
            for message_id in sorted(current - known):
                _, parts = mail.fetch(message_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                if not parts or not parts[0] or not isinstance(parts[0], tuple):
                    continue
                message = email.message_from_bytes(parts[0][1])
                sender, subject = _decode(message.get("From", "")), _decode(message.get("Subject", ""))
                if matches_hints(sender, subject, sender_hint, subject_hint):
                    return {"from": sender, "subject": subject}
        finally:
            try:
                mail.logout()
            except Exception:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if on_wait:
            on_wait(int(remaining))
        time.sleep(min(poll_interval, remaining))
