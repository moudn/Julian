# Julian — AI Sales Agent Core

Backend service for an AI sales agent: lead import, rule-based ICP scoring,
LLM-personalized outreach drafts, and a **human-approved** meeting scheduling
workflow. Built with FastAPI + SQLAlchemy (SQLite by default).

## Lead lifecycle

```
NEW -> SCORED -> OUTREACH_PENDING -> MEETING_PROPOSED -> AWAITING_APPROVAL -> MEETING_CONFIRMED
```

Transitions are enforced by a state machine (`app/state_machine.py`); states
cannot be skipped. `AWAITING_APPROVAL` may fall back to `MEETING_PROPOSED`
when the sales rep rejects a booking.

### The approval guarantee

**No calendar event is ever created without an explicit approval call.**
When a lead picks a meeting slot, the system only records a `PendingBooking`
and emails the sales rep. The Google Calendar `create_event` call lives
exclusively inside `ScheduleManager.approve_booking`, which is reachable only
via `POST /approve_booking/{id}`. This invariant is covered by tests in
`tests/test_schedule_workflow.py`.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in API keys as available
uvicorn app.main:app --reload
```

Interactive API docs: http://127.0.0.1:8000/docs

Without API keys the service still runs end-to-end: the LLM adapter falls
back to a deterministic template, the calendar adapter falls back to an
in-memory implementation, and emails are logged to the console.

## Walkthrough

```bash
# 1. Import leads from CSV (headers: name,email,company,title,company_size,location,...)
curl -F "file=@leads.csv" http://127.0.0.1:8000/leads/import

# 2. Define the ICP (admin). Matching rules add their weight to the score;
#    leads at/above SCORE_THRESHOLD (default 50) move NEW -> SCORED.
curl -X POST http://127.0.0.1:8000/icp/rules -H 'Content-Type: application/json' \
  -d '{"name":"Senior title","field":"title","operator":"in","value":["VP","Director"],"weight":30}'
curl -X POST http://127.0.0.1:8000/icp/rules -H 'Content-Type: application/json' \
  -d '{"name":"Mid-size","field":"company_size","operator":"gte","value":100,"weight":30}'
curl -X POST http://127.0.0.1:8000/leads/score_all

# 3. Generate a personalized first-touch draft (OpenRouter) -> OUTREACH_PENDING
curl -X POST http://127.0.0.1:8000/leads/1/generate_message

# 4. Agent reads the rep's calendar and proposes 2-3 slots -> MEETING_PROPOSED
curl -X POST http://127.0.0.1:8000/leads/1/propose_meeting \
  -H 'Content-Type: application/json' -d '{"duration_minutes":30,"slot_count":3}'

# 5. Lead picks a slot -> PendingBooking created, rep notified -> AWAITING_APPROVAL
#    (NO calendar event yet)
curl -X POST http://127.0.0.1:8000/leads/1/select_slot \
  -H 'Content-Type: application/json' -d '{"slot_start":"2026-07-13T09:00:00+00:00"}'

# 6. Rep reviews pending bookings and approves -> calendar event + confirmation
#    email -> MEETING_CONFIRMED
curl http://127.0.0.1:8000/bookings/pending
curl -X POST http://127.0.0.1:8000/approve_booking/1
# ...or rejects, returning the lead to MEETING_PROPOSED:
curl -X POST http://127.0.0.1:8000/bookings/1/reject
```

### Apollo.io

```bash
# Search people by filters (optionally save results as leads)
curl -X POST http://127.0.0.1:8000/apollo/search_people \
  -H 'Content-Type: application/json' \
  -d '{"titles":["CTO"],"locations":["California"],"per_page":5,"save_to_db":true}'

# Enrich a person by name + company domain; creates/updates the Lead
curl -X POST http://127.0.0.1:8000/apollo/enrich_person \
  -H 'Content-Type: application/json' -d '{"name":"Grace Hopper","domain":"navy.mil"}'
```

## Project layout

```
app/
  config.py                Settings (env / .env)
  database.py              Engine, session, init
  models.py                Lead, ICPRule, PendingBooking + state enums
  state_machine.py         Allowed lead-state transitions
  deps.py                  Adapter wiring / DI
  adapters/
    apollo.py              ApolloAdapter: search_people, enrich_person
    calendar.py            CalendarAdapter (Google REST + in-memory fallback)
    email_sender.py        EmailSenderAdapter (SMTP or console)
    llm.py                 OpenRouterAdapter (template fallback w/o key)
  services/
    leads.py               CSV import + lead upsert
    scoring.py             Rule-based ICP scoring
    schedule_manager.py    propose_meeting -> select_slot -> approve/reject
  routers/                 leads, icp, apollo, bookings endpoints
tests/                     pytest suite (17 tests)
```

## Configuration

See `.env.example`. Key variables: `DATABASE_URL`, `SCORE_THRESHOLD`,
`APOLLO_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_ACCESS_TOKEN`,
`GOOGLE_CALENDAR_ID`, `SALES_REP_EMAIL`, `SMTP_*`.

## Tests

```bash
python -m pytest tests/ -q
```
