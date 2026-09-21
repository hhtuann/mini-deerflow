"""Provider-compatible structured output with Pydantic validation."""

from __future__ import annotations

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


class PromptJsonStructuredRunnable[SchemaT: BaseModel]:
    """Parse a normal chat response against a declared Pydantic schema."""

    def __init__(
        self,
        model: PromptJsonModel,
        schema: type[SchemaT],
    ) -> None:
        self._model = model
        self._adapter: TypeAdapter[SchemaT] = TypeAdapter(schema)

    def invoke(self, messages: object) -> SchemaT:
        return self._parse(self._model.invoke(messages))

    async def ainvoke(self, messages: object) -> SchemaT:
        return self._parse(await self._model.ainvoke(messages))

    def _parse(self, response: object) -> SchemaT:
        content = getattr(response, "content", response)
        if not isinstance(content, str):
            raise TypeError("model response content must be text")
        return self._adapter.validate_json(_strip_code_fence(content))


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


def _strip_code_fence(content: str) -> str:
    """Accept a single fenced JSON response while retaining strict schema checks."""

    normalized = content.strip()
    if not normalized.startswith("```"):
        return normalized
    lines = normalized.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return normalized
