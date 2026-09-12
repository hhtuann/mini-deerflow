import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Self

import pytest
from pydantic import SecretStr

from mini_deerflow.web import (
    JinaWebProvider,
    UrllibWebHttpClient,
    WebFetchError,
    WebHttpResponse,
    WebProviderErrorCategory,
    WebSearchError,
    WebTransportError,
)


class FakeHttpClient:
    def __init__(self, outcomes: list[WebHttpResponse | BaseException]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[dict[str, object]] = []

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WebHttpResponse:
        self.calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        outcome = self._outcomes.popleft()

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


def json_response(body: str, status_code: int = 200) -> WebHttpResponse:
    return WebHttpResponse(
        status_code=status_code,
        body=body.encode(),
        content_type="application/json",
    )


def test_jina_search_provider_normalizes_results_and_request() -> None:
    client = FakeHttpClient(
        [
            json_response(
                '{"code":200,"data":['
                '{"title":"Official docs","url":"https://example.com/docs",'
                '"description":"Reference material"}]}'
            )
        ]
    )
    provider = JinaWebProvider(
        client,
        api_key=SecretStr("private-jina-key"),
        timeout_seconds=7.5,
        max_response_bytes=12_345,
    )

    results = asyncio.run(provider.search("agent evidence", max_results=3))

    assert [result.model_dump(mode="json") for result in results] == [
        {
            "title": "Official docs",
            "url": "https://example.com/docs",
            "snippet": "Reference material",
        }
    ]
    assert client.calls == [
        {
            "url": "https://s.jina.ai/",
            "payload": {"q": "agent evidence", "num": 3},
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer private-jina-key",
            },
            "timeout_seconds": 7.5,
            "max_response_bytes": 12_345,
        }
    ]


def test_jina_fetch_provider_normalizes_page_without_api_key() -> None:
    client = FakeHttpClient(
        [
            json_response(
                '{"code":200,"data":{"title":"Page",'
                '"url":"https://example.com/final","content":"Trusted output"}}'
            )
        ]
    )
    provider = JinaWebProvider(client)

    page = asyncio.run(provider.fetch("https://example.com/start"))

    assert page.model_dump(mode="json") == {
        "url": "https://example.com/final",
        "title": "Page",
        "content": "Trusted output",
        "status_code": 200,
        "content_type": "text/markdown",
    }
    assert client.calls[0]["headers"] == {"Accept": "application/json"}


def test_jina_search_requires_configured_api_key_without_calling_provider() -> None:
    client = FakeHttpClient([])
    provider = JinaWebProvider(client)

    with pytest.raises(WebSearchError, match="configuration error") as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    assert exc_info.value.category is WebProviderErrorCategory.CONFIGURATION
    assert exc_info.value.__cause__ is None
    assert client.calls == []


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        ("search", WebSearchError),
        ("fetch", WebFetchError),
    ],
)
def test_jina_provider_normalizes_transport_timeout(
    operation: str,
    expected_error: type[Exception],
) -> None:
    client = FakeHttpClient(
        [WebTransportError("timeout at host containing-secret.example")]
    )
    provider = JinaWebProvider(
        client,
        api_key=SecretStr("private-jina-key"),
    )

    with pytest.raises(expected_error, match="transport failed") as exc_info:
        if operation == "search":
            asyncio.run(provider.search("query", max_results=1))
        else:
            asyncio.run(provider.fetch("https://example.com"))

    rendered = str(exc_info.value)
    assert exc_info.value.category is WebProviderErrorCategory.TRANSPORT
    assert exc_info.value.__cause__ is None
    assert "private-jina-key" not in rendered
    assert "containing-secret" not in rendered


@pytest.mark.parametrize(
    ("status_code", "category", "message"),
    [
        (401, WebProviderErrorCategory.AUTHENTICATION, "authentication"),
        (403, WebProviderErrorCategory.AUTHENTICATION, "authentication"),
        (429, WebProviderErrorCategory.RATE_LIMIT, "rate limit"),
        (500, WebProviderErrorCategory.UPSTREAM, "service failed"),
        (503, WebProviderErrorCategory.UPSTREAM, "service failed"),
    ],
)
def test_jina_search_classifies_http_failures_without_leaking_response(
    status_code: int,
    category: WebProviderErrorCategory,
    message: str,
) -> None:
    provider = JinaWebProvider(
        FakeHttpClient(
            [
                json_response(
                    '{"authorization":"Bearer private-jina-key",'
                    '"detail":"raw-secret-response-body"}',
                    status_code,
                )
            ]
        ),
        api_key=SecretStr("private-jina-key"),
    )

    with pytest.raises(WebSearchError, match=message) as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.category is category
    assert exc_info.value.__cause__ is None
    assert "private-jina-key" not in rendered
    assert "Bearer" not in rendered
    assert "raw-secret-response-body" not in rendered


def test_jina_provider_normalizes_malformed_response_without_body_leak() -> None:
    provider = JinaWebProvider(
        FakeHttpClient([json_response('{"token":"raw-secret-response-body"}')]),
        api_key=SecretStr("private-jina-key"),
    )

    with pytest.raises(WebSearchError, match="result list") as exc_info:
        asyncio.run(provider.search("query", max_results=1))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.category is WebProviderErrorCategory.MALFORMED_RESPONSE
    assert exc_info.value.__cause__ is None
    assert "private-jina-key" not in rendered
    assert "raw-secret-response-body" not in rendered


def test_jina_provider_normalizes_invalid_json_without_body_leak() -> None:
    provider = JinaWebProvider(
        FakeHttpClient([json_response("raw-secret-response-body")]),
    )

    with pytest.raises(WebFetchError, match="invalid JSON") as exc_info:
        asyncio.run(provider.fetch("https://example.com"))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.category is WebProviderErrorCategory.MALFORMED_RESPONSE
    assert exc_info.value.__cause__ is None
    assert "raw-secret-response-body" not in rendered


def test_urllib_client_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResponse:
        status = 200
        headers = SimpleNamespace(
            get_content_type=lambda: "application/json",
        )

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return b"x" * size

    monkeypatch.setattr(
        "mini_deerflow.web.urlopen",
        lambda request, timeout: OversizedResponse(),
    )

    with pytest.raises(WebTransportError, match="size limit"):
        asyncio.run(
            UrllibWebHttpClient().post_json(
                "https://provider.example",
                {"q": "bounded"},
                headers={"Accept": "application/json"},
                timeout_seconds=1.0,
                max_response_bytes=16,
            )
        )
