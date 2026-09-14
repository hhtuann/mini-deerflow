import asyncio

import pytest

from mini_deerflow.web import WebPolicyError, WebProviderErrorCategory
from mini_deerflow.web_safety import (
    PublicWebTargetValidator,
    WebSafetyErrorCode,
)


class FakeResolver:
    def __init__(
        self,
        answers: tuple[str, ...] = ("93.184.216.34",),
        *,
        error: Exception | None = None,
    ) -> None:
        self.answers = answers
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        if self.error is not None:
            raise self.error
        return self.answers


def test_public_hostname_returns_typed_safe_target() -> None:
    resolver = FakeResolver(("2606:4700:4700::1111", "1.1.1.1"))
    validator = PublicWebTargetValidator(resolver)

    target = asyncio.run(validator.validate("https://Example.COM./article?q=1"))

    assert target.hostname == "example.com"
    assert target.port == 443
    assert target.resolved_addresses == ("1.1.1.1", "2606:4700:4700::1111")
    assert resolver.calls == [("example.com", 443)]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "http://192.0.2.1/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[ff02::1]/",
        "http://[::]/",
    ],
)
def test_non_public_ip_literals_are_denied_without_dns(url: str) -> None:
    resolver = FakeResolver()
    validator = PublicWebTargetValidator(resolver)

    with pytest.raises(WebPolicyError) as exc_info:
        asyncio.run(validator.validate(url))

    assert exc_info.value.category is WebProviderErrorCategory.SAFETY
    assert exc_info.value.code is WebSafetyErrorCode.NON_PUBLIC_ADDRESS
    assert resolver.calls == []
    assert url not in str(exc_info.value)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://user:password@example.com/", WebSafetyErrorCode.USERINFO_NOT_ALLOWED),
        ("http://localhost/", WebSafetyErrorCode.HOST_NOT_ALLOWED),
        ("http://service.local/", WebSafetyErrorCode.HOST_NOT_ALLOWED),
        ("http://[::1", WebSafetyErrorCode.MALFORMED_URL),
        ("ftp://example.com/file", WebSafetyErrorCode.MALFORMED_URL),
    ],
)
def test_malformed_userinfo_and_local_names_are_denied(
    url: str,
    code: WebSafetyErrorCode,
) -> None:
    resolver = FakeResolver()

    with pytest.raises(WebPolicyError) as exc_info:
        asyncio.run(PublicWebTargetValidator(resolver).validate(url))

    assert exc_info.value.code is code
    assert resolver.calls == []


def test_mixed_public_and_private_dns_answers_are_denied() -> None:
    resolver = FakeResolver(("93.184.216.34", "10.0.0.4"))

    with pytest.raises(WebPolicyError) as exc_info:
        asyncio.run(PublicWebTargetValidator(resolver).validate("https://example.com/"))

    assert exc_info.value.code is WebSafetyErrorCode.NON_PUBLIC_ADDRESS
    assert resolver.calls == [("example.com", 443)]


@pytest.mark.parametrize(
    ("resolver", "expected_code"),
    [
        (FakeResolver(()), WebSafetyErrorCode.DNS_NO_RESULTS),
        (
            FakeResolver(error=OSError("internal resolver detail")),
            WebSafetyErrorCode.DNS_RESOLUTION_FAILED,
        ),
        (FakeResolver(("not-an-address",)), WebSafetyErrorCode.DNS_RESOLUTION_FAILED),
    ],
)
def test_dns_failures_are_controlled_and_redacted(
    resolver: FakeResolver,
    expected_code: WebSafetyErrorCode,
) -> None:
    with pytest.raises(WebPolicyError) as exc_info:
        asyncio.run(PublicWebTargetValidator(resolver).validate("https://example.com/"))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert exc_info.value.code is expected_code
    assert "internal resolver detail" not in rendered
    assert "example.com" not in rendered
