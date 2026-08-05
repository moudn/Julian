from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
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
from app.config import get_settings
from app.database import get_db
from app.models import Organization, User
from app.plans import PLANS
from app.services.subscription import ACTIVE_STATUSES

router = APIRouter(prefix="/billing", tags=["billing"])


def require_active_subscription(
    org: Organization = Depends(get_current_org),
) -> Organization:
    """Gate for product endpoints. A no-op while billing is disabled (dev)."""
    if billing_enabled() and org.subscription_status not in ACTIVE_STATUSES:
        # Reaches the customer verbatim — the dashboard renders this string
        # in a banner and an inline message, so it has to read like product
        # copy, not like an API reference.
        raise HTTPException(
            status_code=402,
            detail="Julian is paused because there's no active subscription. "
                   "Pick a plan in Settings to start him up again.",
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
    plan: str | None
    lead_limit: int | None
    leads_used: int | None
    leads_remaining: int | None


class CheckoutIn(BaseModel):
    plan: str


class PlanOut(BaseModel):
    id: str
    label: str
    lead_limit: int
    price_gbp: int
    # Same for every plan, but carried per-plan so the picker can label
    # each card without a second request. 0 means no trial.
    trial_days: int


@router.get("/plans", response_model=list[PlanOut])
def plans():
    trial_days = get_settings().trial_period_days
    return [PlanOut(id=p.id, label=p.label, lead_limit=p.lead_limit,
                    price_gbp=p.price_gbp, trial_days=trial_days)
            for p in PLANS.values()]


@router.get("/status", response_model=BillingStatusOut)
def status(org: Organization = Depends(get_current_org), db: Session = Depends(get_db)):
    from app.services.billing_quota import lead_quota_remaining, leads_used_this_period
    plan = PLANS.get(org.plan or "")
    return BillingStatusOut(
        billing_enabled=billing_enabled(),
        subscription_status=org.subscription_status,
        current_period_end=org.current_period_end,
        plan=org.plan,
        lead_limit=plan.lead_limit if plan else None,
        leads_used=leads_used_this_period(db, org) if plan else None,
        leads_remaining=lead_quota_remaining(db, org),
    )


@router.post("/checkout", response_model=CheckoutOut)
def checkout(
    request: CheckoutIn,
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
    stripe: StripeAdapter = Depends(get_stripe_adapter),
):
    """Create a Stripe Checkout session for the given plan (starter/growth/
    scale); open the returned URL to subscribe."""
    if not billing_enabled():
        raise HTTPException(status_code=503, detail="Billing is not configured")
    if org.subscription_status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Subscription is already active")
    if request.plan not in PLANS:
        raise HTTPException(status_code=422,
                            detail=f"Unknown plan {request.plan!r} — choose one of "
                                   f"{', '.join(PLANS)}")
    try:
        url = stripe.create_checkout_session(org.id, user.email, request.plan)
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
            plan = obj.get("metadata", {}).get("plan")
            if plan in PLANS:
                org.plan = plan
            # Provisional — checkout.session doesn't carry the subscription's
            # real status (e.g. "trialing" when a trial period is active).
            # The subscription.created event fired right alongside this one
            # corrects it below; this is just a safe fallback if that event
            # were somehow missed.
            org.subscription_status = "active"
            db.commit()

    elif event_type in ("customer.subscription.created", "customer.subscription.updated",
                        "customer.subscription.deleted"):
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
            plan = obj.get("metadata", {}).get("plan")
            if plan in PLANS:
                org.plan = plan
            period_start = obj.get("current_period_start")
            if period_start:
                org.current_period_start = datetime.fromtimestamp(
                    int(period_start), tz=timezone.utc)
            period_end = obj.get("current_period_end")
            if period_end:
                org.current_period_end = datetime.fromtimestamp(
                    int(period_end), tz=timezone.utc)
            db.commit()

    return {"received": True}


@router.get("/success")
def success():
    base = get_settings().app_base_url.rstrip("/")
    return RedirectResponse(url=f"{base}/app/#/settings?billing=success", status_code=303)


@router.get("/cancelled")
def cancelled():
    base = get_settings().app_base_url.rstrip("/")
    return RedirectResponse(url=f"{base}/app/#/settings?billing=cancelled", status_code=303)
