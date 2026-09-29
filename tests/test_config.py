import pytest
from pydantic import ValidationError

from mini_deerflow.config import Settings


def test_settings_load_from_environment(monkeypatch):
    monkeypatch.setenv("MINI_DEERFLOW_API_KEY", "test-secret")
    monkeypatch.setenv("MINI_DEERFLOW_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("MINI_DEERFLOW_MODEL_NAME", "test-model")
    monkeypatch.setenv("MINI_DEERFLOW_WIKI_REQUEST_TIMEOUT", "9")

    settings = Settings(_env_file=None)

    assert settings.api_key.get_secret_value() == "test-secret"
    assert str(settings.base_url).rstrip("/") == "https://api.example.test/v1"
    assert settings.model_name == "test-model"
    assert settings.temperature == 0.0
    assert settings.max_retries == 1
    assert settings.wiki_request_timeout == 9.0
    assert settings.structured_output_mode == "prompt_json"
    assert "test-secret" not in repr(settings)


def test_settings_glm_provider_defaults() -> None:
    settings = Settings(api_key="test-secret", _env_file=None)

    assert str(settings.base_url).rstrip("/") == "https://api.z.ai/api/coding/paas/v4"
    assert settings.model_name == "glm-5.3"
    assert settings.wiki_request_timeout == 15.0
    assert settings.structured_output_mode == "prompt_json"


def test_settings_require_api_key(monkeypatch):
    monkeypatch.delenv("MINI_DEERFLOW_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="api_key"):
        Settings(_env_file=None)


def test_settings_reject_invalid_values(monkeypatch):
    monkeypatch.setenv("MINI_DEERFLOW_API_KEY", "test-secret")
    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "3")
    monkeypatch.setenv("MINI_DEERFLOW_MAX_RETRIES", "-1")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_obsolete_google_key_does_not_satisfy_api_key(monkeypatch):
    monkeypatch.delenv("MINI_DEERFLOW_API_KEY", raising=False)
    monkeypatch.setenv("MINI_DEERFLOW_GOOGLE_API_KEY", "obsolete-google-secret")

    with pytest.raises(ValidationError, match="api_key"):
        Settings(_env_file=None)


def test_settings_reject_blank_api_key() -> None:
    with pytest.raises(ValidationError, match="api_key"):
        Settings(_env_file=None, api_key="   ")
