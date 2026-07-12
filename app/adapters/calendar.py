"""Calendar adapters.

CalendarAdapter is the interface the ScheduleManager depends on.
GoogleCalendarAdapter talks to the Google Calendar REST API (freebusy +
event creation). InMemoryCalendarAdapter is used for local development and
tests when no Google credentials are configured.
"""

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

import httpx

from app.config import get_settings


class CalendarError(Exception):
    pass


class CalendarAdapter(ABC):
    @abstractmethod
    def find_available_slots(
        self, duration_minutes: int, count: int, search_days: int = 7
    ) -> list[tuple[datetime, datetime]]:
        """Return up to `count` free (start, end) slots within business hours."""

    @abstractmethod
    def create_event(
        self, summary: str, start: datetime, end: datetime, attendee_emails: list[str],
        description: str = "",
    ) -> str:
        """Create a calendar event and return its ID."""


def _business_hour_slots(
    busy: list[tuple[datetime, datetime]],
    duration_minutes: int,
    count: int,
    search_days: int,
    now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Walk business hours (9:00–17:00 UTC, weekdays) and pick free slots."""
    now = now or datetime.now(timezone.utc)
    duration = timedelta(minutes=duration_minutes)
    slots: list[tuple[datetime, datetime]] = []

    day = now.date() + timedelta(days=1)
    for _ in range(search_days):
        if day.weekday() < 5:  # Monday–Friday
            cursor = datetime.combine(day, time(9, 0), tzinfo=timezone.utc)
            day_end = datetime.combine(day, time(17, 0), tzinfo=timezone.utc)
            while cursor + duration <= day_end and len(slots) < count:
                candidate = (cursor, cursor + duration)
                overlaps = any(b_start < candidate[1] and b_end > candidate[0]
                               for b_start, b_end in busy)
                if not overlaps:
                    slots.append(candidate)
                    cursor += duration * 2  # space proposals out
                else:
                    cursor += timedelta(minutes=30)
            if len(slots) >= count:
                break
        day += timedelta(days=1)
    return slots


class GoogleCalendarAdapter(CalendarAdapter):
    """Talks to Google Calendar with a per-organization OAuth token.

    `token_provider` is called before each request and must return a live
    access token (refreshing it if needed) — see
    app.adapters.google_oauth.get_valid_access_token.
    """

    def __init__(self, token_provider: Callable[[], str], calendar_id: str = "primary",
                 client: httpx.Client | None = None):
        settings = get_settings()
        self.token_provider = token_provider
        self.calendar_id = calendar_id
        self.base_url = settings.google_calendar_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _headers(self) -> dict[str, str]:
        token = self.token_provider()
        if not token:
            raise CalendarError("No Google Calendar access token available")
        return {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.request(
                method, f"{self.base_url}{path}", json=payload, headers=self._headers()
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CalendarError(
                f"Google Calendar API returned {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise CalendarError(f"Google Calendar request failed: {exc}") from exc
        return response.json()

    def find_available_slots(
        self, duration_minutes: int, count: int, search_days: int = 7
    ) -> list[tuple[datetime, datetime]]:
        now = datetime.now(timezone.utc)
        data = self._request("POST", "/freeBusy", {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=search_days + 1)).isoformat(),
            "items": [{"id": self.calendar_id}],
        })
        busy_raw = data.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])
        busy = [
            (datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
             datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
            for b in busy_raw
        ]
        return _business_hour_slots(busy, duration_minutes, count, search_days, now)

    def create_event(
        self, summary: str, start: datetime, end: datetime, attendee_emails: list[str],
        description: str = "",
    ) -> str:
        data = self._request("POST", f"/calendars/{self.calendar_id}/events", {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": email} for email in attendee_emails],
        })
        return data["id"]


class InMemoryCalendarAdapter(CalendarAdapter):
    """Dev/test calendar: no external calls, remembers events it creates."""

    def __init__(self):
        self.busy: list[tuple[datetime, datetime]] = []
        self.events: list[dict[str, Any]] = []

    def find_available_slots(
        self, duration_minutes: int, count: int, search_days: int = 7
    ) -> list[tuple[datetime, datetime]]:
        return _business_hour_slots(self.busy, duration_minutes, count, search_days)

    def create_event(
        self, summary: str, start: datetime, end: datetime, attendee_emails: list[str],
        description: str = "",
    ) -> str:
        event_id = f"local-{uuid.uuid4().hex[:12]}"
        self.events.append({
            "id": event_id,
            "summary": summary,
            "start": start,
            "end": end,
            "attendees": attendee_emails,
            "description": description,
        })
        self.busy.append((start, end))
        return event_id
