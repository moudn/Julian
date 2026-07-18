"""Google OAuth 2.0 helpers for per-organization Calendar connections.

Flow: /integrations/google/connect returns Google's consent URL (with a
signed `state` identifying the org). Google redirects to our callback with a
one-time code, which we exchange for a refresh token stored on the org.
Access tokens are minted from the refresh token on demand and cached until
they expire.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GoogleCredential

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.freebusy "
    "https://www.googleapis.com/auth/gmail.send"
)


class GoogleOAuthError(Exception):
    pass


def _sign(org_id: int) -> str:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, str(org_id).encode(), hashlib.sha256).hexdigest()


def make_state(org_id: int) -> str:
    return f"{org_id}.{_sign(org_id)}"


def parse_state(state: str) -> int:
    """Return the org_id encoded in a state token, or raise if tampered."""
    try:
        org_part, signature = state.split(".", 1)
        org_id = int(org_part)
    except ValueError as exc:
        raise GoogleOAuthError("Malformed state parameter") from exc
    if not hmac.compare_digest(signature, _sign(org_id)):
        raise GoogleOAuthError("Invalid state signature")
    return org_id


def build_authorize_url(org_id: int) -> str:
    settings = get_settings()
    if not settings.google_client_id:
        raise GoogleOAuthError(
            "GOOGLE_CLIENT_ID is not configured. Create an OAuth client at "
            "console.cloud.google.com and set GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI."
        )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # required to receive a refresh_token
        "prompt": "consent",        # re-issue refresh_token on reconnect
        "state": make_state(org_id),
    }
    return f"{settings.google_oauth_auth_url}?{urlencode(params)}"


def _token_request(payload: dict) -> dict:
    settings = get_settings()
    try:
        response = httpx.post(settings.google_oauth_token_url, data=payload, timeout=30)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GoogleOAuthError(
            f"Google token endpoint returned {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"Google token request failed: {exc}") from exc
    return response.json()


def exchange_code(code: str) -> dict:
    settings = get_settings()
    return _token_request({
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    })


def refresh_access_token(refresh_token: str) -> dict:
    settings = get_settings()
    return _token_request({
        "refresh_token": refresh_token,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "grant_type": "refresh_token",
    })


def get_valid_access_token(db: Session, credential: GoogleCredential) -> str:
    """Return a live access token, refreshing and persisting it if expired."""
    now = datetime.now(timezone.utc)
    expiry = credential.token_expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if credential.access_token and expiry and expiry > now + timedelta(minutes=2):
        return credential.access_token

    tokens = refresh_access_token(credential.refresh_token)
    credential.access_token = tokens["access_token"]
    credential.token_expiry = now + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    db.commit()
    return credential.access_token
