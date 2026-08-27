import pytest
from pydantic import ValidationError

from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebFetchError,
    WebFetchProvider,
    WebPolicyError,
    WebProviderError,
    WebSearchError,
    WebSearchProvider,
)


class FakeSearchProvider:
    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        return [
            SearchResult(
                title=query,
                url="https://example.com",
            )
        ][:max_results]


class FakeFetchProvider:
    async def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(
            url=url,
            content="Example content",
            status_code=200,
        )


def test_search_result_normalizes_text() -> None:
    result = SearchResult(
        title="  LangGraph Documentation  ",
        url="https://example.com",
        snippet="  Stateful agent orchestration.  ",
    )

    assert result.title == "LangGraph Documentation"
    assert result.snippet == "Stateful agent orchestration."


def test_search_result_serializes_url_as_string() -> None:
    result = SearchResult(
        title="Example",
        url="https://example.com",
    )

    assert result.model_dump(mode="json") == {
        "title": "Example",
        "url": "https://example.com/",
        "snippet": "",
    }


@pytest.mark.parametrize(
    "invalid_title",
    [
        "",
        "   ",
        "a" * 501,
    ],
)
def test_search_result_rejects_invalid_title(
    invalid_title: str,
) -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title=invalid_title,
            url="https://example.com",
        )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_search_result_rejects_non_http_url(
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title="Example",
            url=invalid_url,
        )


def test_search_result_rejects_oversized_snippet() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title="Example",
            url="https://example.com",
            snippet="a" * 4_001,
        )


def test_web_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title="Example",
            url="https://example.com",
            unexpected=True,
        )


def test_web_model_is_frozen() -> None:
    result = SearchResult(
        title="Example",
        url="https://example.com",
    )

    with pytest.raises(ValidationError):
        result.title = "Modified"


@pytest.mark.parametrize(
    "invalid_status_code",
    [
        99,
        600,
    ],
)
def test_fetched_page_rejects_invalid_status_code(
    invalid_status_code: int,
) -> None:
    with pytest.raises(ValidationError):
        FetchedPage(
            url="https://example.com",
            content="Example",
            status_code=invalid_status_code,
        )


def test_fetched_page_allows_optional_metadata() -> None:
    page = FetchedPage(
        url="https://example.com",
        content="",
        status_code=204,
    )

    assert page.title is None
    assert page.content_type is None
    assert page.content == ""


def test_fake_providers_satisfy_runtime_protocols() -> None:
    assert isinstance(
        FakeSearchProvider(),
        WebSearchProvider,
    )
    assert isinstance(
        FakeFetchProvider(),
        WebFetchProvider,
    )


def test_incomplete_provider_does_not_satisfy_protocol() -> None:
    class IncompleteProvider:
        pass

    provider = IncompleteProvider()

    assert not isinstance(provider, WebSearchProvider)
    assert not isinstance(provider, WebFetchProvider)


def test_web_errors_share_domain_base_error() -> None:
    assert isinstance(
        WebSearchError("search failed"),
        WebProviderError,
    )
    assert isinstance(
        WebFetchError("fetch failed"),
        WebProviderError,
    )
    assert isinstance(
        WebPolicyError("URL blocked"),
        WebProviderError,
    )
