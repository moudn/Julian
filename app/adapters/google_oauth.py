"""Google OAuth 2.0 helpers for per-organization Calendar connections.

Flow: /integrations/google/connect returns Google's consent URL (with a
signed `state` identifying the org). Google redirects to our callback with a
one-time code, which we exchange for a refresh token stored on the org.
Access tokens are minted from the refresh token on demand and cached until
they expire.
"""

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GoogleCredential, OAuthState, utcnow

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.freebusy "
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly"
)


class GoogleOAuthError(Exception):
    pass


class GoogleAccessRevoked(GoogleOAuthError):
    """Refresh failed because the customer revoked or expired access."""


STATE_TTL_MINUTES = 10


def make_state(db: Session, org_id: int) -> str:
    """Mint a single-use, short-lived state token bound to the org."""
    token = secrets.token_urlsafe(32)
    db.add(OAuthState(token=token, org_id=org_id,
                      expires_at=utcnow() + timedelta(minutes=STATE_TTL_MINUTES)))
    # opportunistic cleanup of expired states
    db.execute(delete(OAuthState).where(OAuthState.expires_at < utcnow()))
    db.commit()
    return token


def consume_state(db: Session, state: str) -> int:
    """Validate a state token exactly once; returns the org_id or raises."""
    record = db.scalar(select(OAuthState).where(OAuthState.token == state))
    if record is None:
        raise GoogleOAuthError(
            "Unknown or already-used state token. Restart the connection "
            "from /integrations/google/connect.")
    db.delete(record)  # single use, even if expired
    db.commit()
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        raise GoogleOAuthError("State token expired. Restart the connection.")
    return record.org_id


def build_authorize_url(db: Session, org_id: int) -> str:
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
        "state": make_state(db, org_id),
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
    """Return a live access token, refreshing and persisting it if expired.

    Raises GoogleAccessRevoked (and marks the credential broken) when Google
    rejects the refresh token — the customer must reconnect.
    """
    now = datetime.now(timezone.utc)
    expiry = credential.token_expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if credential.access_token and expiry and expiry > now + timedelta(minutes=2):
        return credential.access_token

    try:
        tokens = refresh_access_token(credential.refresh_token)
    except GoogleOAuthError as exc:
        # invalid_grant = refresh token revoked/expired; anything else 4xx
        # on the token endpoint is also unrecoverable without reconnect.
        if "invalid_grant" in str(exc) or "400" in str(exc) or "401" in str(exc):
            credential.broken = True
            credential.broken_reason = "Google access was revoked or expired"
            db.commit()
            raise GoogleAccessRevoked(str(exc)) from exc
        raise
    credential.access_token = tokens["access_token"]
    credential.token_expiry = now + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    if credential.broken:
        credential.broken = False
        credential.broken_reason = None
        credential.broken_notified = False
    db.commit()
    return credential.access_token
