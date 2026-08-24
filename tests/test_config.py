import pytest
from pydantic import ValidationError

from mini_deerflow.config import Settings


def test_settings_load_from_environment(monkeypatch):
    monkeypatch.setenv("MINI_DEERFLOW_API_KEY", "test-secret")
    monkeypatch.setenv("MINI_DEERFLOW_MODEL_NAME", "test-model")

    settings = Settings(_env_file=None)

    assert settings.api_key.get_secret_value() == "test-secret"
    assert settings.model_name == "test-model"
    assert settings.temperature == 0.0
    assert settings.max_retries == 2
    assert "test-secret" not in repr(settings)


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
