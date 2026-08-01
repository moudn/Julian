"""Stripe billing adapter (subscriptions via Checkout + webhooks).

Talks to the Stripe REST API directly with httpx. Billing is considered
enabled only when STRIPE_SECRET_KEY is set; without it the app runs open
(development mode) and the gating dependency lets everything through.
"""

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.config import get_settings
from app.plans import PLANS


class StripeError(Exception):
    pass


class WebhookVerificationError(StripeError):
    pass


def billing_enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


class StripeAdapter:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.stripe_secret_key
        self.base_url = settings.stripe_api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise StripeError("STRIPE_SECRET_KEY is not configured")
        try:
            response = self._client.post(
                f"{self.base_url}{path}",
                data=data,
                auth=(self.api_key, ""),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise StripeError(
                f"Stripe API returned {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise StripeError(f"Stripe API request failed: {exc}") from exc
        return response.json()

    def create_checkout_session(self, org_id: int, customer_email: str, plan: str) -> str:
        """Create a subscription Checkout session for the given plan
        (app/plans.py); returns the payment URL."""
        if plan not in PLANS:
            raise StripeError(f"Unknown plan {plan!r}")
        settings = get_settings()
        price_id = getattr(settings, f"stripe_price_id_{plan}", "")
        if not price_id:
            raise StripeError(f"STRIPE_PRICE_ID_{plan.upper()} is not configured")
        params = {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "client_reference_id": str(org_id),
            "customer_email": customer_email,
            "success_url": settings.billing_success_url,
            "cancel_url": settings.billing_cancel_url,
            # Echoed back on every webhook event for this session/subscription
            # so we know which plan (and therefore lead quota) was purchased.
            "metadata[plan]": plan,
            "subscription_data[metadata][plan]": plan,
        }
        if settings.trial_period_days > 0:
            params["subscription_data[trial_period_days]"] = str(settings.trial_period_days)
        session = self._post("/checkout/sessions", params)
        return session["url"]

    def create_portal_session(self, stripe_customer_id: str) -> str:
        """Create a Customer Portal session (manage/cancel subscription)."""
        session = self._post("/billing_portal/sessions", {
            "customer": stripe_customer_id,
            "return_url": get_settings().billing_success_url,
        })
        return session["url"]


def verify_webhook_signature(
    payload: bytes, signature_header: str, secret: str | None = None,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    """Verify a Stripe-Signature header and return the parsed event.

    Implements Stripe's scheme: HMAC-SHA256 over "{timestamp}.{payload}"
    with the webhook signing secret, compared against the v1 signature(s).
    """
    secret = secret if secret is not None else get_settings().stripe_webhook_secret
    if not secret:
        raise WebhookVerificationError("STRIPE_WEBHOOK_SECRET is not configured")

    timestamp = None
    candidates = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if not timestamp or not candidates:
        raise WebhookVerificationError("Malformed Stripe-Signature header")

    try:
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            raise WebhookVerificationError("Webhook timestamp outside tolerance")
    except ValueError as exc:
        raise WebhookVerificationError("Malformed webhook timestamp") from exc

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise WebhookVerificationError("Webhook signature mismatch")

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WebhookVerificationError("Webhook payload is not valid JSON") from exc
