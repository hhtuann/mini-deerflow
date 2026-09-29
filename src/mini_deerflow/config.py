from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MINI_DEERFLOW_",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
        frozen=True,
    )

    api_key: SecretStr

    base_url: AnyHttpUrl = AnyHttpUrl("https://api.z.ai/api/coding/paas/v4")

    model_name: str = Field(
        default="glm-5.3",
        min_length=1,
    )

    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
    )

    request_timeout: float = Field(
        default=300.0,
        gt=0.0,
    )

    max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
    )

    wiki_request_timeout: float = Field(
        default=15.0,
        gt=0.0,
        le=120.0,
    )

    structured_output_mode: Literal["native", "prompt_json"] = "prompt_json"

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        normalized = value.get_secret_value().strip()
        if not normalized:
            raise ValueError("api_key must not be blank")
        return SecretStr(normalized)
