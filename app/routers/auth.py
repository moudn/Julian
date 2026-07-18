from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import generate_api_key, get_current_org, get_current_user, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.models import Organization, User

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
    sales_rep_email: EmailStr | None = None
    score_threshold: float | None = None
    product_description: str | None = Field(default=None, max_length=2000)
    email_footer: str | None = Field(default=None, max_length=1000)
    knowledge_base: str | None = Field(default=None, max_length=10000)


class OrgOut(BaseModel):
    id: int
    name: str
    sales_rep_email: str | None
    score_threshold: float
    product_description: str | None
    email_footer: str | None
    knowledge_base: str | None


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(request: SignupRequest, db: Session = Depends(get_db)):
    """Create a new organization with its first user; returns an API key."""
    if db.scalar(select(User).where(User.email == request.email)):
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    org = Organization(
        name=request.organization_name,
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
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Exchange email + password for a fresh API key."""
    user = db.scalar(select(User).where(User.email == request.email))
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return AuthResponse(
        api_key=generate_api_key(db, user), organization_id=user.org_id, user_id=user.id
    )


@router.get("/me", response_model=OrgOut)
def me(org: Organization = Depends(get_current_org)):
    return OrgOut(
        id=org.id, name=org.name, sales_rep_email=org.sales_rep_email,
        score_threshold=org.score_threshold,
        product_description=org.product_description,
        email_footer=org.email_footer,
        knowledge_base=org.knowledge_base,
    )


@router.patch("/org", response_model=OrgOut)
def update_org_settings(
    request: OrgSettingsIn,
    org: Organization = Depends(get_current_org),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
    db.commit()
    db.refresh(org)
    return OrgOut(
        id=org.id, name=org.name, sales_rep_email=org.sales_rep_email,
        score_threshold=org.score_threshold,
        product_description=org.product_description,
        email_footer=org.email_footer,
        knowledge_base=org.knowledge_base,
    )
