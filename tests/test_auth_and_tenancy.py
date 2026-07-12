"""Auth and multi-tenant isolation tests.

The core guarantee: one organization can never see or act on another
organization's leads, rules, or bookings.
"""

import io

from tests.conftest import signup

CSV = "name,email,company,title\nAda Lovelace,ada@acme.io,Acme,VP of Engineering\n"


def _import_lead(client, headers=None):
    return client.post(
        "/leads/import",
        files={"file": ("l.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=headers or {},
    )


def test_endpoints_require_auth(anon_client):
    assert anon_client.get("/leads").status_code == 401
    assert anon_client.post("/icp/rules", json={}).status_code == 401
    assert anon_client.get("/bookings/pending").status_code == 401
    assert anon_client.post("/approve_booking/1").status_code == 401
    assert anon_client.get("/integrations/google/connect").status_code == 401


def test_invalid_key_rejected(anon_client):
    response = anon_client.get(
        "/leads", headers={"Authorization": "Bearer sk_not-a-real-key"}
    )
    assert response.status_code == 401


def test_signup_login_me(anon_client):
    api_key = signup(anon_client)
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {api_key}"})
    assert me.status_code == 200
    assert me.json()["name"] == "Acme Sales"
    assert me.json()["sales_rep_email"] == "rep@example.com"

    # login mints a fresh, different key that also works
    login = anon_client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "s3cretpass!",
    })
    assert login.status_code == 200
    new_key = login.json()["api_key"]
    assert new_key != api_key
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {new_key}"})
    assert me.status_code == 200


def test_login_wrong_password_rejected(anon_client):
    signup(anon_client)
    response = anon_client.post("/auth/login", json={
        "email": "owner@acme-sales.io", "password": "wrong-password",
    })
    assert response.status_code == 401


def test_duplicate_signup_email_rejected(anon_client):
    signup(anon_client)
    response = anon_client.post("/auth/signup", json={
        "organization_name": "Other", "name": "X",
        "email": "owner@acme-sales.io", "password": "s3cretpass!",
    })
    assert response.status_code == 409


def test_orgs_cannot_see_each_others_data(anon_client):
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    assert _import_lead(anon_client, headers_a).json()["imported"] == 1
    lead_id = anon_client.get("/leads", headers=headers_a).json()[0]["id"]

    # Org B sees an empty list and cannot access A's lead directly
    assert anon_client.get("/leads", headers=headers_b).json() == []
    assert anon_client.get(f"/leads/{lead_id}", headers=headers_b).status_code == 404
    assert anon_client.post(f"/leads/{lead_id}/score", headers=headers_b).status_code == 404

    # Both orgs can hold a lead with the same email (per-org uniqueness)
    assert _import_lead(anon_client, headers_b).json()["imported"] == 1

    # Org B's ICP rules don't affect Org A's scoring
    anon_client.post("/icp/rules", headers=headers_b, json={
        "name": "B rule", "field": "title", "operator": "contains",
        "value": "VP", "weight": 100,
    })
    score = anon_client.post(f"/leads/{lead_id}/score", headers=headers_a).json()
    assert score["score"] == 0


def test_org_cannot_approve_other_orgs_booking(anon_client, calendar):
    key_a = signup(anon_client, org_name="Org A", email="a@org-a.io")
    key_b = signup(anon_client, org_name="Org B", email="b@org-b.io")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    _import_lead(anon_client, headers_a)
    anon_client.post("/icp/rules", headers=headers_a, json={
        "name": "VP", "field": "title", "operator": "contains",
        "value": "VP", "weight": 60,
    })
    lead_id = anon_client.get("/leads", headers=headers_a).json()[0]["id"]
    anon_client.post(f"/leads/{lead_id}/score", headers=headers_a)
    anon_client.post(f"/leads/{lead_id}/generate_message", headers=headers_a)
    slots = anon_client.post(f"/leads/{lead_id}/propose_meeting",
                             headers=headers_a, json={}).json()["slots"]
    booking_id = anon_client.post(
        f"/leads/{lead_id}/select_slot", headers=headers_a,
        json={"slot_start": slots[0]},
    ).json()["id"]

    # Org B cannot see or approve Org A's pending booking
    assert anon_client.get("/bookings/pending", headers=headers_b).json() == []
    assert anon_client.post(f"/approve_booking/{booking_id}",
                            headers=headers_b).status_code == 404
    assert calendar.events == []  # nothing was booked by the failed attempt

    # Org A approves it fine
    assert anon_client.post(f"/approve_booking/{booking_id}",
                            headers=headers_a).status_code == 200
    assert len(calendar.events) == 1
