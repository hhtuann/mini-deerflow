"""Provider-compatible structured output with Pydantic validation."""

from __future__ import annotations

import json
import re
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, TypeAdapter

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
    """Create a validated structured runnable using the selected provider mode."""

    if mode == "native":
        with_structured_output = getattr(model, "with_structured_output", None)
        if not callable(with_structured_output):
            raise TypeError("model must support structured output")
        return with_structured_output(schema, method=method)
    if mode == "prompt_json":
        if not isinstance(model, PromptJsonModel):
            raise TypeError(
                "model must support synchronous and asynchronous invocation"
            )
        return PromptJsonStructuredRunnable(model, schema)
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
