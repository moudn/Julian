import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sales_agent.db")
os.environ.setdefault("SCORE_THRESHOLD", "50")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ENFORCE_SEND_WINDOW", "false")
# Research makes outbound HTTP; off by default so tests never hit the network.
# Research-specific tests re-enable it with a mocked researcher.
os.environ.setdefault("RESEARCH_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.calendar import InMemoryCalendarAdapter  # noqa: E402
from app.adapters.email_sender import EmailSenderAdapter  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.deps import get_calendar_adapter, get_email_sender  # noqa: E402
from app.main import app  # noqa: E402


def signup(client: TestClient, org_name="Acme Sales", email="owner@acme-sales.io",
           rep_email="rep@example.com", verify=True) -> str:
    """Create an org + user, set the rep email, and return the API key.

    By default the user's email is marked verified (most tests exercise the
    happy path); pass verify=False to test the unverified gate.
    """
    response = client.post("/auth/signup", json={
        "organization_name": org_name,
        "name": "Owner",
        "email": email,
        "password": "s3cretpass!",
    })
    assert response.status_code == 201, response.text
    api_key = response.json()["api_key"]
    if verify:
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(email=email).one()
            user.email_verified = True
            db.commit()
        finally:
            db.close()
    response = client.patch(
        "/auth/org",
        json={
            "sales_rep_email": rep_email,
            "email_footer": "\n--\nAcme Sales, 1 Test Street, Testville. "
                            "Reply \"no thanks\" to opt out.",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 200, response.text
    return api_key


@pytest.fixture()
def calendar():
    return InMemoryCalendarAdapter()


@pytest.fixture()
def email_sender():
    return EmailSenderAdapter()


@pytest.fixture()
def anon_client(calendar, email_sender):
    """Client with a fresh database and no credentials attached."""
    from app.deps import _dev_calendars
    from app.security import _buckets
    _dev_calendars.clear()
    _buckets.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_calendar_adapter] = lambda: calendar
    app.dependency_overrides[get_email_sender] = lambda: email_sender
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def client(anon_client):
    """Client authenticated as the default test organization."""
    api_key = signup(anon_client)
    anon_client.headers.update({"Authorization": f"Bearer {api_key}"})
    return anon_client
