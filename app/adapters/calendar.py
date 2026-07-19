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
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


class CalendarError(Exception):
    pass


def safe_zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


class CalendarAdapter(ABC):
    @abstractmethod
    def find_available_slots(
        self, duration_minutes: int, count: int, search_days: int = 7,
        tz_name: str = "UTC",
    ) -> list[tuple[datetime, datetime]]:
        """Return up to `count` free (start, end) slots within business hours."""

    @abstractmethod
    def create_event(
        self, summary: str, start: datetime, end: datetime, attendee_emails: list[str],
        description: str = "",
    ) -> str:
        """Create a calendar event and return its ID."""

    @abstractmethod
    def is_slot_free(self, start: datetime, end: datetime) -> bool:
        """Re-check availability (used at approval time)."""


def _business_hour_slots(
    busy: list[tuple[datetime, datetime]],
    duration_minutes: int,
    count: int,
    search_days: int,
    now: datetime | None = None,
    tz_name: str = "UTC",
) -> list[tuple[datetime, datetime]]:
    """Walk the org's business hours (9:00–17:00 local, weekdays) and pick
    free slots. Returned datetimes are timezone-aware in the org's zone."""
    zone = safe_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    duration = timedelta(minutes=duration_minutes)
    slots: list[tuple[datetime, datetime]] = []

    day = now.date() + timedelta(days=1)
    for _ in range(search_days):
        if day.weekday() < 5:  # Monday–Friday
            cursor = datetime.combine(day, time(9, 0), tzinfo=zone)
            day_end = datetime.combine(day, time(17, 0), tzinfo=zone)
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

    def _busy_between(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        data = self._request("POST", "/freeBusy", {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self.calendar_id}],
        })
        busy_raw = data.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])
        return [
            (datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
             datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
            for b in busy_raw
        ]

    def find_available_slots(
        self, duration_minutes: int, count: int, search_days: int = 7,
        tz_name: str = "UTC",
    ) -> list[tuple[datetime, datetime]]:
        now = datetime.now(timezone.utc)
        busy = self._busy_between(now, now + timedelta(days=search_days + 1))
        return _business_hour_slots(busy, duration_minutes, count, search_days,
                                    now, tz_name)

    def is_slot_free(self, start: datetime, end: datetime) -> bool:
        busy = self._busy_between(start, end)
        return not any(b_start < end and b_end > start for b_start, b_end in busy)

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
        self, duration_minutes: int, count: int, search_days: int = 7,
        tz_name: str = "UTC",
    ) -> list[tuple[datetime, datetime]]:
        return _business_hour_slots(self.busy, duration_minutes, count,
                                    search_days, tz_name=tz_name)

    def is_slot_free(self, start: datetime, end: datetime) -> bool:
        return not any(b_start < end and b_end > start
                       for b_start, b_end in self.busy)

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
