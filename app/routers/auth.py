from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import generate_api_key, get_current_org, get_current_user, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.deps import get_email_sender
from app.models import ApiKey, Organization, User
from app.security import make_reset_token, rate_limit, verify_reset_token

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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
    score_threshold: float | None = None
    product_description: str | None = Field(default=None, max_length=2000)
    email_footer: str | None = Field(default=None, max_length=1000)
    knowledge_base: str | None = Field(default=None, max_length=10000)
    timezone: str | None = Field(default=None, max_length=64)
    auto_reply_enabled: bool | None = None
    research_enabled: bool | None = None


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


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(request: SignupRequest, http_request: Request,
           db: Session = Depends(get_db)):
    """Create a new organization with its first user; returns an API key."""
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

    return AuthResponse(
        api_key=generate_api_key(db, user), organization_id=org.id, user_id=user.id
    )


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


@router.post("/forgot_password")
def forgot_password(request: ForgotPasswordRequest, http_request: Request,
                    db: Session = Depends(get_db),
                    email_sender=Depends(get_email_sender)):
    """Email a one-hour reset token. Always answers 200 (no account probing)."""
    rate_limit(http_request, "forgot", limit=5, window_seconds=300)
    user = db.scalar(select(User).where(User.email == request.email))
    if user is not None:
        token = make_reset_token(user.id)
        email_sender.send(
            to=user.email,
            subject="Reset your Julian password",
            body=(f"Hi {user.name},\n\nUse this token to set a new password "
                  f"(valid for 1 hour):\n\n{token}\n\n"
                  "POST it with your new password to /auth/reset_password, or "
                  "paste it into the dashboard's reset form.\n\n"
                  "If you didn't request this, you can ignore this email."),
        )
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


@router.get("/me", response_model=OrgOut)
def me(org: Organization = Depends(get_current_org)):
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
    )


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
    db.commit()
    db.refresh(org)
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
    )
