import logging
from typing import Annotated

from pydantic import Field, HttpUrl, StringConstraints

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebFetchProvider,
    WebPolicyError,
    WebProviderError,
    WebSearchProvider,
)
from mini_deerflow.web_safety import WebTargetValidator

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
                    "error_category": exc.category.value,
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

    def __init__(
        self,
        provider: WebFetchProvider,
        target_validator: WebTargetValidator,
        *,
        max_content_chars: int = 100_000,
    ) -> None:
        if not isinstance(provider, WebFetchProvider):
            raise TypeError("provider must satisfy WebFetchProvider.")

        if not isinstance(target_validator, WebTargetValidator):
            raise TypeError("target_validator must satisfy WebTargetValidator.")

        if (
            isinstance(max_content_chars, bool)
            or not isinstance(max_content_chars, int)
            or max_content_chars <= 0
        ):
            raise ValueError("max_content_chars must be a positive integer.")

        self._provider = provider
        self._target_validator = target_validator
        self._max_content_chars = max_content_chars

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WebFetchInput)

        try:
            safe_target = await self._target_validator.validate(tool_input.url)
        except WebPolicyError as exc:
            return ToolResult.fail(
                error="Web fetch denied by safety policy.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                    "error_category": exc.category.value,
                    "error_code": (
                        exc.code.value
                        if getattr(exc, "code", None) is not None
                        else None
                    ),
                },
            )

        try:
            page = await self._provider.fetch(
                safe_target,
            )
        except WebProviderError as exc:
            return ToolResult.fail(
                error="Web fetch failed.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                    "error_category": exc.category.value,
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

        redirect_targets = [*page.redirect_chain, page.url]
        checked_urls = {str(safe_target.url)}
        try:
            for redirect_target in redirect_targets:
                rendered_redirect = str(redirect_target)
                if rendered_redirect in checked_urls:
                    continue
                await self._target_validator.validate(redirect_target)
                checked_urls.add(rendered_redirect)
        except WebPolicyError as exc:
            return ToolResult.fail(
                error="Web fetch redirect denied by safety policy.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                    "error_category": exc.category.value,
                    "error_code": (
                        exc.code.value
                        if getattr(exc, "code", None) is not None
                        else None
                    ),
                },
            )

        page_data = page.model_dump(mode="json", exclude={"redirect_chain"})
        original_content_chars = len(page.content)
        truncated = original_content_chars > self._max_content_chars
        page_data["content"] = page.content[: self._max_content_chars]

        return ToolResult.ok(
            data=page_data,
            metadata={
                "tool_name": self.name,
                "content_truncated": truncated,
                "original_content_chars": original_content_chars,
            },
        )
