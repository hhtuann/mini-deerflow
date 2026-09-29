from enum import Enum
from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
)

if TYPE_CHECKING:
    from mini_deerflow.web_safety import SafeWebTarget, WebSafetyErrorCode

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


class WebProviderErrorCategory(str, Enum):
    """Safe classifications exposed at the provider and tool boundaries."""

    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    UPSTREAM = "upstream"
    ENDPOINT = "endpoint"
    REJECTION = "provider_rejection"
    TRANSPORT = "transport"
    MALFORMED_RESPONSE = "malformed_response"
    SAFETY = "safety_denial"
    UNKNOWN = "unknown"


class WebProviderError(RuntimeError):
    """Base error raised for expected web provider failures."""

    def __init__(
        self,
        message: str,
        *,
        category: WebProviderErrorCategory = WebProviderErrorCategory.UNKNOWN,
        code: "WebSafetyErrorCode | None" = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code


class WebSearchError(WebProviderError):
    """Raised when a web search provider cannot complete a request."""


class WebFetchError(WebProviderError):
    """Raised when a web fetch provider cannot retrieve a page."""


class WebPolicyError(WebProviderError):
    """Raised when a URL violates the network access policy."""


class WebTransportError(WebProviderError):
    """Raised when the HTTP transport cannot complete a request."""


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
    redirect_chain: tuple[HttpUrl, ...] = Field(
        default_factory=tuple,
        max_length=10,
        exclude=True,
    )


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
        target: "SafeWebTarget",
    ) -> FetchedPage:
        """Fetch a prevalidated page and report every known redirect target."""


@runtime_checkable
class WebProvider(WebSearchProvider, WebFetchProvider, Protocol):
    """Combined provider used by the default research runtime."""
