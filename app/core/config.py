from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/receipt_app"

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg_driver(cls, v: str) -> str:
        # Managed Postgres providers (e.g. Render) hand out postgres:// or
        # postgresql:// URLs; SQLAlchemy's async engine needs the asyncpg driver.
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440

    # Gmail API (OAuth refresh-token flow); see scripts/gmail_authorize.py.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_sender_email: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    app_base_url: str = "http://localhost:8000"
    storage_dir: str = "./static"

    # Comma-separated list of allowed browser origins for the frontend portal,
    # e.g. "http://localhost:5173,https://portal.example.com".
    cors_allow_origins_raw: str = "http://localhost:5173"

    # Regex for browser origins to allow beyond the explicit list above, e.g. to
    # cover Vercel's per-deploy preview URLs, which change on every deploy and
    # can't be pinned to a single origin. Empty disables the regex match.
    cors_allow_origin_regex: str = (
        r"^https://recieptappportal(-[a-z0-9]+)?-mmadiba724s-projects\.vercel\.app$"
    )

    # Opt-in seeding of pre-verified users on startup (no email API needed).
    # SEED_DEFAULT_USERS is a JSON list, e.g.
    # [{"email": "admin@example.com", "password": "...", "full_name": "Admin",
    #   "business_name": "Demo Business", "role": "owner"}]
    seed_default_users_enabled: bool = False
    seed_default_users: str = ""

    # Application logging (see app/core/logging.py). LOG_JSON=true emits one JSON object per line.
    log_level: str = "INFO"
    log_json: bool = False

    # In-process scheduler for reminders, schedule occurrences and quotation expiry.
    # Single-instance only (the app already assumes one process); disabled in tests.
    scheduler_enabled: bool = False
    reminder_poll_seconds: int = 300

    # Public document pages (/i/{token}, /q/{token}): requests per minute per IP and path.
    public_rate_limit_per_minute: int = 60

    @property
    def cors_allow_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins_raw.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
