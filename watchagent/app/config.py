from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    poll_interval_seconds: int = 300
    log_level: str = "INFO"


settings = Settings()
