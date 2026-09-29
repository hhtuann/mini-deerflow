import asyncio
import json
from typing import Any, Self

import pytest

from mini_deerflow import wikipedia as wikipedia_module
from mini_deerflow.wikipedia import MediaWikiProvider, WikipediaProviderError


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | bytes) -> None:
        self._payload = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._payload


def test_mediawiki_search_parses_results_without_network(monkeypatch) -> None:
    seen_urls: list[str] = []

    def fake_urlopen(request, *, timeout: float):
        seen_urls.append(request.full_url)
        assert timeout == 7.5
        return FakeResponse(
            {
                "query": {
                    "search": [
                        {
                            "title": "Lionel Messi",
                            "snippet": '<span class="searchmatch">Argentine</span> footballer',
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(wikipedia_module, "urlopen", fake_urlopen)
    provider = MediaWikiProvider(timeout_seconds=7.5)

    results = asyncio.run(provider.search("Lionel Messi", language="en", max_results=3))

    assert len(results) == 1
    assert results[0].title == "Lionel Messi"
    assert str(results[0].url) == "https://en.wikipedia.org/wiki/Lionel_Messi"
    assert results[0].snippet == "Argentine footballer"
    assert seen_urls and seen_urls[0].startswith("https://en.wikipedia.org/w/api.php?")
    assert "srsearch=Lionel+Messi" in seen_urls[0]


def test_mediawiki_lookup_parses_plain_text_page_without_network(monkeypatch) -> None:
    def fake_urlopen(request, *, timeout: float):
        assert request.full_url.startswith("https://vi.wikipedia.org/w/api.php?")
        assert timeout == 15.0
        return FakeResponse(
            {
                "query": {
                    "pages": [
                        {
                            "title": "Lionel Messi",
                            "extract": "Lionel Messi là một cầu thủ bóng đá người Argentina.",
                            "fullurl": "https://vi.wikipedia.org/wiki/Lionel_Messi",
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(wikipedia_module, "urlopen", fake_urlopen)
    provider = MediaWikiProvider()

    page = asyncio.run(provider.lookup("Lionel Messi", language="vi"))

    assert page.title == "Lionel Messi"
    assert str(page.url) == "https://vi.wikipedia.org/wiki/Lionel_Messi"
    assert "Argentina" in page.content


def test_mediawiki_provider_rejects_invalid_json_without_leaking_body(
    monkeypatch,
) -> None:
    def fake_urlopen(_request, *, timeout: float):
        assert timeout == 15.0
        return FakeResponse(b"secret provider body that is not json")

    monkeypatch.setattr(wikipedia_module, "urlopen", fake_urlopen)
    provider = MediaWikiProvider()

    with pytest.raises(WikipediaProviderError, match="invalid JSON") as exc_info:
        asyncio.run(provider.search("Messi", language="en", max_results=1))

    assert "secret provider body" not in str(exc_info.value)
