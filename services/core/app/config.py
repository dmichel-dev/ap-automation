from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (see .env.example)."""

    ap_database_url: SecretStr
    core_api_token: SecretStr
    mistral_api_key: SecretStr = SecretStr("")
    mistral_ocr_model: str = "mistral-ocr-latest"
    confidence_threshold: float = 0.85
    home_currency: str = "USD"
    file_storage_dir: str = "/data/files"
    file_retention_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
