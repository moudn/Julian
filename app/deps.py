"""Adapter wiring.

Real adapters are used when their credentials are configured; the calendar
falls back to an in-memory implementation so the approval workflow can be
exercised locally. Tests override these via FastAPI dependency_overrides.
"""

from functools import lru_cache

from app.adapters.apollo import ApolloAdapter
from app.adapters.calendar import CalendarAdapter, GoogleCalendarAdapter, InMemoryCalendarAdapter
from app.adapters.email_sender import EmailSenderAdapter
from app.adapters.llm import OpenRouterAdapter
from app.config import get_settings


@lru_cache
def get_apollo_adapter() -> ApolloAdapter:
    return ApolloAdapter()


@lru_cache
def get_calendar_adapter() -> CalendarAdapter:
    if get_settings().google_access_token:
        return GoogleCalendarAdapter()
    return InMemoryCalendarAdapter()


@lru_cache
def get_email_sender() -> EmailSenderAdapter:
    return EmailSenderAdapter()


@lru_cache
def get_llm_adapter() -> OpenRouterAdapter:
    return OpenRouterAdapter()
