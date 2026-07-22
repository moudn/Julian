"""Authentication: password hashing, API keys, and the current-org dependency.

Stdlib-only crypto: PBKDF2-HMAC-SHA256 for passwords, SHA-256 for API key
lookup (keys themselves are 32 random url-safe bytes, shown once at creation).
"""

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApiKey, Organization, User

_PBKDF2_ITERATIONS = 600_000

bearer_scheme = HTTPBearer(auto_error=False)


# ---------- Passwords ----------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


# ---------- API keys ----------

def generate_api_key(db: Session, user: User) -> str:
    """Create a new API key for a user; the plaintext is returned exactly once."""
    plaintext = f"sk_{secrets.token_urlsafe(32)}"
    db.add(ApiKey(
        org_id=user.org_id,
        user_id=user.id,
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        prefix=plaintext[:8],
    ))
    db.commit()
    return plaintext


def _lookup_key(db: Session, plaintext: str) -> ApiKey | None:
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))


# ---------- Request dependencies ----------

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Send 'Authorization: Bearer <api_key>'.",
        )
    api_key = _lookup_key(db, credentials.credentials)
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user = db.get(User, api_key.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


def get_current_org(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Organization:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=401, detail="Organization not found")
    return org


def require_verified_user(user: User = Depends(get_current_user)) -> User:
    """Gate for actions that send email — the account's own email must be
    confirmed first (blocks throwaway/spoofed signups from sending)."""
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Verify your email address before sending outreach. "
                   "Check your inbox or call /auth/resend_verification.",
        )
    return user
