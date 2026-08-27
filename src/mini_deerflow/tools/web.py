import logging
from typing import Annotated

from pydantic import Field, HttpUrl, StringConstraints

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebFetchProvider,
    WebProviderError,
    WebSearchProvider,
)

logger = logging.getLogger(__name__)

SearchQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]


class WebSearchInput(ToolInput):
    query: SearchQuery
    max_results: int = Field(
        default=5,
        ge=1,
        le=10,
        strict=True,
    )


class WebFetchInput(ToolInput):
    url: HttpUrl


class WebSearchTool:
    name = "web_search"
    description = "Search the public web for relevant sources."
    input_model = WebSearchInput
    timeout_seconds = 20.0
    idempotent = True

    def __init__(self, provider: WebSearchProvider) -> None:
        if not isinstance(provider, WebSearchProvider):
            raise TypeError("provider must satisfy WebSearchProvider.")

        self._provider = provider

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WebSearchInput)

        try:
            results = await self._provider.search(
                tool_input.query,
                max_results=tool_input.max_results,
            )
        except WebProviderError as exc:
            return ToolResult.fail(
                error="Web search failed.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                },
            )

        if not isinstance(results, list) or not all(
            isinstance(result, SearchResult) for result in results
        ):
            logger.error(
                "Web search provider returned an invalid result: %s",
                type(results).__name__,
            )
            return ToolResult.fail(
                error="Web search provider returned invalid results.",
                metadata={
                    "tool_name": self.name,
                    "error_type": "InvalidProviderResultError",
                },
            )

        limited_results = results[: tool_input.max_results]

        return ToolResult.ok(
            data={
                "query": tool_input.query,
                "results": [
                    result.model_dump(mode="json") for result in limited_results
                ],
                "count": len(limited_results),
            }
        )


class WebFetchTool:
    name = "web_fetch"
    description = "Fetch one public HTTP or HTTPS web page."
    input_model = WebFetchInput
    timeout_seconds = 30.0
    idempotent = True

    def __init__(self, provider: WebFetchProvider) -> None:
        if not isinstance(provider, WebFetchProvider):
            raise TypeError("provider must satisfy WebFetchProvider.")

        self._provider = provider

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WebFetchInput)

        try:
            page = await self._provider.fetch(
                str(tool_input.url),
            )
        except WebProviderError as exc:
            return ToolResult.fail(
                error="Web fetch failed.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                },
            )

        if not isinstance(page, FetchedPage):
            logger.error(
                "Web fetch provider returned %s instead of FetchedPage",
                type(page).__name__,
            )
            return ToolResult.fail(
                error="Web fetch provider returned an invalid page.",
                metadata={
                    "tool_name": self.name,
                    "error_type": "InvalidProviderResultError",
                },
            )

        return ToolResult.ok(
            data=page.model_dump(mode="json"),
        )
