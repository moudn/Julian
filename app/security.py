"""Security utilities: encryption at rest, rate limiting, reset tokens."""

import base64
import hashlib
import hmac
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from sqlalchemy import String, Text, TypeDecorator

from app.config import get_settings


# ---------- encryption at rest ----------

def _fernet() -> Fernet:
    settings = get_settings()
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode())
    # Dev fallback: derive a stable key from SECRET_KEY. Set ENCRYPTION_KEY
    # (a real Fernet key) in production.
    derived = hashlib.sha256(f"fernet:{settings.secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


class EncryptedText(TypeDecorator):
    """Column type that transparently encrypts values at rest (Fernet)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return decrypt(value)
        except InvalidToken:
            # Value predates encryption (dev DB) — return as-is
            return value


# ---------- rate limiting (in-memory sliding window) ----------
# Sufficient for a single process; move to Redis when running replicas.

_buckets: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, bucket: str, limit: int = 10,
               window_seconds: int = 60) -> None:
    """Raise 429 when `limit` calls in `window_seconds` is exceeded per IP."""
    client_ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client_ip}"
    now = time.monotonic()
    entries = _buckets[key]
    while entries and now - entries[0] > window_seconds:
        entries.popleft()
    if len(entries) >= limit:
        raise HTTPException(status_code=429,
                            detail="Too many attempts — try again in a minute.")
    entries.append(now)


# ---------- password-reset tokens (signed, expiring, stateless) ----------

def _make_token(user_id: int, purpose: str, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    payload = f"{user_id}.{expires}"
    signature = hmac.new(get_settings().secret_key.encode(),
                         f"{purpose}:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_token(token: str, purpose: str) -> int | None:
    try:
        user_part, expires_part, signature = token.split(".")
        payload = f"{user_part}.{expires_part}"
    except ValueError:
        return None
    expected = hmac.new(get_settings().secret_key.encode(),
                        f"{purpose}:{payload}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    if int(expires_part) < time.time():
        return None
    return int(user_part)


def make_reset_token(user_id: int, ttl_seconds: int = 3600) -> str:
    return _make_token(user_id, "reset", ttl_seconds)


def verify_reset_token(token: str) -> int | None:
    return _verify_token(token, "reset")


def make_verify_token(user_id: int, ttl_seconds: int = 86400) -> str:
    return _make_token(user_id, "verify", ttl_seconds)


def verify_email_token(token: str) -> int | None:
    return _verify_token(token, "verify")
