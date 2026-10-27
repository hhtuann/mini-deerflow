"""Provider-compatible structured output with Pydantic validation."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, TypeAdapter

from mini_deerflow.model_usage import (
    ModelUsageObservation,
    UsageRecordingModel,
    _clean_name,
    _clean_usage,
    current_scope,
    model_name_of,
)

StructuredOutputMode = Literal["native", "prompt_json"]


@runtime_checkable
class PromptJsonModel(Protocol):
    """Minimum live-model surface needed for JSON specified in the prompt."""

    def invoke(self, messages: object) -> object:
        """Invoke synchronously."""

    async def ainvoke(self, messages: object) -> object:
        """Invoke asynchronously."""


@runtime_checkable
class NativeStructuredOutputModel(Protocol):
    """LangChain's native provider-constrained structured output surface."""

    def with_structured_output[SchemaT: BaseModel](
        self,
        schema: type[SchemaT],
        *,
        method: str,
    ) -> object:
        """Return a provider-native structured runnable."""


@runtime_checkable
class StructuredChatModel(PromptJsonModel, NativeStructuredOutputModel, Protocol):
    """Provider-neutral chat model surface used by the live runtime."""


class NativeStructuredRawRunnable[SchemaT: BaseModel]:
    """Capture SDK usage while preserving ``include_raw=False`` semantics.

    ``include_raw=True`` keeps the provider ``AIMessage`` (with
    ``usage_metadata``) alongside the parsed schema; this wrapper records the
    bounded usage observation from the raw message and returns exactly the
    parsed value callers already expect, including their existing error
    contract. Failed attempts (provider or parsing errors) record an
    observation without usage and re-raise unchanged.
    """

    _IS_RAW_SHAPE = __import__("re").compile(r"^parsedparsing_errorraw$|^parsedraw$")

    def __init__(
        self, runnable: object, *, schema_name: str, model_name: str | None
    ) -> None:
        self._runnable = runnable
        self._schema_name = schema_name
        self._model_name = model_name

    def _record(self, result: object, started: float, *, failed: bool) -> None:
        scope = current_scope()
        if scope is None:
            return
        raw = (
            None
            if failed
            else (result or {}).get("raw")
            if isinstance(result, dict)
            else None
        )
        scope.observations.append(
            ModelUsageObservation(
                schema_name=_clean_name(self._schema_name, 64),
                model_name=self._model_name,
                duration_ms=max(0, round((perf_counter() - started) * 1000)),
                usage=None
                if failed
                else _clean_usage(getattr(raw, "usage_metadata", None)),
                failed=failed,
            )
        )

    def _extract(self, result: object, started: float) -> SchemaT:
        if not isinstance(result, dict) or not self._IS_RAW_SHAPE.match(
            "".join(sorted(result.keys()))
        ):
            # Non-raw shapes (plain schema instances or legacy dict returns)
            # pass through unchanged so downstream type checks still work.
            self._record(result, started, failed=False)
            return cast(SchemaT, result)
        error = result.get("parsing_error")
        if error is not None:
            self._record(result, started, failed=True)
            raise error
        parsed = result.get("parsed")
        if parsed is None:
            self._record(result, started, failed=True)
            raise ValueError("structured output returned no parsed value")
        self._record(result, started, failed=False)
        return cast(SchemaT, parsed)

    def invoke(self, messages: object) -> SchemaT:
        started = perf_counter()
        try:
            result = self._runnable.invoke(messages)
        except BaseException:
            self._record(None, started, failed=True)
            raise
        return self._extract(result, started)

    async def ainvoke(self, messages: object) -> SchemaT:
        started = perf_counter()
        try:
            result = await self._runnable.ainvoke(messages)
        except BaseException:
            self._record(None, started, failed=True)
            raise
        return self._extract(result, started)


class PromptJsonStructuredRunnable[SchemaT: BaseModel]:
    """Parse a normal chat response against a declared Pydantic schema."""

    def __init__(
        self,
        model: PromptJsonModel,
        schema: type[SchemaT],
    ) -> None:
        self._model = model
        self._adapter: TypeAdapter[SchemaT] = TypeAdapter(schema)
        self._wrapper_keys = {
            schema.__name__,
            _camel_to_snake(schema.__name__),
        }

    def invoke(self, messages: object) -> SchemaT:
        return self._parse(self._model.invoke(messages))

    async def ainvoke(self, messages: object) -> SchemaT:
        return self._parse(await self._model.ainvoke(messages))

    def _parse(self, response: object) -> SchemaT:
        content = _text_content(getattr(response, "content", response))
        if content is None:
            raise TypeError("model response content must be text")
        normalized = _strip_code_fence(content)
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            # Preserve Pydantic's existing json_invalid ValidationError contract.
            return self._adapter.validate_json(normalized)
        if (
            isinstance(payload, dict)
            and len(payload) == 1
            and next(iter(payload)) in self._wrapper_keys
        ):
            payload = next(iter(payload.values()))
        return self._adapter.validate_python(payload)


def create_structured_output_runnable[SchemaT: BaseModel](
    model: object,
    schema: type[SchemaT],
    *,
    method: str,
    mode: StructuredOutputMode,
) -> object:
    """Create a validated structured runnable using the selected provider mode.

    The delegate model records one bounded usage observation per actual
    provider invocation (both modes) without changing any return shape, so
    typed outputs, retry behavior and provider call counts are unchanged.
    """

    schema_name = schema.__name__

    if mode == "native":
        with_structured_output = getattr(model, "with_structured_output", None)
        if not callable(with_structured_output):
            raise TypeError("model must support structured output")
        model_name = model_name_of(model)
        try:
            return NativeStructuredRawRunnable(
                with_structured_output(schema, method=method, include_raw=True),
                schema_name=schema_name,
                model_name=model_name,
            )
        except TypeError:
            # Model doubles/providers without ``include_raw`` keep their exact
            # legacy shape; native usage capture is simply unavailable there.
            return with_structured_output(schema, method=method)
    if mode == "prompt_json":
        if not isinstance(model, PromptJsonModel):
            raise TypeError(
                "model must support synchronous and asynchronous invocation"
            )
        recording_model = UsageRecordingModel(model, schema_name)
        return PromptJsonStructuredRunnable(recording_model, schema)
    raise ValueError("unsupported structured output mode")


def _text_content(content: object) -> str | None:
    """Normalize LangChain string or content-block responses to plain text."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) if parts else None


def _strip_code_fence(content: str) -> str:
    """Accept a single fenced JSON response while retaining strict schema checks."""

    normalized = content.strip()
    if not normalized.startswith("```"):
        return normalized
    lines = normalized.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return normalized


def _camel_to_snake(value: str) -> str:
    """Convert a Pydantic model class name to the common provider wrapper key."""

    first_pass = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first_pass).lower()
