"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from the environment or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    poll_interval_seconds: int = 300
    log_level: str = "INFO"

    # How many recent readings to load per city for event detection context.
    history_limit: int = 24

    # Maximum number of times to retry a failed Open-Meteo HTTP call.
    weather_api_retry_attempts: int = 3

    # Seconds to wait between Open-Meteo retry attempts.
    weather_api_retry_wait_seconds: int = 2


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (constructed once per process)."""
    return Settings()
