from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.google_oauth import (
    GoogleOAuthError,
    build_authorize_url,
    exchange_code,
    parse_state,
)
from app.auth import get_current_org
from app.database import get_db
from app.models import GoogleCredential, Organization, utcnow

router = APIRouter(prefix="/integrations/google", tags=["integrations"])


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


@router.get("/connect", response_model=ConnectOut)
def connect(org: Organization = Depends(get_current_org)):
    """Start the OAuth flow: returns the Google consent URL for this org."""
    try:
        return ConnectOut(authorize_url=build_authorize_url(org.id))
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/callback")
def callback(code: str, state: str, db: Session = Depends(get_db)):
    """Google redirects here after consent. Stores the org's refresh token.

    Unauthenticated by design (the browser lands here from Google); the org
    is identified by the HMAC-signed state parameter.
    """
    try:
        org_id = parse_state(state)
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
    db.commit()

    return {"status": "connected", "message": "Google Calendar is now connected."}


@router.get("/status", response_model=StatusOut)
def status(org: Organization = Depends(get_current_org), db: Session = Depends(get_db)):
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is None:
        return StatusOut(connected=False)
    return StatusOut(connected=True, account_email=credential.account_email,
                     calendar_id=credential.calendar_id)


@router.delete("", status_code=204)
def disconnect(org: Organization = Depends(get_current_org), db: Session = Depends(get_db)):
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is not None:
        db.delete(credential)
        db.commit()
