"""Send email through the customer's own Gmail via the Gmail API.

Sending from the tenant's real mailbox (rather than a bulk SMTP relay) is
the single biggest deliverability lever for cold outreach — and it reuses
the Google OAuth connection the tenant already made for Calendar.
"""

import base64
from typing import Callable

import httpx

from app.adapters.mime import build_email_message
from app.config import get_settings


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """Gmail rejected our token (401).

    Distinct from a generic GmailError because it means the connection is
    unusable, not that one send happened to fail — retrying with the same
    token can only fail again.
    """


class GmailReaderAdapter:
    """Read inbound mail from the connected account (reply detection)."""

    def __init__(self, token_provider: Callable[[], str],
                 client: httpx.Client | None = None,
                 on_auth_error: Callable[[], str] | None = None):
        self.token_provider = token_provider
        # Called when Gmail 401s, to force a token refresh. Raises
        # GoogleAccessRevoked if the connection is genuinely gone.
        self.on_auth_error = on_auth_error
        self.base_url = get_settings().gmail_api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._request_with_auth_retry(
            lambda token: self._client.get(
                f"{self.base_url}{path}", params=params,
                headers={"Authorization": f"Bearer {token}"}))
        return response.json()

    def _request_with_auth_retry(self, send) -> httpx.Response:
        """Issue a request, refreshing the token once on a 401.

        A cached access token can be dead before it looks expired — most
        obviously when the customer revokes access in their Google account.
        Without this the 401 looked like an ordinary send failure and the
        connection was never marked broken.
        """
        token = self.token_provider()
        for attempt in (1, 2):
            try:
                response = send(token)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code == 401 and attempt == 1
                        and self.on_auth_error is not None):
                    token = self.on_auth_error()  # may raise GoogleAccessRevoked
                    continue
                if exc.response.status_code == 401:
                    raise GmailAuthError(
                        f"Gmail rejected the access token: "
                        f"{exc.response.text[:300]}") from exc
                raise GmailError(
                    f"Gmail API returned {exc.response.status_code}: "
                    f"{exc.response.text[:500]}") from exc
            except httpx.HTTPError as exc:
                raise GmailError(f"Gmail API request failed: {exc}") from exc
        raise GmailError("Gmail request failed after retry")

    def list_message_ids(self, query: str, max_results: int = 20) -> list[str]:
        data = self._get("/users/me/messages",
                         params={"q": query, "maxResults": max_results})
        return [m["id"] for m in data.get("messages", [])]

    def get_message(self, message_id: str) -> dict:
        """Return {id, subject, from, body, label_ids} for one message."""
        data = self._get(f"/users/me/messages/{message_id}", params={"format": "full"})
        return _parse_message(data)

    def get_thread_messages(self, thread_id: str) -> list[dict]:
        """Return every message in a Gmail thread, in the same shape as
        get_message. Used to scope reply-polling to the specific
        conversation Julian started with a lead, rather than a blanket
        sender-address search across the whole mailbox."""
        data = self._get(f"/users/me/threads/{thread_id}", params={"format": "full"})
        return [_parse_message(m) for m in data.get("messages", [])]


def _parse_message(data: dict) -> dict:
    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    return {
        "id": data.get("id"),
        "thread_id": data.get("threadId"),
        # "SENT" means this account sent it — used to tell Julian's own
        # outbound copy apart from a genuine inbound reply within a thread.
        "label_ids": data.get("labelIds", []),
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
                 client: httpx.Client | None = None,
                 on_auth_error: Callable[[], str] | None = None):
        self.token_provider = token_provider
        self.on_auth_error = on_auth_error
        self.base_url = get_settings().gmail_api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=30)
        self.sent: list[dict[str, str]] = []  # local record for inspection
        # Thread of the message just sent — callers use this to record which
        # Gmail conversation a lead's reply-polling should be scoped to.
        self.last_thread_id: str | None = None

    def send(self, to: str, subject: str, body: str, *,
             signature_html: str | None = None, signature_text: str | None = None,
             logo_bytes: bytes | None = None, logo_content_type: str | None = None) -> str:
        """Send an email as the connected account; returns Gmail id.

        Plain text unless signature_html is given, in which case the email
        is a multipart/alternative with an HTML branded signature (and an
        inline logo if supplied) alongside a plain-text fallback.
        """
        message = build_email_message(
            body, signature_html=signature_html, signature_text=signature_text,
            logo_bytes=logo_bytes, logo_content_type=logo_content_type)
        message["To"] = to
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        response = GmailReaderAdapter._request_with_auth_retry(
            self, lambda token: self._client.post(
                f"{self.base_url}/users/me/messages/send",
                json={"raw": raw},
                headers={"Authorization": f"Bearer {token}"}))

        self.sent.append({"to": to, "subject": subject, "body": body})
        result = response.json()
        self.last_thread_id = result.get("threadId")
        return result.get("id", "")
