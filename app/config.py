from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sales_agent.db"

    # Scoring
    score_threshold: int = 50

    # Apollo.io
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/v1"

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-4.5"

    # Google Calendar
    google_calendar_id: str = "primary"
    google_access_token: str = ""
    google_calendar_base_url: str = "https://www.googleapis.com/calendar/v3"

    # Notifications
    sales_rep_email: str = "rep@example.com"

    # SMTP — if smtp_host is empty, emails are logged instead of sent
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "agent@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
