"""Settings. Every value comes from the environment; nothing is hard-coded."""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
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

    AI_PLATFORM_BASE_URL: str = ""
    AI_PLATFORM_API_KEY: str = ""
    AI_PLATFORM_WEBHOOK_SECRET: str = ""

    PAYMENT_PROVIDER: Literal["mock", "selcom", "azampay", "clickpesa"] = "mock"
    PAYMENT_WEBHOOK_SECRET: str = ""

    SMS_PROVIDER: str = "mock"
    SMS_SENDER_ID: str = "BEYONDSCH"
    SMS_API_KEY: str = ""

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
