"""Adapter wiring.

The calendar adapter is chosen per organization: a real Google Calendar
client when the org has connected Google via OAuth, otherwise an in-memory
calendar (kept per org) so the workflow stays fully exercisable in
development. Tests override these via FastAPI dependency_overrides.
"""

from functools import lru_cache

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.apollo import ApolloAdapter
from app.adapters.calendar import (
    CalendarAdapter,
    GoogleCalendarAdapter,
    InMemoryCalendarAdapter,
)
from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.google_oauth import get_valid_access_token
from app.adapters.llm import OpenRouterAdapter
from app.auth import get_current_org
from app.database import get_db
from app.models import GoogleCredential, Organization

_dev_calendars: dict[int, InMemoryCalendarAdapter] = {}


@lru_cache
def get_apollo_adapter() -> ApolloAdapter:
    return ApolloAdapter()


def get_calendar_adapter(
    org: Organization = Depends(get_current_org),
    db: Session = Depends(get_db),
) -> CalendarAdapter:
    credential = db.scalar(
        select(GoogleCredential).where(GoogleCredential.org_id == org.id)
    )
    if credential is not None:
        return GoogleCalendarAdapter(
            token_provider=lambda: get_valid_access_token(db, credential),
            calendar_id=credential.calendar_id,
        )
    return _dev_calendars.setdefault(org.id, InMemoryCalendarAdapter())


@lru_cache
def get_email_sender() -> EmailSenderAdapter:
    return EmailSenderAdapter()


@lru_cache
def get_llm_adapter() -> OpenRouterAdapter:
    return OpenRouterAdapter()
