"""Per-invocation provider usage observation, without raw payload capture.

A context-variable scope collects one :class:`ModelUsageObservation` per actual
model invocation (including failed attempts). The structured-output seam
records observations; the tracer drains them inside the executing graph node so
every observation is attributed to the correct run, turn, node, delegation and
branch. No prompts, provider payloads, secrets or private reasoning are ever
stored — only schema category, model identity, latency and provider-reported
token counts.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

MAX_TOKENS_VALUE = 10_000_000_000
MAX_MODEL_NAME_CHARS = 64
MAX_SCHEMA_NAME_CHARS = 64

# Usage keys accepted from ``AIMessage.usage_metadata`` (SDK-reported only).
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens")


def _clean_int(value: object) -> int | None:
    """Accept only genuine nonnegative bounded ints; anything else is unknown."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_TOKENS_VALUE:
        return None
    return value


def _clean_usage(usage: object) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    cleaned = {key: _clean_int(usage.get(key)) for key in _USAGE_KEYS}
    if all(value is None for value in cleaned.values()):
        return None
    return cleaned


def _clean_name(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or any(ord(char) < 32 for char in normalized):
        return None
    return normalized[:limit]


@dataclass(slots=True)
class ModelUsageObservation:
    """One actual model invocation: bounded identity, latency and SDK usage."""

    schema_name: str | None
    model_name: str | None
    duration_ms: int | None = None
    usage: dict[str, int] | None = None
    failed: bool = False


@dataclass(slots=True)
class ModelUsageScope:
    """Node-scoped collector; one scope per executing graph node."""

    observations: list[ModelUsageObservation] = field(default_factory=list)


_scope: ContextVar[ModelUsageScope | None] = ContextVar(
    "mini_deerflow_model_usage_scope", default=None
)


def open_scope() -> tuple[ModelUsageScope, Token]:
    """Open a fresh collection scope (nested scopes capture independently)."""

    scope = ModelUsageScope()
    token = _scope.set(scope)
    return scope, token


def close_scope(token: Token) -> None:
    _scope.reset(token)


def current_scope() -> ModelUsageScope | None:
    """Return the active collection scope, if any."""

    return _scope.get()


def model_name_of(model: object) -> str | None:
    return _clean_name(getattr(model, "model_name", None), MAX_MODEL_NAME_CHARS)


class UsageRecordingModel:
    """Delegate that records one observation per real provider invocation.

    Works for both structured-output modes because the delegate sits *under*
    the structured wrapper: prompt_json calls ``invoke`` directly, and native
    ``with_structured_output`` chains the delegate before parsing. The return
    shape is unchanged, so typed outputs and provider call counts are
    preserved exactly.
    """

    def __init__(self, model: object, schema_name: str) -> None:
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_schema_name", schema_name)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_model"), name)

    def _record(
        self, response: object, started: float, *, failed: bool = False
    ) -> None:
        scope = _scope.get()
        if scope is None:
            return
        usage = (
            None if failed else _clean_usage(getattr(response, "usage_metadata", None))
        )
        scope.observations.append(
            ModelUsageObservation(
                schema_name=_clean_name(
                    object.__getattribute__(self, "_schema_name"),
                    MAX_SCHEMA_NAME_CHARS,
                ),
                model_name=model_name_of(object.__getattribute__(self, "_model")),
                duration_ms=max(0, round((perf_counter() - started) * 1000)),
                usage=usage,
                failed=failed,
            )
        )

    def invoke(self, messages: object, *args: object, **kwargs: object) -> object:
        started = perf_counter()
        try:
            response = object.__getattribute__(self, "_model").invoke(
                messages, *args, **kwargs
            )
        except BaseException:
            self._record(None, started, failed=True)
            raise
        self._record(response, started)
        return response

    async def ainvoke(
        self, messages: object, *args: object, **kwargs: object
    ) -> object:
        started = perf_counter()
        try:
            response = await object.__getattribute__(self, "_model").ainvoke(
                messages, *args, **kwargs
            )
        except BaseException:
            self._record(None, started, failed=True)
            raise
        self._record(response, started)
        return response
