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


class GmailReaderAdapter:
    """Read inbound mail from the connected account (reply detection)."""

    def __init__(self, token_provider: Callable[[], str],
                 client: httpx.Client | None = None):
        self.token_provider = token_provider
        self.base_url = get_settings().gmail_api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self._client.get(
                f"{self.base_url}{path}", params=params,
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
        return response.json()

    def list_message_ids(self, query: str, max_results: int = 20) -> list[str]:
        data = self._get("/users/me/messages",
                         params={"q": query, "maxResults": max_results})
        return [m["id"] for m in data.get("messages", [])]

    def get_message(self, message_id: str) -> dict:
        """Return {id, subject, from, body} for one message."""
        data = self._get(f"/users/me/messages/{message_id}", params={"format": "full"})
        payload = data.get("payload", {})
        headers = {h["name"].lower(): h["value"]
                   for h in payload.get("headers", [])}
        return {
            "id": data.get("id", message_id),
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "body": _extract_plain_text(payload),
        }


def _extract_plain_text(payload: dict) -> str:
    """Pull the text/plain body out of a Gmail message payload."""
    body_data = payload.get("body", {}).get("data")
    if body_data and payload.get("mimeType", "").startswith("text/plain"):
        return _b64url_decode(body_data)
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    if body_data:  # fall back to whatever the top-level body holds
        return _b64url_decode(body_data)
    return ""


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode(errors="replace")
    except Exception:
        return ""


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
