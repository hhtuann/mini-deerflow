from langchain_openai import ChatOpenAI

from mini_deerflow.config import Settings


def create_chat_model(settings: Settings) -> ChatOpenAI:
    """Create the chat model used by Mini DeerFlow."""

    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.api_key,
        base_url=str(settings.base_url).rstrip("/"),
        temperature=settings.temperature,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )
