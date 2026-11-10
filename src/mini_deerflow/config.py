from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mini_deerflow.documents import DEFAULT_DOCUMENT_LIMITS


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

    live_run_deadline_seconds: float = Field(
        default=1_200.0,
        gt=0.0,
        le=86_400.0,
        allow_inf_nan=False,
    )

    synthesis_deadline_seconds: float = Field(
        default=300.0,
        gt=0.0,
        le=3_600.0,
        allow_inf_nan=False,
    )

    model_max_output_tokens: int = Field(
        default=8_192,
        ge=256,
        le=131_072,
    )

    synthesis_max_response_chars: int = Field(
        default=32_768,
        ge=1_024,
        le=1_000_000,
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

    document_max_bytes: int = Field(
        default=DEFAULT_DOCUMENT_LIMITS.max_document_bytes,
        ge=1_024,
        le=52_428_800,
    )
    document_workspace_max_documents: int = Field(
        default=DEFAULT_DOCUMENT_LIMITS.max_documents_per_workspace,
        ge=1,
        le=1_000,
    )
    document_max_versions: int = Field(
        default=DEFAULT_DOCUMENT_LIMITS.max_versions_per_document,
        ge=1,
        le=100,
    )
    document_workspace_max_bytes: int = Field(
        default=DEFAULT_DOCUMENT_LIMITS.max_workspace_bytes,
        ge=1_048_576,
        le=2_147_483_648,
    )
    document_max_workspaces: int = Field(
        default=DEFAULT_DOCUMENT_LIMITS.max_workspaces,
        ge=1,
        le=1_000,
    )

    document_extraction_deadline_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=3_600.0,
        allow_inf_nan=False,
    )
    document_extraction_max_pages: int = Field(
        default=300,
        ge=1,
        le=5_000,
    )
    document_extraction_max_chars: int = Field(
        default=2_000_000,
        ge=10_000,
        le=50_000_000,
    )
    document_extraction_max_xml_bytes: int = Field(
        default=20_971_520,
        ge=1_024,
        le=524_288_000,
    )
    document_extraction_max_zip_entries: int = Field(
        default=5_000,
        ge=1,
        le=100_000,
    )
    document_extraction_max_total_uncompressed: int = Field(
        default=104_857_600,
        ge=1_048_576,
        le=2_147_483_648,
    )

    document_retrieval_max_hits: int = Field(
        default=8,
        ge=1,
        le=50,
    )
    document_retrieval_max_context_chars: int = Field(
        default=12_000,
        ge=500,
        le=200_000,
    )
    document_retrieval_max_query_chars: int = Field(
        default=400,
        ge=16,
        le=2_000,
    )
    document_retrieval_deadline_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        allow_inf_nan=False,
    )

    structured_output_mode: Literal["native", "prompt_json"] = "prompt_json"

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        normalized = value.get_secret_value().strip()
        if not normalized:
            raise ValueError("api_key must not be blank")
        return SecretStr(normalized)
