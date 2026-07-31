"""Email sending adapter.

Uses SMTP when SMTP_HOST is configured; otherwise logs the message so the
whole workflow stays runnable in development without a mail server.
"""

import logging
import smtplib

from app.adapters.mime import build_email_message
from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailSenderAdapter:
    def __init__(self):
        self.settings = get_settings()
        self.sent: list[dict[str, str]] = []  # in-memory log, handy for dev/tests

    def send(self, to: str, subject: str, body: str, *,
             signature_html: str | None = None, signature_text: str | None = None,
             logo_bytes: bytes | None = None, logo_content_type: str | None = None) -> None:
        record = {"to": to, "subject": subject, "body": body}
        self.sent.append(record)

        if not self.settings.smtp_host:
            # Metadata only — message bodies are PII and must stay out of logs
            logger.info("[email:console] to=%s subject=%r (%d chars, not sent: "
                        "SMTP not configured)", to, subject, len(body))
            return

        message = build_email_message(
            body, signature_html=signature_html, signature_text=signature_text,
            logo_bytes=logo_bytes, logo_content_type=logo_content_type)
        message["From"] = self.settings.smtp_from
        message["To"] = to
        message["Subject"] = subject

        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as smtp:
                smtp.starttls()
                if self.settings.smtp_user:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError):
            # smtplib.SMTPException already subclasses OSError, so callers that
            # catch OSError (sending.py, replies.py) keep working unchanged —
            # this just adds server-side visibility into *why* a send failed
            # (bad SMTP_FROM, wrong creds, unreachable host, etc.) without
            # putting the message body in the log.
            logger.error(
                "SMTP send failed: to=%s subject=%r host=%s:%s from=%s",
                to, subject, self.settings.smtp_host, self.settings.smtp_port,
                self.settings.smtp_from, exc_info=True,
            )
            raise
