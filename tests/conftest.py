import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sales_agent.db")
os.environ.setdefault("SALES_REP_EMAIL", "rep@example.com")
os.environ.setdefault("SCORE_THRESHOLD", "50")

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.calendar import InMemoryCalendarAdapter  # noqa: E402
from app.adapters.email_sender import EmailSenderAdapter  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.deps import get_calendar_adapter, get_email_sender  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def calendar():
    return InMemoryCalendarAdapter()


@pytest.fixture()
def email_sender():
    return EmailSenderAdapter()


@pytest.fixture()
def client(calendar, email_sender):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_calendar_adapter] = lambda: calendar
    app.dependency_overrides[get_email_sender] = lambda: email_sender
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
