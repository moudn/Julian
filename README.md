# Julian — AI Sales Agent Core

Multi-tenant backend service for an AI sales agent: lead import, rule-based
ICP scoring, LLM-personalized outreach drafts, and a **human-approved**
meeting scheduling workflow. Built with FastAPI + SQLAlchemy (SQLite by
default). Each customer signs up as an organization, gets an API key, and
connects their own Google Calendar via OAuth — data is fully isolated
between organizations.

## Lead lifecycle

```
NEW -> SCORED -> OUTREACH_PENDING -> SEQUENCE_ACTIVE -> ENGAGED
    -> MEETING_PROPOSED -> AWAITING_APPROVAL -> MEETING_CONFIRMED
```

`SEQUENCE_ACTIVE` is autopilot: after one activation call the scheduler
sends the 4-step sequence on cadence (0/3/7/12 days) from the tenant's own
Gmail, and stops the moment the lead leaves the state (reply, unsubscribe,
booking). `ENGAGED` means a human owns the conversation. `NOT_INTERESTED`
and `UNSUBSCRIBED` are terminal and never receive mail. Transitions are
enforced by a state machine (`app/state_machine.py`); states cannot be
skipped. `AWAITING_APPROVAL` may fall back to `MEETING_PROPOSED` when the
sales rep rejects a booking.

Before writing, Julian **researches each lead** (if enabled): it reads the
company website and searches recent news, then the LLM distills citable
factual bullets stored on the lead (`research_notes`). Those facts flow
into every draft so the opener references something real. Runs
automatically at sequence generation, or on demand via
`POST /leads/{id}/research`. Website fetches are SSRF-guarded and all
gathered web content is treated as untrusted (prompt-injection safe). The
news step uses a Tavily key (`SEARCH_API_KEY`); without one, research falls
back to the website alone. Toggle per org (`research_enabled`) or globally
(`RESEARCH_ENABLED`).

Outreach flow: `POST /leads/{id}/generate_sequence` (research-backed 4-step
drafts) -> review -> `POST /leads/{id}/activate_sequence` (one approval,
then autopilot). `POST /scheduler/run` triggers a send cycle manually; a
background loop does it automatically every `SCHEDULER_INTERVAL_SECONDS`.

Replies are polled from the connected Gmail (or fed via
`POST /replies/ingest`) and triaged: opt-outs and "not interested" end the
sequence with no human involvement (keyword-detected, never
LLM-dependent); out-of-office postpones it; an interested reply makes
Julian propose 2-3 real calendar slots immediately; questions are answered
by Julian only from the org's pre-approved `knowledge_base`; everything
else hands off to the rep with a suggested reply ready to send. A reply
picking a slot ("option 2", "Wednesday works") creates the PendingBooking
— the calendar event still only exists after `POST /approve_booking/{id}`.
`GET /leads/{id}/conversation` shows the full thread.

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
# 0. Create your organization and get an API key (shown once — store it!)
curl -X POST http://127.0.0.1:8000/auth/signup -H 'Content-Type: application/json' \
  -d '{"organization_name":"Acme","name":"You","email":"you@acme.com","password":"a-strong-pass"}'
export KEY="sk_..."   # every request below sends: -H "Authorization: Bearer $KEY"

# Optional: set where booking-approval emails go (defaults to your signup email)
curl -X PATCH http://127.0.0.1:8000/auth/org -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"sales_rep_email":"rep@acme.com"}'

# Optional: connect your Google Calendar (needs GOOGLE_CLIENT_ID/SECRET configured)
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/integrations/google/connect
# -> open the returned authorize_url in a browser and approve

# 1. Import leads from CSV (headers: name,email,company,title,company_size,location,...)
curl -H "Authorization: Bearer $KEY" -F "file=@leads.csv" http://127.0.0.1:8000/leads/import

# 2. Define the ICP (admin). Matching rules add their weight to the score;
#    leads at/above your org's score_threshold (default 50) move NEW -> SCORED.
curl -X POST http://127.0.0.1:8000/icp/rules -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Senior title","field":"title","operator":"in","value":["VP","Director"],"weight":30}'
curl -X POST http://127.0.0.1:8000/icp/rules -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Mid-size","field":"company_size","operator":"gte","value":100,"weight":30}'
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/leads/score_all

# 3. Generate a personalized first-touch draft (OpenRouter) -> OUTREACH_PENDING
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/leads/1/generate_message

# 4. Agent reads the rep's calendar and proposes 2-3 slots -> MEETING_PROPOSED
curl -X POST http://127.0.0.1:8000/leads/1/propose_meeting -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"duration_minutes":30,"slot_count":3}'

# 5. Lead picks a slot -> PendingBooking created, rep notified -> AWAITING_APPROVAL
#    (NO calendar event yet)
curl -X POST http://127.0.0.1:8000/leads/1/select_slot -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"slot_start":"2026-07-13T09:00:00+00:00"}'

# 6. Rep reviews pending bookings and approves -> calendar event + confirmation
#    email -> MEETING_CONFIRMED
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/bookings/pending
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/approve_booking/1
# ...or rejects, returning the lead to MEETING_PROPOSED:
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/bookings/1/reject
```

### Apollo.io

```bash
# Search people by filters (optionally save results as leads)
curl -X POST http://127.0.0.1:8000/apollo/search_people -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"titles":["CTO"],"locations":["California"],"per_page":5,"save_to_db":true}'

# Enrich a person by name + company domain; creates/updates the Lead
curl -X POST http://127.0.0.1:8000/apollo/enrich_person -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"name":"Grace Hopper","domain":"navy.mil"}'
```

## Google Calendar (per-customer OAuth)

One-time setup for you as the operator, free of charge:

1. Create a project at console.cloud.google.com and enable the
   **Google Calendar API**.
2. Configure the OAuth consent screen (External, add your email as a test
   user while unverified).
3. Create an **OAuth client ID** (type: Web application) with redirect URI
   `http://localhost:8000/integrations/google/callback` (add your production
   URL later), and put the client ID/secret in `.env`.

Each customer then calls `GET /integrations/google/connect`, opens the
returned URL, and approves — the refresh token is stored per organization
and access tokens are refreshed automatically.

## Billing (Stripe subscriptions)

With `STRIPE_SECRET_KEY` unset, billing is disabled and every endpoint is
open (development mode). To charge customers:

1. In the Stripe dashboard (test mode first): create a Product with a
   recurring Price, copy the `price_...` id.
2. Set `STRIPE_SECRET_KEY` (sk_test_...), `STRIPE_PRICE_ID`, and
   `STRIPE_WEBHOOK_SECRET` in `.env`. For local webhook testing install the
   Stripe CLI and run
   `stripe listen --forward-to localhost:8000/billing/webhook` — it prints
   the `whsec_...` secret.
3. Once billing is enabled, product endpoints return **402** until the org
   subscribes:
   - `POST /billing/checkout` → returns a Stripe Checkout URL (test card:
     4242 4242 4242 4242, any future date / CVC)
   - Stripe webhooks flip the org to `active` and keep the status in sync
     (`past_due`, `canceled`, ...)
   - `GET /billing/status` shows the current state;
     `POST /billing/portal` returns a Customer Portal link for
     managing/cancelling

## Project layout

```
app/
  config.py                Settings (env / .env)
  database.py              Engine, session, init
  models.py                Organization, User, ApiKey, GoogleCredential,
                           Lead, ICPRule, PendingBooking + state enums
  auth.py                  Password hashing, API keys, current-org dependency
  state_machine.py         Allowed lead-state transitions
  deps.py                  Adapter wiring / DI (per-org calendar selection)
  adapters/
    apollo.py              ApolloAdapter: search_people, enrich_person
    calendar.py            CalendarAdapter (Google REST + in-memory fallback)
    google_oauth.py        OAuth consent URL, code exchange, token refresh
    email_sender.py        EmailSenderAdapter (SMTP or console)
    llm.py                 OpenRouterAdapter (template fallback w/o key)
  services/
    leads.py               CSV import + lead upsert (org-scoped)
    scoring.py             Rule-based ICP scoring (org rules + threshold)
    schedule_manager.py    propose_meeting -> select_slot -> approve/reject
  routers/                 auth, integrations, leads, icp, apollo, bookings
tests/                     pytest suite (30 tests)
```

## Configuration

See `.env.example`. Key variables: `DATABASE_URL`, `SECRET_KEY`,
`SCORE_THRESHOLD`, `APOLLO_API_KEY`, `OPENROUTER_API_KEY`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `SMTP_*`.
Sales-rep email and scoring threshold are per-organization settings
(`PATCH /auth/org`), not global env vars.

## Security & compliance hardening

- Google OAuth uses single-use, 10-minute state tokens (account-linking
  CSRF protection); OAuth tokens are encrypted at rest (`ENCRYPTION_KEY`)
- Login/signup/reset endpoints are rate-limited; password reset via
  emailed one-hour tokens (`/auth/forgot_password`, `/auth/reset_password`);
  API keys can be listed and revoked (`/auth/keys`)
- Deliverability guardrails: per-org daily send caps with new-account
  ramp-up, jitter, and org-local business-hours sending
  (`SCHEDULER`/`ENFORCE_SEND_WINDOW` settings); per-org IANA `timezone`
  drives sending windows and meeting-slot proposals
- Compliance: activation requires an email footer (opt-out + postal
  address); opt-outs land on a permanent per-org suppression list that
  blocks re-import and re-sending; `DELETE /leads/{id}` erases a lead
  (GDPR right to erasure, suppresses by default);
  `GET /leads/{id}/export` provides data-subject access; email bodies are
  kept out of server logs
- Julian never auto-sends knowledge-base answers unless the org opts in
  (`auto_reply_enabled`); the classifier treats reply content as untrusted
  (prompt-injection hardening); bookings re-check calendar availability at
  approval time
- Security headers (CSP, nosniff, frame-deny) on all responses; CSV
  imports capped at 2 MB / 5,000 rows; lead listing is paginated with
  `GET /leads/stats` for dashboard counts

## Database migrations

Schema is managed with Alembic (`migrations/`). For a new database:
`alembic upgrade head`. After changing models:
`alembic revision --autogenerate -m "describe change"` then review and
`alembic upgrade head`. (Dev/tests still auto-create tables.)

## Tests

```bash
python -m pytest tests/ -q
```
