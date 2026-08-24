from unittest.mock import patch

from mini_deerflow.config import Settings
from mini_deerflow.model import create_chat_model


def test_create_chat_model_forwards_validated_settings():
    settings = Settings(
        _env_file=None,
        api_key="test-secret",
        base_url="https://example.com/v1/",
        model_name="test-model",
        temperature=0.25,
        request_timeout=30,
        max_retries=1,
    )

    with patch("mini_deerflow.model.ChatOpenAI") as chat_open_ai:
        expected_model = chat_open_ai.return_value

        actual_model = create_chat_model(settings)

    assert actual_model is expected_model

    chat_open_ai.assert_called_once_with(
        model="test-model",
        api_key=settings.api_key,
        base_url="https://example.com/v1",
        temperature=0.25,
        timeout=30,
        max_retries=1,
    )
