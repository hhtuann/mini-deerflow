import asyncio

from mini_deerflow.tools.wiki import (
    WikiLookupInput,
    WikiLookupTool,
    WikiSearchInput,
    WikiSearchTool,
)
from mini_deerflow.wikipedia import (
    WikiPage,
    WikipediaProviderError,
    WikiSearchResult,
)


class FakeWikipediaProvider:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str, int]] = []
        self.lookup_calls: list[tuple[str, str]] = []
        self.fail_search = False
        self.fail_lookup = False

    async def search(
        self,
        query: str,
        *,
        language: str,
        max_results: int,
    ) -> list[WikiSearchResult]:
        self.search_calls.append((query, language, max_results))
        if self.fail_search:
            raise WikipediaProviderError("provider failure")
        return [
            WikiSearchResult(
                title="Lionel Messi",
                url="https://en.wikipedia.org/wiki/Lionel_Messi",
                snippet="Argentine footballer",
            )
        ]

    async def lookup(self, title: str, *, language: str) -> WikiPage:
        self.lookup_calls.append((title, language))
        if self.fail_lookup:
            raise WikipediaProviderError("provider failure")
        return WikiPage(
            title=title,
            url="https://vi.wikipedia.org/wiki/Lionel_Messi",
            content="Lionel Messi là một cầu thủ bóng đá người Argentina.",
        )


def test_wiki_search_tool_returns_schema_compatible_results() -> None:
    provider = FakeWikipediaProvider()
    tool = WikiSearchTool(provider)

    result = asyncio.run(
        tool.run(
            WikiSearchInput(
                query="Lionel Messi",
                language="en",
                max_results=3,
            )
        )
    )

    assert result.success is True
    assert result.error is None
    assert result.data == {
        "query": "Lionel Messi",
        "language": "en",
        "results": [
            {
                "title": "Lionel Messi",
                "url": "https://en.wikipedia.org/wiki/Lionel_Messi",
                "snippet": "Argentine footballer",
            }
        ],
        "count": 1,
    }
    assert provider.search_calls == [("Lionel Messi", "en", 3)]


def test_wiki_lookup_tool_returns_page_content() -> None:
    provider = FakeWikipediaProvider()
    tool = WikiLookupTool(provider)

    result = asyncio.run(tool.run(WikiLookupInput(title="Lionel Messi", language="vi")))

    assert result.success is True
    assert result.data == {
        "title": "Lionel Messi",
        "url": "https://vi.wikipedia.org/wiki/Lionel_Messi",
        "content": "Lionel Messi là một cầu thủ bóng đá người Argentina.",
    }
    assert provider.lookup_calls == [("Lionel Messi", "vi")]


def test_wiki_tools_redact_provider_errors() -> None:
    provider = FakeWikipediaProvider()
    provider.fail_search = True
    provider.fail_lookup = True

    search_result = asyncio.run(
        WikiSearchTool(provider).run(WikiSearchInput(query="Messi"))
    )
    lookup_result = asyncio.run(
        WikiLookupTool(provider).run(WikiLookupInput(title="Messi"))
    )

    assert search_result.success is False
    assert search_result.error == "Wikipedia search failed."
    assert search_result.metadata["error_type"] == "WikipediaProviderError"
    assert lookup_result.success is False
    assert lookup_result.error == "Wikipedia lookup failed."
    assert lookup_result.metadata["error_type"] == "WikipediaProviderError"
