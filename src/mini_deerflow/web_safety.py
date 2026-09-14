import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from enum import Enum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from mini_deerflow.web import WebPolicyError, WebProviderErrorCategory


class WebSafetyErrorCode(str, Enum):
    """Controlled denial codes that never contain an untrusted target."""

    MALFORMED_URL = "malformed_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    USERINFO_NOT_ALLOWED = "userinfo_not_allowed"
    HOST_NOT_ALLOWED = "host_not_allowed"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    DNS_NO_RESULTS = "dns_no_results"
    NON_PUBLIC_ADDRESS = "non_public_address"


class SafeWebTarget(BaseModel):
    """A validated public HTTP(S) target passed across the fetch boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    hostname: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65_535)
    resolved_addresses: tuple[str, ...] = Field(min_length=1, max_length=32)


@runtime_checkable
class HostResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        """Resolve one hostname without performing an HTTP request."""


@runtime_checkable
class WebTargetValidator(Protocol):
    async def validate(self, url: str | HttpUrl) -> SafeWebTarget:
        """Return a typed target only when every resolved address is public."""


class SystemHostResolver:
    """Production resolver seam; deterministic tests inject a fake resolver."""

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        return await asyncio.to_thread(self._resolve_sync, hostname, port)

    @staticmethod
    def _resolve_sync(hostname: str, port: int) -> tuple[str, ...]:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return tuple(sorted({record[4][0] for record in records}))


class PublicWebTargetValidator:
    """Deny non-public web targets before a fetch provider is invoked."""

    def __init__(self, resolver: HostResolver) -> None:
        if not isinstance(resolver, HostResolver):
            raise TypeError("resolver must satisfy HostResolver")
        self._resolver = resolver

    async def validate(self, url: str | HttpUrl) -> SafeWebTarget:
        rendered_url = str(url)

        try:
            parsed = urlsplit(rendered_url)
            validated_url = HttpUrl(rendered_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise self._denial(WebSafetyErrorCode.MALFORMED_URL) from None

        if parsed.scheme.lower() not in {"http", "https"}:
            raise self._denial(WebSafetyErrorCode.UNSUPPORTED_SCHEME)

        if parsed.username is not None or parsed.password is not None:
            raise self._denial(WebSafetyErrorCode.USERINFO_NOT_ALLOWED)

        hostname = parsed.hostname
        if hostname is None:
            raise self._denial(WebSafetyErrorCode.MALFORMED_URL)

        hostname = hostname.rstrip(".").lower()
        if (
            not hostname
            or len(hostname) > 253
            or "%" in hostname
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local"))
        ):
            raise self._denial(WebSafetyErrorCode.HOST_NOT_ALLOWED)

        resolved_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        literal = self._parse_address(hostname)

        if literal is not None:
            addresses = (literal,)
        else:
            try:
                answers = await self._resolver.resolve(hostname, resolved_port)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - normalize the resolver seam
                raise self._denial(WebSafetyErrorCode.DNS_RESOLUTION_FAILED) from None

            if not answers:
                raise self._denial(WebSafetyErrorCode.DNS_NO_RESULTS)

            parsed_answers: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
            for answer in answers:
                parsed_answer = self._parse_address(answer)
                if parsed_answer is None:
                    raise self._denial(WebSafetyErrorCode.DNS_RESOLUTION_FAILED)
                parsed_answers.add(parsed_answer)
            if len(parsed_answers) > 32:
                raise self._denial(WebSafetyErrorCode.DNS_RESOLUTION_FAILED)
            addresses = tuple(
                sorted(
                    parsed_answers, key=lambda address: (address.version, int(address))
                )
            )

        if not all(self._is_public(address) for address in addresses):
            raise self._denial(WebSafetyErrorCode.NON_PUBLIC_ADDRESS)

        return SafeWebTarget(
            url=validated_url,
            hostname=hostname,
            port=resolved_port,
            resolved_addresses=tuple(str(address) for address in addresses),
        )

    @staticmethod
    def _parse_address(
        value: str,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_public(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return bool(
            address.is_global
            and not address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and not address.is_reserved
        )

    @staticmethod
    def _denial(code: WebSafetyErrorCode) -> WebPolicyError:
        return WebPolicyError(
            "Web fetch target was denied by the safety policy.",
            category=WebProviderErrorCategory.SAFETY,
            code=code,
        )
