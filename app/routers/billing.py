from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.stripe_billing import (
    StripeAdapter,
    StripeError,
    WebhookVerificationError,
    billing_enabled,
    verify_webhook_signature,
)
from app.auth import get_current_org, get_current_user
from app.database import get_db
from app.models import Organization, User

router = APIRouter(prefix="/billing", tags=["billing"])

ACTIVE_STATUSES = {"active", "trialing"}


def require_active_subscription(
    org: Organization = Depends(get_current_org),
) -> Organization:
    """Gate for product endpoints. A no-op while billing is disabled (dev)."""
    if billing_enabled() and org.subscription_status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=402,
            detail="No active subscription. Start one via POST /billing/checkout.",
        )
    return org


def get_stripe_adapter() -> StripeAdapter:
    return StripeAdapter()


class CheckoutOut(BaseModel):
    checkout_url: str


class PortalOut(BaseModel):
    portal_url: str


class BillingStatusOut(BaseModel):
    billing_enabled: bool
    subscription_status: str
    current_period_end: datetime | None


@router.get("/status", response_model=BillingStatusOut)
def status(org: Organization = Depends(get_current_org)):
    return BillingStatusOut(
        billing_enabled=billing_enabled(),
        subscription_status=org.subscription_status,
        current_period_end=org.current_period_end,
    )


@router.post("/checkout", response_model=CheckoutOut)
def checkout(
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
    stripe: StripeAdapter = Depends(get_stripe_adapter),
):
    """Create a Stripe Checkout session; open the returned URL to subscribe."""
    if not billing_enabled():
        raise HTTPException(status_code=503, detail="Billing is not configured")
    if org.subscription_status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Subscription is already active")
    try:
        url = stripe.create_checkout_session(org.id, user.email)
    except StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CheckoutOut(checkout_url=url)


@router.post("/portal", response_model=PortalOut)
def portal(
    org: Organization = Depends(get_current_org),
    stripe: StripeAdapter = Depends(get_stripe_adapter),
):
    """Customer Portal link for managing or cancelling the subscription."""
    if not org.stripe_customer_id:
        raise HTTPException(status_code=409, detail="No Stripe customer yet — subscribe first")
    try:
        url = stripe.create_portal_session(org.stripe_customer_id)
    except StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PortalOut(portal_url=url)


@router.post("/webhook")
async def webhook(
    request: Request,
    stripe_signature: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Stripe calls this on subscription lifecycle events (signature-verified)."""
    payload = await request.body()
    try:
        event = verify_webhook_signature(payload, stripe_signature)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        org = db.get(Organization, int(obj.get("client_reference_id") or 0))
        if org is not None:
            org.stripe_customer_id = obj.get("customer")
            org.stripe_subscription_id = obj.get("subscription")
            org.subscription_status = "active"
            db.commit()

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        org = db.scalar(select(Organization).where(
            Organization.stripe_subscription_id == obj.get("id")))
        if org is None and obj.get("customer"):
            org = db.scalar(select(Organization).where(
                Organization.stripe_customer_id == obj.get("customer")))
        if org is not None:
            if event_type == "customer.subscription.deleted":
                org.subscription_status = "canceled"
            else:
                org.subscription_status = obj.get("status", org.subscription_status)
            period_end = obj.get("current_period_end")
            if period_end:
                org.current_period_end = datetime.fromtimestamp(
                    int(period_end), tz=timezone.utc)
            db.commit()

    return {"received": True}


@router.get("/success")
def success():
    return {"message": "Subscription started — you're all set. "
                       "Check GET /billing/status with your API key."}


@router.get("/cancelled")
def cancelled():
    return {"message": "Checkout cancelled — no charge was made."}
