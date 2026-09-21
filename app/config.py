from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str = Field(min_length=32)
    mfa_encryption_key: str
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    allowed_origins: str = ""
    otlp_endpoint: str = ""
    environment: str = "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
