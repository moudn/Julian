# Deploying Julian to production

Julian ships as a single container (API + dashboard + scheduler). The
recommended first deployment is a managed platform — Railway, Render, or
Fly.io — with managed Postgres. Expect ~$5–20/month to start.

## 1. Prerequisites

- A domain (e.g. `yourproduct.com`) — the app will live at
  `https://app.yourproduct.com` or similar
- A Postgres database (the platform provides one)
- Your API credentials: Google OAuth client, Stripe keys, OpenRouter key

## 2. Environment variables (production values)

| Variable | Value |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:pass@host:5432/dbname` (from your platform) |
| `SECRET_KEY` | long random string — `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ENCRYPTION_KEY` | Fernet key — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **store a copy somewhere safe; losing it orphans all Google connections** |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from Google Cloud |
| `GOOGLE_REDIRECT_URI` | `https://YOUR-DOMAIN/integrations/google/callback` |
| `OPENROUTER_API_KEY` | from openrouter.ai |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` / `STRIPE_WEBHOOK_SECRET` | from Stripe (test mode until launch) |
| `SMTP_*` | from Resend/SendGrid (rep notifications + password resets) |
| `SCHEDULER_ENABLED` | `true` |

Never commit any of these. Set them in the platform's dashboard.

## 3. Platform setup (Railway example; Render/Fly are near-identical)

1. Create a project → "Deploy from GitHub repo" → select the Julian repo.
   The `Dockerfile` is detected automatically; `start.sh` runs migrations
   then the server.
2. Add the Postgres plugin; copy its URL into `DATABASE_URL`
   (change the scheme to `postgresql+psycopg://`).
3. Set every variable from the table above.
4. Add your custom domain; the platform issues HTTPS automatically.
5. Deploy. Check `https://YOUR-DOMAIN/health` returns `{"status":"ok"}`.

## 4. Third-party console updates (easy to forget)

- **Google Cloud → Credentials → your OAuth client**: add the production
  redirect URI (`https://YOUR-DOMAIN/integrations/google/callback`).
  Keep the localhost one for development.
- **Google Cloud → OAuth consent screen**: add your domain; keep the app
  in Testing (100-user cap) until you pursue verification.
- **Stripe → Developers → Webhooks**: add endpoint
  `https://YOUR-DOMAIN/billing/webhook` (events: `checkout.session.completed`,
  `customer.subscription.updated`, `customer.subscription.deleted`), then
  copy the signing secret into `STRIPE_WEBHOOK_SECRET`.

## 5. Scaling constraints (read before adding replicas)

Run **one instance with one worker** until these are addressed:

- The send/reply scheduler runs in-process. Postgres row locking
  (`FOR UPDATE SKIP LOCKED`) protects against double-sends, but the
  cleanest multi-instance setup is one dedicated worker instance with
  `SCHEDULER_ENABLED=true` and web instances with it `false`.
- The auth rate limiter is in-memory (per process). Move to Redis before
  horizontal scaling matters.

A single small instance comfortably serves the first dozens of customers.

## 6. Production checklist

- [ ] `SECRET_KEY` and `ENCRYPTION_KEY` are long, random, and backed up
- [ ] Database backups enabled (platform setting; daily is fine to start)
- [ ] `https://YOUR-DOMAIN/health` monitored by an uptime service
  (UptimeRobot free tier works)
- [ ] Error tracking: create a free Sentry project and add
  `sentry-sdk[fastapi]` when ready (optional at first)
- [ ] Stripe in test mode until the legal documents are published
- [ ] Legal pages published and linked (see `docs/legal/`) before real
  customer signups
- [ ] Verify one full journey in production: signup → connect Google →
  import lead → activate → simulated reply → approve booking

## 7. Local production-like run

```bash
cp .env.example .env   # fill in secrets
docker compose up --build
# app on http://localhost:8000, Postgres included
```
