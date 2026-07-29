import logging
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.google_oauth import (
    GoogleOAuthError,
    build_authorize_url,
    consume_state,
    exchange_code,
)
from app.auth import get_current_org
from app.config import get_settings
from app.database import get_db
from app.models import GoogleCredential, Organization, utcnow

router = APIRouter(prefix="/integrations/google", tags=["integrations"])
logger = logging.getLogger(__name__)


class ConnectOut(BaseModel):
    authorize_url: str
    instructions: str = (
        "Open authorize_url in a browser, approve access, and you will be "
        "redirected back to the callback which stores the connection."
    )


class StatusOut(BaseModel):
    connected: bool
    account_email: str | None = None
    calendar_id: str | None = None
    broken: bool = False
    broken_reason: str | None = None


@router.get("/connect", response_model=ConnectOut)
def connect(org: Organization = Depends(get_current_org),
            db: Session = Depends(get_db)):
    """Start the OAuth flow: returns the Google consent URL for this org."""
    try:
        return ConnectOut(authorize_url=build_authorize_url(db, org.id))
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/callback")
def callback(code: str, state: str, db: Session = Depends(get_db)):
    """Google redirects here after consent. Stores the org's refresh token.

    Unauthenticated by design (the browser lands here from Google); the org
    is identified by a single-use, short-lived state token minted at
    /connect — unknown, reused, or expired states are rejected.
    """
    try:
        org_id = consume_state(db, state)
        tokens = exchange_code(code)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Google did not return a refresh token. Remove the app's "
                   "access at myaccount.google.com/permissions and connect again.",
        )

    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org_id)
    )
    if credential is None:
        credential = GoogleCredential(org_id=org_id, refresh_token=refresh_token)
        db.add(credential)
    else:
        credential.refresh_token = refresh_token
    credential.access_token = tokens.get("access_token")
    credential.token_expiry = utcnow() + timedelta(
        seconds=int(tokens.get("expires_in", 3600)))
    credential.account_email = _fetch_account_email(credential.access_token)
    credential.broken = False
    credential.broken_reason = None
    credential.broken_notified = False
    db.commit()
    _verify_cache.pop(org_id, None)  # re-check on the next status call

    # Land the user back in the dashboard rather than on raw JSON. The
    # query flag lets the UI confirm the connection without a manual reload.
    base = get_settings().app_base_url.rstrip("/")
    return RedirectResponse(url=f"{base}/app/#/settings?google=connected",
                            status_code=303)


def _fetch_account_email(access_token: str | None) -> str | None:
    """Best-effort lookup of the connected Gmail address (for display)."""
    if not access_token:
        return None
    import httpx

    try:
        response = httpx.get(
            f"{get_settings().gmail_api_base.rstrip('/')}/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        response.raise_for_status()
        return response.json().get("emailAddress")
    except httpx.HTTPError:
        return None


# Revocation happens in Google's UI, so nothing tells Julian about it. The
# stored `broken` flag is only set when a send fails, which means the
# dashboard could report "Connected" for days while outreach silently went
# nowhere. Verify against Google when showing status, cached briefly so
# re-renders don't hammer the API.
_VERIFY_TTL_SECONDS = 60
_verify_cache: dict[int, float] = {}


def _verify_connection(db: Session, credential: GoogleCredential) -> None:
    """Ask Google whether the connection still works; mark it broken if not."""
    from app.adapters.google_oauth import GoogleAccessRevoked, get_valid_access_token

    # None, not 0.0, for "never checked": time.monotonic() is measured from
    # boot, so a 0.0 sentinel makes the first check on a freshly started
    # machine look recent and skip verification for the first minute.
    last = _verify_cache.get(credential.org_id)
    if credential.broken or (last is not None
                             and time.monotonic() - last < _VERIFY_TTL_SECONDS):
        return
    _verify_cache[credential.org_id] = time.monotonic()
    try:
        token = get_valid_access_token(db, credential)
        if _fetch_account_email(token) is None:
            # Token looked valid but Google refused it — force a refresh,
            # which is what actually distinguishes revoked from expired.
            get_valid_access_token(db, credential, force_refresh=True)
    except GoogleAccessRevoked:
        pass  # get_valid_access_token has already flagged it
    except GoogleOAuthError as exc:
        logger.info("could not verify Google connection for org %s: %s",
                    credential.org_id, exc)


@router.get("/status", response_model=StatusOut)
def status(org: Organization = Depends(get_current_org), db: Session = Depends(get_db)):
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is None:
        return StatusOut(connected=False)
    _verify_connection(db, credential)
    db.refresh(credential)
    return StatusOut(connected=True, account_email=credential.account_email,
                     calendar_id=credential.calendar_id,
                     broken=credential.broken, broken_reason=credential.broken_reason)


@router.delete("", status_code=204)
def disconnect(org: Organization = Depends(get_current_org), db: Session = Depends(get_db)):
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is not None:
        db.delete(credential)
        db.commit()
