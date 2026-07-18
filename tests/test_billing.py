"""Stripe billing: webhook verification, subscription gating, checkout flow."""

import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.adapters import stripe_billing
from app.adapters.stripe_billing import (
    StripeAdapter,
    StripeError,
    WebhookVerificationError,
    verify_webhook_signature,
)
from app.config import get_settings

WEBHOOK_SECRET = "whsec_testsecret"


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    timestamp = timestamp or int(time.time())
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


@pytest.fixture()
def billing_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "stripe_price_id", "price_123")
    return settings


# ---------- signature verification ----------

def test_webhook_signature_roundtrip(billing_on):
    payload = json.dumps({"type": "x", "data": {}}).encode()
    event = verify_webhook_signature(payload, sign(payload))
    assert event["type"] == "x"


def test_webhook_rejects_bad_signature(billing_on):
    payload = b'{"type":"x"}'
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_webhook_signature(payload, sign(b'{"type":"tampered"}'))


def test_webhook_rejects_stale_timestamp(billing_on):
    payload = b'{"type":"x"}'
    stale = sign(payload, timestamp=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_webhook_signature(payload, stale)


def test_webhook_rejects_malformed_header(billing_on):
    with pytest.raises(WebhookVerificationError, match="Malformed"):
        verify_webhook_signature(b"{}", "not-a-real-header")


# ---------- gating ----------

def test_billing_disabled_leaves_endpoints_open(client):
    # default test settings have no stripe key -> everything works
    assert client.get("/leads").status_code == 200
    status = client.get("/billing/status").json()
    assert status["billing_enabled"] is False


def test_endpoints_gated_without_subscription(client, billing_on):
    response = client.get("/leads")
    assert response.status_code == 402
    assert "subscription" in response.json()["detail"].lower()
    # auth + billing endpoints stay reachable so the org can subscribe
    assert client.get("/auth/me").status_code == 200
    assert client.get("/billing/status").status_code == 200


def test_checkout_session_flow_unlocks_endpoints(client, billing_on):
    org_id = client.get("/auth/me").json()["id"]

    # webhook: checkout completed for this org
    payload = json.dumps({
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(org_id),
            "customer": "cus_123",
            "subscription": "sub_123",
        }},
    }).encode()
    response = client.post("/billing/webhook", content=payload,
                           headers={"Stripe-Signature": sign(payload)})
    assert response.status_code == 200

    assert client.get("/billing/status").json()["subscription_status"] == "active"
    assert client.get("/leads").status_code == 200  # unlocked

    # subscription cancelled -> locked again
    payload = json.dumps({
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_123", "customer": "cus_123"}},
    }).encode()
    response = client.post("/billing/webhook", content=payload,
                           headers={"Stripe-Signature": sign(payload)})
    assert response.status_code == 200
    assert client.get("/billing/status").json()["subscription_status"] == "canceled"
    assert client.get("/leads").status_code == 402


def test_webhook_rejects_unsigned_requests(client, billing_on):
    payload = json.dumps({"type": "checkout.session.completed",
                          "data": {"object": {"client_reference_id": "1"}}}).encode()
    response = client.post("/billing/webhook", content=payload)
    assert response.status_code == 400
    # and the org was NOT activated by the unsigned request
    assert client.get("/billing/status").json()["subscription_status"] == "none"


def test_subscription_updated_syncs_status(client, billing_on):
    org_id = client.get("/auth/me").json()["id"]
    payload = json.dumps({
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(org_id),
                            "customer": "cus_9", "subscription": "sub_9"}},
    }).encode()
    client.post("/billing/webhook", content=payload,
                headers={"Stripe-Signature": sign(payload)})

    period_end = int(time.time()) + 30 * 86400
    payload = json.dumps({
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_9", "customer": "cus_9",
                            "status": "past_due",
                            "current_period_end": period_end}},
    }).encode()
    client.post("/billing/webhook", content=payload,
                headers={"Stripe-Signature": sign(payload)})

    status = client.get("/billing/status").json()
    assert status["subscription_status"] == "past_due"
    assert status["current_period_end"] is not None
    assert client.get("/leads").status_code == 402  # past_due is not active


# ---------- checkout endpoint / adapter ----------

def test_checkout_returns_stripe_url(client, billing_on, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"url": "https://checkout.stripe.com/pay/cs_test"})

    from app.main import app
    from app.routers.billing import get_stripe_adapter
    adapter = StripeAdapter(api_key="sk_test_123",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    app.dependency_overrides[get_stripe_adapter] = lambda: adapter

    response = client.post("/billing/checkout")
    assert response.status_code == 200
    assert response.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    assert captured["path"].endswith("/checkout/sessions")
    assert "mode=subscription" in captured["body"]
    assert "price_123" in captured["body"]


def test_checkout_requires_billing_enabled(client):
    assert client.post("/billing/checkout").status_code == 503


def test_adapter_requires_configuration():
    adapter = StripeAdapter(api_key="", client=httpx.Client())
    with pytest.raises(StripeError, match="not configured"):
        adapter.create_checkout_session(1, "a@b.c")
