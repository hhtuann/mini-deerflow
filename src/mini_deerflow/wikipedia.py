"""Small Wikipedia-only provider used by the educational demo runtime."""

from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from typing import Literal, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

WikiLanguage = Literal["en", "vi"]


class WikipediaProviderError(RuntimeError):
    """Safe provider error that does not expose response bodies."""


class WikiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WikiSearchResult(WikiModel):
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    snippet: str = Field(default="", max_length=4_000)


class WikiPage(WikiModel):
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    content: str = Field(min_length=1, max_length=20_000)


@runtime_checkable
class WikipediaProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        language: WikiLanguage,
        max_results: int,
    ) -> list[WikiSearchResult]: ...

    async def lookup(
        self,
        title: str,
        *,
        language: WikiLanguage,
    ) -> WikiPage: ...


class MediaWikiProvider:
    """Read-only adapter around Wikipedia's public MediaWiki API."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout_seconds = float(timeout_seconds)

    async def search(
        self,
        query: str,
        *,
        language: WikiLanguage,
        max_results: int,
    ) -> list[WikiSearchResult]:
        return await asyncio.to_thread(
            self._search_sync,
            query,
            language,
            max_results,
        )

    async def lookup(
        self,
        title: str,
        *,
        language: WikiLanguage,
    ) -> WikiPage:
        return await asyncio.to_thread(self._lookup_sync, title, language)

    def _search_sync(
        self,
        query: str,
        language: WikiLanguage,
        max_results: int,
    ) -> list[WikiSearchResult]:
        payload = self._request_json(
            language,
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(max_results),
                "format": "json",
                "formatversion": "2",
            },
        )
        query_data = payload.get("query")
        raw_results = query_data.get("search") if isinstance(query_data, dict) else None
        if not isinstance(raw_results, list):
            raise WikipediaProviderError("Wikipedia search returned invalid data.")

        results: list[WikiSearchResult] = []
        for item in raw_results[:max_results]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            snippet = item.get("snippet")
            if not isinstance(title, str) or not title.strip():
                continue
            results.append(
                WikiSearchResult(
                    title=title,
                    url=self._article_url(language, title),
                    snippet=self._plain_text(snippet)[:4_000],
                )
            )
        return results

    def _lookup_sync(self, title: str, language: WikiLanguage) -> WikiPage:
        payload = self._request_json(
            language,
            {
                "action": "query",
                "prop": "extracts|info",
                "inprop": "url",
                "explaintext": "1",
                "redirects": "1",
                "titles": title,
                "format": "json",
                "formatversion": "2",
            },
        )
        query_data = payload.get("query")
        pages = query_data.get("pages") if isinstance(query_data, dict) else None
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            raise WikipediaProviderError("Wikipedia lookup returned invalid data.")

        page = pages[0]
        if page.get("missing") is True:
            raise WikipediaProviderError("Wikipedia page was not found.")
        resolved_title = page.get("title")
        content = page.get("extract")
        full_url = page.get("fullurl")
        if not isinstance(resolved_title, str) or not isinstance(content, str):
            raise WikipediaProviderError("Wikipedia page content was unavailable.")
        if not content.strip():
            raise WikipediaProviderError("Wikipedia page content was empty.")
        url = (
            full_url
            if isinstance(full_url, str) and full_url.startswith("https://")
            else self._article_url(language, resolved_title)
        )
        return WikiPage(
            title=resolved_title,
            url=url,
            content=content[:20_000],
        )

    def _request_json(
        self, language: WikiLanguage, params: dict[str, str]
    ) -> dict[str, object]:
        endpoint = f"https://{language}.wikipedia.org/w/api.php?{urlencode(params)}"
        request = Request(
            endpoint,
            headers={"User-Agent": "MiniDeerFlow/0.1 educational-demo"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(1_000_001)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise WikipediaProviderError("Wikipedia request failed.") from exc
        if len(raw) > 1_000_000:
            raise WikipediaProviderError("Wikipedia response exceeded the size limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WikipediaProviderError("Wikipedia returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise WikipediaProviderError("Wikipedia returned invalid data.")
        return payload

    @staticmethod
    def _article_url(language: WikiLanguage, title: str) -> str:
        slug = quote(title.replace(" ", "_"), safe="()_-'!")
        return f"https://{language}.wikipedia.org/wiki/{slug}"

    @staticmethod
    def _plain_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return unescape(re.sub(r"<[^>]+>", "", value)).strip()
