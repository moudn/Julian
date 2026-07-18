"""Send email through the customer's own Gmail via the Gmail API.

Sending from the tenant's real mailbox (rather than a bulk SMTP relay) is
the single biggest deliverability lever for cold outreach — and it reuses
the Google OAuth connection the tenant already made for Calendar.
"""

import base64
from email.message import EmailMessage
from typing import Callable

import httpx

from app.config import get_settings


class GmailError(Exception):
    pass


class GmailSenderAdapter:
    def __init__(self, token_provider: Callable[[], str],
                 client: httpx.Client | None = None):
        self.token_provider = token_provider
        self.base_url = get_settings().gmail_api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=30)
        self.sent: list[dict[str, str]] = []  # local record for inspection

    def send(self, to: str, subject: str, body: str) -> str:
        """Send a plain-text email as the connected account; returns Gmail id."""
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        try:
            response = self._client.post(
                f"{self.base_url}/users/me/messages/send",
                json={"raw": raw},
                headers={"Authorization": f"Bearer {self.token_provider()}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GmailError(
                f"Gmail API returned {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise GmailError(f"Gmail API request failed: {exc}") from exc

        self.sent.append({"to": to, "subject": subject, "body": body})
        return response.json().get("id", "")
