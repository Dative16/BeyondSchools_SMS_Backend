"""Settings. Every value comes from the environment; nothing is hard-coded."""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
    #: Below INFO the mock SMS sender's one-time code is invisible, and the
    #: applicant flow cannot be exercised locally at all — the code is hashed
    #: before it is stored, so the log is the only place it exists.
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    SECRET_KEY: str
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: str = ""

    # Two roles, and the difference matters. POSTGRES_USER owns the tables and
    # runs the migrations; row-level security does not apply to a table's
    # owner, so the application never connects as it. DB_APP_ROLE is the
    # non-superuser, non-owner role the API and the workers use, and the one
    # the policies actually bite on.
    POSTGRES_HOST: str
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    DB_APP_ROLE: str = "bs_app"
    DB_APP_PASSWORD: str = "bs_app"  # noqa: S105 - a local default, overridden by .env

    REDIS_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    JWT_ISSUER: str
    JWT_ALGORITHM: str = "RS256"
    JWT_PRIVATE_KEY_PATH: str
    JWT_PUBLIC_KEY_PATH: str
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_SECONDS: int = 1_209_600

    #: This service's own address, as the outside world reaches it. Used to
    #: build the URLs the local storage adapter serves, and nothing else.
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Documents. "local" writes to disk and serves through this API so the
    # admissions flow is runnable before R2 exists; "r2" is Cloudflare R2
    # through its S3-compatible API, which is what production uses. The bucket
    # is private either way: these are children's birth certificates, and a
    # public URL is a URL that ends up in a WhatsApp group.
    STORAGE_PROVIDER: Literal["local", "r2"] = "local"
    STORAGE_LOCAL_PATH: str = "/srv/.storage"
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = ""
    #: Only for a non-default endpoint; otherwise derived from the account id.
    R2_ENDPOINT: str = ""
    #: How long an upload or a download link lives. Long enough for a slow
    #: upload on a weak connection, short enough that a link copied out of a
    #: browser history is worthless.
    STORAGE_UPLOAD_TTL_SECONDS: int = 900
    STORAGE_DOWNLOAD_TTL_SECONDS: int = 300

    AI_PLATFORM_BASE_URL: str = ""
    AI_PLATFORM_API_KEY: str = ""
    AI_PLATFORM_WEBHOOK_SECRET: str = ""

    # "manual" means there is no aggregator: a bursar records each payment,
    # named and audited, and the callback path stays dormant. That is the
    # Phase 1 decision, and the reason app/integrations/payments/ still holds
    # three unimplemented adapters — the protocol is ready for the day an
    # aggregator is chosen, and nothing above it has to change.
    PAYMENT_PROVIDER: Literal["manual", "mock", "selcom", "azampay", "clickpesa"] = "manual"
    PAYMENT_WEBHOOK_SECRET: str = ""

    # SMS is a Phase 1 dependency, not a Phase 3 one: the public application
    # form cannot verify a phone number without it. Which gateway the pilot
    # school uses is still open; "mock" logs instead of sending.
    SMS_PROVIDER: str = "mock"
    #: Beem calls this the source address. It must be a sender ID that has
    #: already been approved on the account, or every send is rejected.
    SMS_SENDER_ID: str = "BEYONDSCH"
    SMS_API_KEY: str = ""
    SMS_API_SECRET: str = ""
    #: The full send endpoint, not a base path: Beem's own settings name it
    #: that way and it keeps sandbox and production a one-line change.
    SMS_BASE_URL: str = "https://apisms.beem.africa/v1/send"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> PostgresDsn:  # noqa: N802
        """What the application connects with: the restricted role.

        If this ever reverts to POSTGRES_USER, every policy in the schema stops
        applying and nothing fails — which is exactly why
        `tests/integration/test_tenant_isolation.py` exists.
        """
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.DB_APP_ROLE,
            password=self.DB_APP_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def MIGRATION_DATABASE_URL(self) -> PostgresDsn:  # noqa: N802
        """What Alembic connects with: the owning role. Only migrations."""
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
