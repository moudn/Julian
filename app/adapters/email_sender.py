"""Email sending adapter.

Uses SMTP when SMTP_HOST is configured; otherwise logs the message so the
whole workflow stays runnable in development without a mail server.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailSenderAdapter:
    def __init__(self):
        self.settings = get_settings()
        self.sent: list[dict[str, str]] = []  # in-memory log, handy for dev/tests

    def send(self, to: str, subject: str, body: str) -> None:
        record = {"to": to, "subject": subject, "body": body}
        self.sent.append(record)

        if not self.settings.smtp_host:
            # Metadata only — message bodies are PII and must stay out of logs
            logger.info("[email:console] to=%s subject=%r (%d chars, not sent: "
                        "SMTP not configured)", to, subject, len(body))
            return

        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as smtp:
            smtp.starttls()
            if self.settings.smtp_user:
                smtp.login(self.settings.smtp_user, self.settings.smtp_password)
            smtp.send_message(message)
