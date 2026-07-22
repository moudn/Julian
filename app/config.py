from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sales_agent.db"

    # Signs password-reset tokens; set a long random value in production
    secret_key: str = "dev-secret-change-me"
    # Fernet key for encrypting OAuth tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Empty = derived from secret_key (acceptable only in development).
    encryption_key: str = ""

    # Scoring default for new organizations
    score_threshold: int = 50

    # Apollo.io
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/v1"

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-4.5"

    # Lead research (company website + news search before writing).
    # Global kill-switch; each org also has its own research_enabled toggle.
    research_enabled: bool = True
    research_timeout_seconds: int = 10
    # Web search for company news. Tavily by default (free tier); empty key
    # disables the news step and research falls back to the website only.
    search_api_key: str = ""
    search_base_url: str = "https://api.tavily.com"

    # Google Calendar OAuth app (create at console.cloud.google.com)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/integrations/google/callback"
    google_calendar_base_url: str = "https://www.googleapis.com/calendar/v3"
    google_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    google_oauth_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    gmail_api_base: str = "https://gmail.googleapis.com/gmail/v1"

    # Sequence send scheduler (background loop). Interval in seconds;
    # scheduler_enabled=false relies on POST /scheduler/run (cron) instead.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 60
    # Only send within org-local business hours (disable for testing only)
    enforce_send_window: bool = True

    # Stripe billing — leave stripe_secret_key empty to disable billing
    # entirely (all endpoints open; good for development)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""
    stripe_api_base: str = "https://api.stripe.com/v1"
    billing_success_url: str = "http://localhost:8000/billing/success"
    billing_cancel_url: str = "http://localhost:8000/billing/cancelled"

    # Error tracking (optional). Set a Sentry DSN to enable.
    sentry_dsn: str = ""
    environment: str = "development"

    # SMTP — if smtp_host is empty, emails are logged instead of sent
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "agent@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
