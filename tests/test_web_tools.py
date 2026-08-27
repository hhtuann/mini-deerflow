import asyncio

import pytest
from pydantic import ValidationError

from mini_deerflow.tools.registry import ToolRegistry
from mini_deerflow.tools.runner import ToolRunner
from mini_deerflow.tools.web import (
    WebFetchInput,
    WebFetchTool,
    WebSearchInput,
    WebSearchTool,
)
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebFetchError,
    WebPolicyError,
    WebSearchError,
)


class SuccessfulSearchProvider:
    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        # Intentionally ignore max_results so the tool must enforce it.
        return [
            SearchResult(
                title="Result 1",
                url="https://example.com/1",
                snippet=f"First result for {query}",
            ),
            SearchResult(
                title="Result 2",
                url="https://example.com/2",
                snippet=f"Second result for {query}",
            ),
            SearchResult(
                title="Result 3",
                url="https://example.com/3",
                snippet=f"Third result for {query}",
            ),
        ]


class SuccessfulFetchProvider:
    async def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(
            url=url,
            title="Example page",
            content="Example content",
            status_code=200,
            content_type="text/html",
        )


def test_web_search_input_normalizes_query_and_uses_default() -> None:
    tool_input = WebSearchInput(query="  LangGraph  ")

    assert tool_input.query == "LangGraph"
    assert tool_input.max_results == 5


@pytest.mark.parametrize(
    "invalid_max_results",
    [
        0,
        11,
        True,
        1.0,
    ],
)
def test_web_search_input_rejects_invalid_max_results(
    invalid_max_results: object,
) -> None:
    with pytest.raises(ValidationError):
        WebSearchInput(
            query="LangGraph",
            max_results=invalid_max_results,
        )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "not-a-url",
    ],
)
def test_web_fetch_input_rejects_invalid_url(
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError):
        WebFetchInput(url=invalid_url)


def test_web_tools_reject_incompatible_provider() -> None:
    with pytest.raises(
        TypeError,
        match="WebSearchProvider",
    ):
        WebSearchTool(object())

    with pytest.raises(
        TypeError,
        match="WebFetchProvider",
    ):
        WebFetchTool(object())


def test_web_search_tool_normalizes_and_limits_results() -> None:
    tool = WebSearchTool(SuccessfulSearchProvider())

    result = asyncio.run(
        tool.run(
            WebSearchInput(
                query="  LangGraph  ",
                max_results=2,
            )
        )
    )

    assert result.success is True
    assert result.error is None
    assert result.data == {
        "query": "LangGraph",
        "results": [
            {
                "title": "Result 1",
                "url": "https://example.com/1",
                "snippet": "First result for LangGraph",
            },
            {
                "title": "Result 2",
                "url": "https://example.com/2",
                "snippet": "Second result for LangGraph",
            },
        ],
        "count": 2,
    }


def test_web_search_tool_converts_domain_error_to_safe_failure() -> None:
    class FailingSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            raise WebSearchError("Private provider host search.internal failed")

    tool = WebSearchTool(FailingSearchProvider())
    result = asyncio.run(
        tool.run(
            WebSearchInput(query="LangGraph"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Web search failed."
    assert result.metadata["error_type"] == "WebSearchError"
    assert "search.internal" not in result.model_dump_json()


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"results": []},
        [{"title": "Not a SearchResult"}],
    ],
)
def test_web_search_tool_rejects_invalid_provider_result(
    invalid_result: object,
) -> None:
    class InvalidSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> object:
            return invalid_result

    tool = WebSearchTool(InvalidSearchProvider())
    result = asyncio.run(
        tool.run(
            WebSearchInput(query="LangGraph"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Web search provider returned invalid results."
    assert result.metadata["error_type"] == "InvalidProviderResultError"


def test_web_search_tool_propagates_unexpected_exception() -> None:
    class BrokenSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            raise AttributeError("Programming bug")

    tool = WebSearchTool(BrokenSearchProvider())

    with pytest.raises(
        AttributeError,
        match="Programming bug",
    ):
        asyncio.run(
            tool.run(
                WebSearchInput(query="LangGraph"),
            )
        )


def test_web_fetch_tool_returns_normalized_page() -> None:
    tool = WebFetchTool(SuccessfulFetchProvider())

    result = asyncio.run(
        tool.run(
            WebFetchInput(
                url="https://example.com/article",
            )
        )
    )

    assert result.success is True
    assert result.error is None
    assert result.data == {
        "url": "https://example.com/article",
        "title": "Example page",
        "content": "Example content",
        "status_code": 200,
        "content_type": "text/html",
    }


def test_web_fetch_tool_converts_policy_error_to_safe_failure() -> None:
    class BlockedFetchProvider:
        async def fetch(self, url: str) -> FetchedPage:
            raise WebPolicyError("Blocked internal URL http://127.0.0.1/admin")

    tool = WebFetchTool(BlockedFetchProvider())
    result = asyncio.run(
        tool.run(
            WebFetchInput(
                url="http://127.0.0.1/admin",
            )
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Web fetch failed."
    assert result.metadata["error_type"] == "WebPolicyError"
    assert "127.0.0.1" not in result.model_dump_json()


def test_web_fetch_tool_converts_fetch_error_to_safe_failure() -> None:
    class FailingFetchProvider:
        async def fetch(self, url: str) -> FetchedPage:
            raise WebFetchError("Provider credential was rejected")

    tool = WebFetchTool(FailingFetchProvider())
    result = asyncio.run(
        tool.run(
            WebFetchInput(
                url="https://example.com",
            )
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Web fetch failed."
    assert result.metadata["error_type"] == "WebFetchError"
    assert "credential" not in result.model_dump_json()


def test_web_fetch_tool_rejects_invalid_provider_result() -> None:
    class InvalidFetchProvider:
        async def fetch(self, url: str) -> object:
            return {
                "url": url,
                "content": "Not a FetchedPage",
            }

    tool = WebFetchTool(InvalidFetchProvider())
    result = asyncio.run(
        tool.run(
            WebFetchInput(
                url="https://example.com",
            )
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Web fetch provider returned an invalid page."
    assert result.metadata["error_type"] == "InvalidProviderResultError"


def test_runner_rejects_invalid_fetch_url_before_provider_call() -> None:
    class TrackingFetchProvider:
        def __init__(self) -> None:
            self.was_called = False

        async def fetch(self, url: str) -> FetchedPage:
            self.was_called = True
            return FetchedPage(
                url="https://example.com",
                content="Example",
                status_code=200,
            )

    provider = TrackingFetchProvider()
    runner = ToolRunner(
        ToolRegistry(
            [
                WebFetchTool(provider),
            ]
        )
    )

    result = asyncio.run(
        runner.run(
            "web_fetch",
            {
                "url": "file:///etc/passwd",
            },
        )
    )

    assert result.success is False
    assert result.error == "Tool input is invalid."
    assert provider.was_called is False
