import base64
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import generate_api_key, get_current_org, get_current_user, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.deps import get_email_sender
from app.models import ApiKey, Organization, User
from app.security import (
    make_reset_token,
    make_verify_token,
    rate_limit,
    verify_email_token,
    verify_reset_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

EMAIL_SEND_ERROR_DETAIL = (
    "Couldn't send the email — the mail server rejected it or is unreachable. "
    "Check SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM and try again."
)


PASSWORD_RULES = (
    "Password must be at least 8 characters and include an uppercase "
    "letter, a lowercase letter, and a number."
)


def validate_password_strength(password: str) -> str:
    """Enforced server-side so the rules hold regardless of the client.

    Deliberately no forced symbol: length and mixed character classes carry
    most of the real strength, and symbol rules mostly push people towards
    predictable substitutions.
    """
    if len(password) < 8:
        raise ValueError(PASSWORD_RULES)
    if not any(c.isupper() for c in password):
        raise ValueError(PASSWORD_RULES)
    if not any(c.islower() for c in password):
        raise ValueError(PASSWORD_RULES)
    if not any(c.isdigit() for c in password):
        raise ValueError(PASSWORD_RULES)
    return password


class SignupRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _strong_password(cls, value: str) -> str:
        return validate_password_strength(value)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    api_key: str
    organization_id: int
    user_id: int
    note: str = "Store this API key now — it is not shown again."


class OrgSettingsIn(BaseModel):
    sender_name: str | None = Field(default=None, max_length=255)
    sales_rep_email: EmailStr | None = None
    # A negative threshold silently passes every lead, including ones the
    # ICP rules scored at zero — an accidental way to blast an entire list.
    score_threshold: float | None = Field(default=None, ge=0, le=1000)
    product_description: str | None = Field(default=None, max_length=2000)
    email_footer: str | None = Field(default=None, max_length=1000)
    knowledge_base: str | None = Field(default=None, max_length=10000)
    timezone: str | None = Field(default=None, max_length=64)
    auto_reply_enabled: bool | None = None
    research_enabled: bool | None = None
    example_emails: str | None = Field(default=None, max_length=8000)
    step_templates: dict[str, str] | None = None
    email_signature_enabled: bool | None = None
    signature_title: str | None = Field(default=None, max_length=255)
    signature_phone: str | None = Field(default=None, max_length=64)
    signature_website: str | None = Field(default=None, max_length=255)
    ai_fit_scoring_enabled: bool | None = None
    ai_fit_weight: float | None = Field(default=None, ge=0, le=1000)


class OrgOut(BaseModel):
    id: int
    name: str
    sender_name: str | None
    sales_rep_email: str | None
    score_threshold: float
    product_description: str | None
    email_footer: str | None
    knowledge_base: str | None
    timezone: str
    auto_reply_enabled: bool
    research_enabled: bool
    example_emails: str | None
    step_templates: dict[str, str] | None
    email_signature_enabled: bool
    signature_title: str | None
    signature_phone: str | None
    signature_website: str | None
    logo_data_url: str | None
    ai_fit_scoring_enabled: bool
    ai_fit_weight: float
    email_verified: bool = True


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(request: SignupRequest, http_request: Request,
           db: Session = Depends(get_db),
           email_sender=Depends(get_email_sender)):
    """Create a new organization with its first user; returns an API key.

    A verification email is sent; unverified accounts can sign in and
    configure settings but cannot activate outreach (see require_verified).
    """
    rate_limit(http_request, "signup", limit=5, window_seconds=60)
    if db.scalar(select(User).where(User.email == request.email)):
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    org = Organization(
        name=request.organization_name,
        sender_name=request.name,
        sales_rep_email=request.email,
        score_threshold=get_settings().score_threshold,
    )
    db.add(org)
    db.flush()
    user = User(
        org_id=org.id,
        email=request.email,
        name=request.name,
        password_hash=hash_password(request.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        _send_verification_email(email_sender, user)
    except OSError as exc:
        # The account already exists at this point (committed above) — a
        # broken mail server shouldn't make signup look like it failed.
        # The user can retry from the dashboard's "Resend email" button.
        logger.error("verification email failed to send during signup for "
                     "user %s: %s", user.id, exc)

    return AuthResponse(
        api_key=generate_api_key(db, user), organization_id=org.id, user_id=user.id
    )


def _send_verification_email(email_sender, user: User) -> None:
    token = make_verify_token(user.id)
    link = f"{get_settings().app_base_url.rstrip('/')}/auth/verify?token={token}"
    email_sender.send(
        to=user.email,
        subject="Confirm your email for Julian",
        body=(f"Hi {user.name},\n\nWelcome to Julian. Confirm your email so "
              f"you can start sending outreach:\n\n{link}\n\n"
              f"If the link doesn't work, paste this code into the "
              f"dashboard instead:\n\n{token}\n\n"
              "Either way, it's valid for 24 hours."),
    )


@router.get("/verify")
def verify_email_link(token: str, db: Session = Depends(get_db)):
    """Clickable link from the verification email.

    Redirects back into the dashboard either way so the user always lands
    somewhere useful rather than on raw JSON.
    """
    user_id = verify_email_token(token)
    user = db.get(User, user_id) if user_id is not None else None
    base = get_settings().app_base_url.rstrip("/")
    if user is None:
        return RedirectResponse(url=f"{base}/app/#/settings?verified=expired",
                                status_code=303)
    user.email_verified = True
    db.commit()
    return RedirectResponse(url=f"{base}/app/#/dashboard?verified=1",
                            status_code=303)


class VerifyEmailRequest(BaseModel):
    token: str


@router.post("/verify_email")
def verify_email(request: VerifyEmailRequest, db: Session = Depends(get_db)):
    user_id = verify_email_token(request.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user.email_verified = True
    db.commit()
    return {"status": "ok", "message": "Email verified."}


@router.post("/resend_verification")
def resend_verification(user: User = Depends(get_current_user),
                        email_sender=Depends(get_email_sender)):
    if user.email_verified:
        return {"status": "ok", "message": "Already verified."}
    try:
        _send_verification_email(email_sender, user)
    except OSError as exc:
        logger.error("resend_verification failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=502, detail=EMAIL_SEND_ERROR_DETAIL) from exc
    return {"status": "ok", "message": "Verification email sent."}


@router.post("/login", response_model=AuthResponse)
def login(request: LoginRequest, http_request: Request,
          db: Session = Depends(get_db)):
    """Exchange email + password for a fresh API key."""
    rate_limit(http_request, "login", limit=10, window_seconds=60)
    user = db.scalar(select(User).where(User.email == request.email))
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return AuthResponse(
        api_key=generate_api_key(db, user), organization_id=user.org_id, user_id=user.id
    )


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strong_password(cls, value: str) -> str:
        return validate_password_strength(value)


@router.post("/forgot_password")
def forgot_password(request: ForgotPasswordRequest, http_request: Request,
                    db: Session = Depends(get_db),
                    email_sender=Depends(get_email_sender)):
    """Email a one-hour reset token. Always answers 200 (no account probing)."""
    rate_limit(http_request, "forgot", limit=5, window_seconds=300)
    user = db.scalar(select(User).where(User.email == request.email))
    if user is not None:
        token = make_reset_token(user.id)
        try:
            email_sender.send(
                to=user.email,
                subject="Reset your Julian password",
                body=(f"Hi {user.name},\n\nUse this token to set a new password "
                      f"(valid for 1 hour):\n\n{token}\n\n"
                      "POST it with your new password to /auth/reset_password, or "
                      "paste it into the dashboard's reset form.\n\n"
                      "If you didn't request this, you can ignore this email."),
            )
        except OSError as exc:
            # Anti-enumeration: never let a mail-server failure change the
            # response — always answer "ok" regardless of what happened.
            logger.error("forgot_password email failed for user %s: %s", user.id, exc)
    return {"status": "ok",
            "message": "If that email has an account, a reset token was sent."}


@router.post("/reset_password")
def reset_password(request: ResetPasswordRequest, http_request: Request,
                   db: Session = Depends(get_db)):
    rate_limit(http_request, "reset", limit=10, window_seconds=300)
    user_id = verify_reset_token(request.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user.password_hash = hash_password(request.new_password)
    db.commit()
    return {"status": "ok", "message": "Password updated — log in to get a new API key."}


class ApiKeyOut(BaseModel):
    id: int
    prefix: str
    created_at: str


@router.get("/keys", response_model=list[ApiKeyOut])
def list_keys(user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    keys = db.scalars(select(ApiKey).where(ApiKey.org_id == user.org_id)
                      .order_by(ApiKey.created_at)).all()
    return [ApiKeyOut(id=k.id, prefix=k.prefix,
                      created_at=k.created_at.isoformat()) for k in keys]


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: int, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """Revoke an API key immediately (e.g. after a leak)."""
    key = db.get(ApiKey, key_id)
    if key is None or key.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Key not found")
    db.delete(key)
    db.commit()


def _org_out(org: Organization, email_verified: bool = True) -> OrgOut:
    logo_data_url = None
    if org.logo_image and org.logo_content_type:
        logo_data_url = (f"data:{org.logo_content_type};base64,"
                         + base64.b64encode(org.logo_image).decode())
    return OrgOut(
        id=org.id, name=org.name, sender_name=org.sender_name,
        sales_rep_email=org.sales_rep_email,
        score_threshold=org.score_threshold,
        product_description=org.product_description,
        email_footer=org.email_footer,
        knowledge_base=org.knowledge_base,
        timezone=org.timezone,
        auto_reply_enabled=org.auto_reply_enabled,
        research_enabled=org.research_enabled,
        example_emails=org.example_emails,
        step_templates=org.step_templates,
        email_signature_enabled=org.email_signature_enabled,
        signature_title=org.signature_title,
        signature_phone=org.signature_phone,
        signature_website=org.signature_website,
        logo_data_url=logo_data_url,
        ai_fit_scoring_enabled=org.ai_fit_scoring_enabled,
        ai_fit_weight=org.ai_fit_weight,
        email_verified=email_verified,
    )


@router.get("/me", response_model=OrgOut)
def me(org: Organization = Depends(get_current_org),
       user: User = Depends(get_current_user)):
    return _org_out(org, email_verified=user.email_verified)


@router.patch("/org", response_model=OrgOut)
def update_org_settings(
    request: OrgSettingsIn,
    org: Organization = Depends(get_current_org),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if request.sender_name is not None:
        org.sender_name = request.sender_name
    if request.sales_rep_email is not None:
        org.sales_rep_email = request.sales_rep_email
    if request.score_threshold is not None:
        org.score_threshold = request.score_threshold
    if request.product_description is not None:
        org.product_description = request.product_description
    if request.email_footer is not None:
        org.email_footer = request.email_footer
    if request.knowledge_base is not None:
        org.knowledge_base = request.knowledge_base
    if request.timezone is not None:
        try:
            ZoneInfo(request.timezone)
        except Exception:
            raise HTTPException(status_code=422,
                                detail=f"Unknown timezone {request.timezone!r} "
                                       "(use an IANA name like Europe/London)")
        org.timezone = request.timezone
    if request.auto_reply_enabled is not None:
        org.auto_reply_enabled = request.auto_reply_enabled
    if request.research_enabled is not None:
        org.research_enabled = request.research_enabled
    if request.example_emails is not None:
        org.example_emails = request.example_emails
    if request.step_templates is not None:
        org.step_templates = request.step_templates
    if request.email_signature_enabled is not None:
        org.email_signature_enabled = request.email_signature_enabled
    if request.signature_title is not None:
        org.signature_title = request.signature_title
    if request.signature_phone is not None:
        org.signature_phone = request.signature_phone
    if request.signature_website is not None:
        org.signature_website = request.signature_website
    if request.ai_fit_scoring_enabled is not None:
        org.ai_fit_scoring_enabled = request.ai_fit_scoring_enabled
    if request.ai_fit_weight is not None:
        org.ai_fit_weight = request.ai_fit_weight
    db.commit()
    db.refresh(org)
    return _org_out(org, email_verified=user.email_verified)


MAX_LOGO_BYTES = 300 * 1024

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_image_content_type(data: bytes) -> str | None:
    """Identify an image by its magic bytes rather than trusting the
    client-supplied Content-Type header."""
    for signature, content_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post("/org/logo", response_model=OrgOut)
async def upload_org_logo(
    file: UploadFile,
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a logo to embed inline in outreach emails once the branded
    signature is enabled. Validated by magic bytes, not the client-supplied
    content type."""
    data = await file.read()
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"Logo too large (max {MAX_LOGO_BYTES // 1024} KB)")
    content_type = _sniff_image_content_type(data)
    if content_type is None:
        raise HTTPException(status_code=400,
                            detail="Unrecognized image format (use PNG, JPEG, GIF, or WEBP)")
    org.logo_image = data
    org.logo_content_type = content_type
    db.commit()
    db.refresh(org)
    return _org_out(org, email_verified=user.email_verified)


@router.delete("/org/logo", response_model=OrgOut)
def delete_org_logo(
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    org.logo_image = None
    org.logo_content_type = None
    db.commit()
    db.refresh(org)
    return _org_out(org, email_verified=user.email_verified)
