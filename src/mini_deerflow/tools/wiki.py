"""Wikipedia-only tools for bounded educational research."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.wikipedia import WikipediaProvider, WikipediaProviderError

WikiText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class WikiSearchInput(ToolInput):
    query: WikiText
    language: Literal["en", "vi"] = "en"
    max_results: int = Field(default=3, ge=1, le=5, strict=True)


class WikiLookupInput(ToolInput):
    title: WikiText
    language: Literal["en", "vi"] = "en"


class WikiSearchTool:
    name = "wiki_search"
    description = "Search English or Vietnamese Wikipedia for relevant articles."
    input_model = WikiSearchInput
    timeout_seconds = 20.0
    idempotent = True

    def __init__(self, provider: WikipediaProvider) -> None:
        if not isinstance(provider, WikipediaProvider):
            raise TypeError("provider must satisfy WikipediaProvider")
        self._provider = provider

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WikiSearchInput)
        try:
            results = await self._provider.search(
                tool_input.query,
                language=tool_input.language,
                max_results=tool_input.max_results,
            )
        except WikipediaProviderError:
            return ToolResult.fail(
                "Wikipedia search failed.",
                metadata={
                    "tool_name": self.name,
                    "error_type": "WikipediaProviderError",
                },
            )
        return ToolResult.ok(
            {
                "query": tool_input.query,
                "language": tool_input.language,
                "results": [result.model_dump(mode="json") for result in results],
                "count": len(results),
            }
        )


class WikiLookupTool:
    name = "wiki_lookup"
    description = "Read one English or Vietnamese Wikipedia article by title."
    input_model = WikiLookupInput
    timeout_seconds = 20.0
    idempotent = True

    def __init__(self, provider: WikipediaProvider) -> None:
        if not isinstance(provider, WikipediaProvider):
            raise TypeError("provider must satisfy WikipediaProvider")
        self._provider = provider

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WikiLookupInput)
        try:
            page = await self._provider.lookup(
                tool_input.title,
                language=tool_input.language,
            )
        except WikipediaProviderError:
            return ToolResult.fail(
                "Wikipedia lookup failed.",
                metadata={
                    "tool_name": self.name,
                    "error_type": "WikipediaProviderError",
                },
            )
        return ToolResult.ok(page.model_dump(mode="json"))
