import asyncio
from collections import deque

import httpx
import pytest
from google.genai import errors, types

from mini_deerflow.google_web import GoogleWebProvider
from mini_deerflow.web import (
    WebFetchError,
    WebProviderErrorCategory,
    WebSearchError,
)
from mini_deerflow.web_safety import SafeWebTarget


def safe_target(url: str = "https://example.com/") -> SafeWebTarget:
    return SafeWebTarget(
        url=url,
        hostname="example.com",
        port=443,
        resolved_addresses=("93.184.216.34",),
    )


class FakeGoogleContentClient:
    def __init__(self, outcomes: list[object | BaseException]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[dict[str, object]] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> object:
        self.calls.append(
            {
                "model": model,
                "contents": contents,
                "config": config,
            }
        )
        outcome = self._outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def grounded_search_response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=[
                        types.GroundingChunk(
                            web=types.GroundingChunkWeb(
                                title="Official docs",
                                uri="https://example.com/docs",
                            )
                        ),
                        types.GroundingChunk(
                            web=types.GroundingChunkWeb(
                                title="Duplicate docs",
                                uri="https://example.com/docs",
                            )
                        ),
                        types.GroundingChunk(
                            web=types.GroundingChunkWeb(
                                title="Independent source",
                                uri="https://example.org/report",
                            )
                        ),
                    ],
                    grounding_supports=[
                        types.GroundingSupport(
                            grounding_chunk_indices=[0, 1],
                            segment=types.Segment(text="Reference material"),
                        ),
                        types.GroundingSupport(
                            grounding_chunk_indices=[2],
                            segment=types.Segment(text="Independent evidence"),
                        ),
                    ],
                )
            )
        ]
    )


def url_context_response(
    *,
    retrieved_url: str = "https://example.com/final",
    content: str = "Trusted output",
    status: types.UrlRetrievalStatus = (
        types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS
    ),
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=content)],
                ),
                url_context_metadata=types.UrlContextMetadata(
                    url_metadata=[
                        types.UrlMetadata(
                            retrieved_url=retrieved_url,
                            url_retrieval_status=status,
                        )
                    ]
                ),
            )
        ]
    )


def test_google_search_uses_grounding_metadata_and_deduplicates_sources() -> None:
    client = FakeGoogleContentClient([grounded_search_response()])
    provider = GoogleWebProvider(client, model_name="gemini-2.5-flash")

    results = asyncio.run(provider.search("agent evidence", max_results=5))

    assert [result.model_dump(mode="json") for result in results] == [
        {
            "title": "Official docs",
            "url": "https://example.com/docs",
            "snippet": "Reference material",
        },
        {
            "title": "Independent source",
            "url": "https://example.org/report",
            "snippet": "Independent evidence",
        },
    ]
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "gemini-2.5-flash"
    assert "agent evidence" in str(client.calls[0]["contents"])
    config = client.calls[0]["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.tools is not None
    assert len(config.tools) == 1
    assert config.tools[0].google_search is not None
    assert config.tools[0].url_context is None


def test_google_search_never_treats_generated_prose_as_source_identity() -> None:
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text=(
                                "Invented source: "
                                "https://model-generated.invalid/not-trusted"
                            )
                        )
                    ],
                ),
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=[],
                    grounding_supports=[],
                ),
            )
        ]
    )
    provider = GoogleWebProvider(
        FakeGoogleContentClient([response]),
        model_name="gemini-2.5-flash",
    )

    assert asyncio.run(provider.search("query", max_results=5)) == []


def test_google_fetch_uses_url_context_metadata_and_model_mediated_content() -> None:
    client = FakeGoogleContentClient([url_context_response()])
    provider = GoogleWebProvider(client, model_name="gemini-2.5-flash")

    page = asyncio.run(provider.fetch(safe_target("https://example.com/start")))

    assert page.model_dump(mode="json") == {
        "url": "https://example.com/final",
        "title": None,
        "content": "Trusted output",
        "status_code": 200,
        "content_type": "text/plain",
    }
    assert [str(url) for url in page.redirect_chain] == ["https://example.com/final"]
    config = client.calls[0]["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.tools is not None
    assert len(config.tools) == 1
    assert config.tools[0].url_context is not None
    assert config.tools[0].google_search is None


def test_google_fetch_rejects_unvalidated_target_before_client_call() -> None:
    client = FakeGoogleContentClient([])
    provider = GoogleWebProvider(client, model_name="gemini-2.5-flash")

    with pytest.raises(TypeError, match="SafeWebTarget"):
        asyncio.run(provider.fetch("https://example.com"))  # type: ignore[arg-type]

    assert client.calls == []


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (
            types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_UNSAFE,
            WebProviderErrorCategory.REJECTION,
        ),
        (
            types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_PAYWALL,
            WebProviderErrorCategory.UPSTREAM,
        ),
        (
            types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_ERROR,
            WebProviderErrorCategory.UPSTREAM,
        ),
    ],
)
def test_google_fetch_maps_url_context_retrieval_failures(
    status: types.UrlRetrievalStatus,
    category: WebProviderErrorCategory,
) -> None:
    provider = GoogleWebProvider(
        FakeGoogleContentClient([url_context_response(status=status)]),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(WebFetchError) as exc_info:
        asyncio.run(provider.fetch(safe_target()))

    assert exc_info.value.category is category
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (401, WebProviderErrorCategory.AUTHENTICATION),
        (403, WebProviderErrorCategory.AUTHENTICATION),
        (404, WebProviderErrorCategory.ENDPOINT),
        (429, WebProviderErrorCategory.RATE_LIMIT),
        (500, WebProviderErrorCategory.UPSTREAM),
        (503, WebProviderErrorCategory.UPSTREAM),
        (400, WebProviderErrorCategory.REJECTION),
    ],
)
def test_google_search_classifies_api_failures_without_leaking_response(
    status_code: int,
    category: WebProviderErrorCategory,
) -> None:
    provider = GoogleWebProvider(
        FakeGoogleContentClient(
            [
                errors.ClientError(
                    status_code,
                    {"error": {"message": "raw-secret-response-body"}},
                )
            ]
        ),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(WebSearchError) as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.category is category
    assert exc_info.value.__cause__ is None
    assert "raw-secret-response-body" not in rendered


@pytest.mark.parametrize(
    "transport_error",
    [
        TimeoutError("timeout at host containing-secret.example"),
        httpx.ConnectError("connect failed at containing-secret.example"),
    ],
)
def test_google_provider_normalizes_transport_failure_without_leaking_cause(
    transport_error: Exception,
) -> None:
    provider = GoogleWebProvider(
        FakeGoogleContentClient([transport_error]),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(WebSearchError, match="transport failed") as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.category is WebProviderErrorCategory.TRANSPORT
    assert exc_info.value.__cause__ is None
    assert "containing-secret" not in rendered


def test_google_provider_preserves_cancellation() -> None:
    provider = GoogleWebProvider(
        FakeGoogleContentClient([asyncio.CancelledError()]),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.search("query", max_results=1))


def test_google_search_rejects_missing_grounding_metadata() -> None:
    response = types.GenerateContentResponse(candidates=[types.Candidate()])
    provider = GoogleWebProvider(
        FakeGoogleContentClient([response]),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(WebSearchError, match="grounding metadata") as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    assert exc_info.value.category is WebProviderErrorCategory.MALFORMED_RESPONSE


def test_google_fetch_rejects_missing_url_context_metadata() -> None:
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="untrusted generated URL")],
                )
            )
        ]
    )
    provider = GoogleWebProvider(
        FakeGoogleContentClient([response]),
        model_name="gemini-2.5-flash",
    )

    with pytest.raises(WebFetchError, match="retrieval metadata") as exc_info:
        asyncio.run(provider.fetch(safe_target()))

    assert exc_info.value.category is WebProviderErrorCategory.MALFORMED_RESPONSE
