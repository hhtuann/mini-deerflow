from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

NonEmptyText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
    ),
]

WebTitle = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]

WebSnippet = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=4_000,
    ),
]


class WebProviderError(RuntimeError):
    """Base error raised for expected web provider failures."""


class WebSearchError(WebProviderError):
    """Raised when a web search provider cannot complete a request."""


class WebFetchError(WebProviderError):
    """Raised when a web fetch provider cannot retrieve a page."""


class WebPolicyError(WebProviderError):
    """Raised when a URL violates the network access policy."""


class WebModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class SearchResult(WebModel):
    title: WebTitle
    url: HttpUrl
    snippet: WebSnippet = ""


class FetchedPage(WebModel):
    url: HttpUrl
    title: WebTitle | None = None
    content: str
    status_code: int = Field(ge=100, le=599)
    content_type: str | None = None


@runtime_checkable
class WebSearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        """Return normalized web search results."""


@runtime_checkable
class WebFetchProvider(Protocol):
    async def fetch(
        self,
        url: str,
    ) -> FetchedPage:
        """Fetch and normalize one public web page."""
