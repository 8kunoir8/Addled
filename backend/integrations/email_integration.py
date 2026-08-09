"""
Email integration — IMAP/SMTP for reading and sending emails.
"""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger("addled.integrations.email")


class EmailIntegration:
    """IMAP/SMTP email client."""

    def __init__(self):
        self._imap: imaplib.IMAP4_SSL | None = None
        self._smtp: smtplib.SMTP | None = None
        self._config_loaded = False

    def _load_config(self) -> dict:
        try:
            from backend.config import config
            return {
                "imap_server": config.get("integrations", "imap_server", default=""),
                "imap_port": config.get("integrations", "imap_port", default=993),
                "smtp_server": config.get("integrations", "smtp_server", default=""),
                "smtp_port": config.get("integrations", "smtp_port", default=587),
                "email": config.get("integrations", "email_address", default=""),
                "password": config.get("integrations", "email_password", default=""),
            }
        except Exception:
            return {}

    def connect(self) -> bool:
        """Connect to IMAP server."""
        cfg = self._load_config()
        if not cfg["imap_server"] or not cfg["email"]:
            return False
        try:
            self._imap = imaplib.IMAP4_SSL(cfg["imap_server"], int(cfg["imap_port"]))
            self._imap.login(cfg["email"], cfg["password"])
            self._config_loaded = True
            return True
        except Exception as e:
            log.error("IMAP connect failed: %s", e)
            return False

    def disconnect(self):
        if self._imap:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def fetch_unread(self, limit: int = 10) -> list[dict]:
        """Fetch unread emails from inbox."""
        if not self._imap and not self.connect():
            return []

        try:
            self._imap.select("INBOX")
            status, messages = self._imap.search(None, "UNSEEN")
            if status != "OK":
                return []

            msg_ids = messages[0].split()
            if not msg_ids:
                return []

            results = []
            for num in reversed(msg_ids[-limit:]):
                status, data = self._imap.fetch(num, "(RFC822)")
                if status != "OK":
                    continue
                for response_part in data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        results.append({
                            "id": num.decode(),
                            "subject": msg.get("Subject", ""),
                            "from": msg.get("From", ""),
                            "date": msg.get("Date", ""),
                            "body": _extract_body(msg),
                        })
            return results
        except Exception as e:
            log.error("Fetch emails failed: %s", e)
            return []

    def send(self, to: str, subject: str, body: str, html: bool = False) -> dict:
        """Send an email via SMTP."""
        cfg = self._load_config()
        if not cfg["smtp_server"] or not cfg["email"]:
            return {"success": False, "error": "SMTP not configured"}

        try:
            msg = MIMEMultipart()
            msg["From"] = cfg["email"]
            msg["To"] = to
            msg["Subject"] = subject
            subtype = "html" if html else "plain"
            msg.attach(MIMEText(body, subtype))

            if int(cfg["smtp_port"]) == 465:
                self._smtp = smtplib.SMTP_SSL(cfg["smtp_server"], int(cfg["smtp_port"]))
            else:
                self._smtp = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
                self._smtp.starttls()

            self._smtp.login(cfg["email"], cfg["password"])
            self._smtp.send_message(msg)
            self._smtp.quit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search emails by subject/body containing query."""
        if not self._imap and not self.connect():
            return []
        try:
            self._imap.select("INBOX")
            status, messages = self._imap.search(None, f'(OR SUBJECT "{query}" BODY "{query}")')
            if status != "OK":
                return []
            msg_ids = messages[0].split()
            results = []
            for num in reversed(msg_ids[-limit:]):
                status, data = self._imap.fetch(num, "(RFC822)")
                if status != "OK":
                    continue
                for response_part in data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        results.append({
                            "id": num.decode(),
                            "subject": msg.get("Subject", ""),
                            "from": msg.get("From", ""),
                            "date": msg.get("Date", ""),
                            "snippet": _extract_body(msg)[:200],
                        })
            return results
        except Exception as e:
            log.error("Email search failed: %s", e)
            return []


def _extract_body(msg) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    return payload.decode("utf-8", errors="replace") if payload else ""
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            return payload.decode("utf-8", errors="replace") if payload else ""
        except Exception:
            pass
    return ""


# Singleton
email_client = EmailIntegration()
