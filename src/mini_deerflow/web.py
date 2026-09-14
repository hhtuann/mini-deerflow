import asyncio
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    ValidationError,
)

DEFAULT_WEB_RESPONSE_BYTES = 2_000_000

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


@dataclass(frozen=True, slots=True)
class WebHttpResponse:
    status_code: int
    body: bytes
    content_type: str | None = None


@runtime_checkable
class WebHttpClient(Protocol):
    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WebHttpResponse:
        """POST JSON and return a size-bounded response."""


class UrllibWebHttpClient:
    """Minimal async HTTP boundary backed by the Python standard library."""

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WebHttpResponse:
        return await asyncio.to_thread(
            self._post_json_sync,
            url,
            payload,
            headers,
            timeout_seconds,
            max_response_bytes,
        )

    @staticmethod
    def _post_json_sync(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> WebHttpResponse:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(max_response_bytes + 1)

                if len(body) > max_response_bytes:
                    raise WebTransportError("Web response exceeded the size limit.")

                return WebHttpResponse(
                    status_code=response.status,
                    body=body,
                    content_type=response.headers.get_content_type(),
                )
        except HTTPError as error:
            return WebHttpResponse(
                status_code=error.code,
                body=b"",
                content_type=None,
            )
        except (TimeoutError, URLError, OSError) as error:
            raise WebTransportError("Web request failed.") from error


class JinaWebProvider:
    """Jina Search and Reader provider with an injectable HTTP client."""

    search_endpoint = "https://s.jina.ai/"
    fetch_endpoint = "https://r.jina.ai/"

    def __init__(
        self,
        http_client: WebHttpClient,
        *,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = DEFAULT_WEB_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(http_client, WebHttpClient):
            raise TypeError("http_client must satisfy WebHttpClient")

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than zero")

        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be greater than zero")

        self._http_client = http_client
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        if self._api_key_value() is None:
            raise WebSearchError(
                "Web search configuration error: a Jina API key is required.",
                category=WebProviderErrorCategory.CONFIGURATION,
            )

        response = await self._request(
            self.search_endpoint,
            {
                "q": query,
                "num": max_results,
            },
            error_type=WebSearchError,
        )
        data = response.get("data")

        if not isinstance(data, list):
            raise WebSearchError(
                "Search response did not contain a result list.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            )

        results: list[SearchResult] = []

        for item in data[:max_results]:
            if not isinstance(item, dict):
                continue

            title = item.get("title")
            url = item.get("url")
            description = item.get("description", "")

            if not isinstance(title, str) or not isinstance(url, str):
                continue

            if not isinstance(description, str):
                description = ""

            try:
                results.append(
                    SearchResult(
                        title=title[:500],
                        url=url,
                        snippet=description[:4_000],
                    )
                )
            except ValidationError:
                continue

        return results

    async def fetch(self, target: "SafeWebTarget") -> FetchedPage:
        from mini_deerflow.web_safety import SafeWebTarget

        if not isinstance(target, SafeWebTarget):
            raise TypeError("fetch target must be SafeWebTarget")
        url = str(target.url)
        response = await self._request(
            self.fetch_endpoint,
            {"url": url},
            error_type=WebFetchError,
        )
        data = response.get("data")

        if not isinstance(data, dict):
            raise WebFetchError(
                "Fetch response did not contain page data.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            )

        resolved_url = data.get("url", url)
        title = data.get("title")
        content = data.get("content")
        status_code = response.get("code", 200)

        if not isinstance(resolved_url, str) or not isinstance(content, str):
            raise WebFetchError(
                "Fetch response page data was invalid.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            )

        if title is not None and not isinstance(title, str):
            title = None

        if not isinstance(status_code, int) or not 100 <= status_code <= 599:
            status_code = 200

        try:
            redirect_chain = () if resolved_url == url else (resolved_url,)
            return FetchedPage(
                url=resolved_url,
                title=title[:500] if title else None,
                content=content,
                status_code=status_code,
                content_type="text/markdown",
                redirect_chain=redirect_chain,
            )
        except ValidationError:
            raise WebFetchError(
                "Fetch response page data was invalid.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            ) from None

    def _api_key_value(self) -> str | None:
        if self._api_key is None:
            return None

        value = self._api_key.get_secret_value().strip()
        return value or None

    async def _request(
        self,
        endpoint: str,
        payload: dict[str, object],
        *,
        error_type: type[WebSearchError] | type[WebFetchError],
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        api_key = self._api_key_value()

        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await self._http_client.post_json(
                endpoint,
                payload,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
                max_response_bytes=self._max_response_bytes,
            )
        except (
            WebTransportError,
            TimeoutError,
            ConnectionError,
            OSError,
        ):
            raise error_type(
                "Provider transport failed.",
                category=WebProviderErrorCategory.TRANSPORT,
            ) from None

        if response.status_code < 200 or response.status_code >= 300:
            raise error_type(
                _http_error_message(response.status_code),
                category=_http_error_category(response.status_code),
            )

        try:
            decoded = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise error_type(
                "Provider returned invalid JSON.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            ) from None

        if not isinstance(decoded, dict):
            raise error_type(
                "Provider returned an invalid response object.",
                category=WebProviderErrorCategory.MALFORMED_RESPONSE,
            )

        return decoded


def _http_error_category(status_code: int) -> WebProviderErrorCategory:
    if status_code in (401, 403):
        return WebProviderErrorCategory.AUTHENTICATION

    if status_code == 429:
        return WebProviderErrorCategory.RATE_LIMIT

    if status_code in (404, 405):
        return WebProviderErrorCategory.ENDPOINT

    if 500 <= status_code <= 599:
        return WebProviderErrorCategory.UPSTREAM

    return WebProviderErrorCategory.REJECTION


def _http_error_message(status_code: int) -> str:
    category = _http_error_category(status_code)

    if category is WebProviderErrorCategory.AUTHENTICATION:
        return f"Provider authentication failed (HTTP {status_code})."

    if category is WebProviderErrorCategory.RATE_LIMIT:
        return "Provider rate limit exceeded (HTTP 429)."

    if category is WebProviderErrorCategory.ENDPOINT:
        return f"Provider endpoint rejected the request (HTTP {status_code})."

    if category is WebProviderErrorCategory.UPSTREAM:
        return f"Provider service failed (HTTP {status_code})."

    return f"Provider rejected the request (HTTP {status_code})."
