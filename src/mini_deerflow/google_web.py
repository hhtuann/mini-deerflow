"""Google Gemini grounded search and URL Context adapter."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

import httpx
from google import genai
from google.genai import errors, types
from pydantic import SecretStr, ValidationError

from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebFetchError,
    WebProviderErrorCategory,
    WebSearchError,
)
from mini_deerflow.web_safety import SafeWebTarget


@runtime_checkable
class GoogleContentClient(Protocol):
    """Minimum async Google content-generation surface used by the provider."""

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> object:
        """Generate one response with Google-hosted tools enabled."""


class GoogleGenAIContentClient:
    """Production adapter around the native google-genai async client."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        key = api_key.get_secret_value().strip()
        if not key:
            raise ValueError("Google API key must not be blank.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than zero.")

        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=max(1, round(float(timeout_seconds) * 1_000)),
            ),
        )

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> object:
        return await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )


class GoogleWebProvider:
    """Normalize Gemini Search grounding and URL Context into web contracts."""

    def __init__(
        self,
        client: GoogleContentClient,
        *,
        model_name: str,
    ) -> None:
        if not isinstance(client, GoogleContentClient):
            raise TypeError("client must satisfy GoogleContentClient")
        normalized_model = model_name.strip()
        if not normalized_model:
            raise ValueError("model_name must not be blank")

        self._client = client
        self._model_name = normalized_model

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise WebSearchError(
                "Google web search query must not be blank.",
                category=WebProviderErrorCategory.REJECTION,
            )
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise TypeError("max_results must be an integer")
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero")

        response = await self._generate(
            contents=(
                "Search the public web for reliable sources relevant to this "
                "research query. Prefer primary and authoritative sources. "
                "Source identity will be accepted only from Google grounding "
                f"metadata. Query: {normalized_query}"
            ),
            config=types.GenerateContentConfig(
                temperature=0,
                tools=[
                    types.Tool(
                        google_search=types.GoogleSearch(),
                    )
                ],
            ),
            error_type=WebSearchError,
        )
        return _normalize_search_response(response, max_results=max_results)

    async def fetch(self, target: SafeWebTarget) -> FetchedPage:
        if not isinstance(target, SafeWebTarget):
            raise TypeError("fetch target must be SafeWebTarget")

        requested_url = str(target.url)
        response = await self._generate(
            contents=(
                "Use URL Context to retrieve this already safety-validated public "
                "URL and extract its main factual text. Return plain text content "
                "only; the trusted URL will be taken from URL Context metadata. "
                f"URL: {requested_url}"
            ),
            config=types.GenerateContentConfig(
                temperature=0,
                tools=[
                    types.Tool(
                        url_context=types.UrlContext(),
                    )
                ],
            ),
            error_type=WebFetchError,
        )
        return _normalize_fetch_response(
            response,
            requested_url=requested_url,
        )

    async def _generate(
        self,
        *,
        contents: str,
        config: types.GenerateContentConfig,
        error_type: type[WebSearchError | WebFetchError],
    ) -> object:
        try:
            return await self._client.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )
        except errors.APIError as error:
            category = _api_error_category(error.code)
            raise error_type(
                _category_message(category),
                category=category,
            ) from None
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError):
            raise error_type(
                _category_message(WebProviderErrorCategory.TRANSPORT),
                category=WebProviderErrorCategory.TRANSPORT,
            ) from None


def _normalize_search_response(
    response: object,
    *,
    max_results: int,
) -> list[SearchResult]:
    candidate = _first_candidate(
        response,
        error_type=WebSearchError,
    )
    grounding = getattr(candidate, "grounding_metadata", None)
    if grounding is None:
        raise WebSearchError(
            "Google search response lacked grounding metadata.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )

    chunks = getattr(grounding, "grounding_chunks", None) or []
    if not isinstance(chunks, list):
        raise WebSearchError(
            "Google search grounding metadata was invalid.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )
    supports = getattr(grounding, "grounding_supports", None) or []
    snippets = _snippets_by_chunk(supports, chunk_count=len(chunks))

    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for index, chunk in enumerate(chunks):
        web = getattr(chunk, "web", None)
        title = getattr(web, "title", None)
        uri = getattr(web, "uri", None)
        if not isinstance(title, str) or not isinstance(uri, str):
            continue

        try:
            result = SearchResult(
                title=title[:500],
                url=uri,
                snippet=snippets.get(index, "")[:4_000],
            )
        except ValidationError:
            continue

        normalized_url = str(result.url)
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        results.append(result)
        if len(results) >= max_results:
            break

    return results


def _snippets_by_chunk(
    supports: object,
    *,
    chunk_count: int,
) -> dict[int, str]:
    if not isinstance(supports, list):
        return {}

    collected: dict[int, list[str]] = {}
    for support in supports:
        segment = getattr(support, "segment", None)
        text = getattr(segment, "text", None)
        indices = getattr(support, "grounding_chunk_indices", None)
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(indices, list)
        ):
            continue
        snippet = text.strip()
        for index in indices:
            if not isinstance(index, int) or not 0 <= index < chunk_count:
                continue
            bucket = collected.setdefault(index, [])
            if snippet not in bucket:
                bucket.append(snippet)

    return {index: "\n".join(parts)[:4_000] for index, parts in collected.items()}


def _normalize_fetch_response(
    response: object,
    *,
    requested_url: str,
) -> FetchedPage:
    candidate = _first_candidate(
        response,
        error_type=WebFetchError,
    )
    metadata = getattr(candidate, "url_context_metadata", None)
    url_metadata = (
        getattr(metadata, "url_metadata", None) if metadata is not None else None
    )
    if not isinstance(url_metadata, list) or not url_metadata:
        raise WebFetchError(
            "Google URL Context response lacked retrieval metadata.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )

    entry = url_metadata[0]
    status = getattr(entry, "url_retrieval_status", None)
    if status != types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS:
        category = (
            WebProviderErrorCategory.REJECTION
            if status == types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_UNSAFE
            else WebProviderErrorCategory.UPSTREAM
        )
        raise WebFetchError(
            _category_message(category),
            category=category,
        )

    retrieved_url = getattr(entry, "retrieved_url", None)
    if not isinstance(retrieved_url, str) or not retrieved_url.strip():
        raise WebFetchError(
            "Google URL Context response lacked a retrieved URL.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )

    content = getattr(response, "text", None)
    if not isinstance(content, str) or not content.strip():
        raise WebFetchError(
            "Google URL Context response lacked page content.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )

    try:
        redirect_chain = () if retrieved_url == requested_url else (retrieved_url,)
        return FetchedPage(
            url=retrieved_url,
            title=None,
            content=content,
            status_code=200,
            content_type="text/plain",
            redirect_chain=redirect_chain,
        )
    except ValidationError:
        raise WebFetchError(
            "Google URL Context response contained invalid page metadata.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        ) from None


def _first_candidate(
    response: object,
    *,
    error_type: type[WebSearchError | WebFetchError],
) -> object:
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, list) or not candidates:
        raise error_type(
            "Google provider response did not contain a candidate.",
            category=WebProviderErrorCategory.MALFORMED_RESPONSE,
        )
    return candidates[0]


def _api_error_category(status_code: int) -> WebProviderErrorCategory:
    if status_code in (401, 403):
        return WebProviderErrorCategory.AUTHENTICATION
    if status_code == 429:
        return WebProviderErrorCategory.RATE_LIMIT
    if status_code == 404:
        return WebProviderErrorCategory.ENDPOINT
    if 500 <= status_code <= 599:
        return WebProviderErrorCategory.UPSTREAM
    return WebProviderErrorCategory.REJECTION


def _category_message(category: WebProviderErrorCategory) -> str:
    messages = {
        WebProviderErrorCategory.AUTHENTICATION: "Google provider authentication failed.",
        WebProviderErrorCategory.RATE_LIMIT: "Google provider rate limit exceeded.",
        WebProviderErrorCategory.ENDPOINT: "Google provider model or endpoint is unavailable.",
        WebProviderErrorCategory.REJECTION: "Google provider rejected the request.",
        WebProviderErrorCategory.UPSTREAM: "Google provider service failed.",
        WebProviderErrorCategory.TRANSPORT: "Google provider transport failed.",
    }
    return messages.get(category, "Google provider request failed.")
