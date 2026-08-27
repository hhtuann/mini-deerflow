from typing import Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


class ToolInput(BaseModel):
    """Base class for validated tool inputs."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class ToolResult(BaseModel):
    """Serializable result returned by every tool."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    success: bool
    data: JsonValue | None = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict,
    )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.success:
            if self.data is None:
                raise ValueError(
                    "successful tool result must include data",
                )

            if self.error is not None:
                raise ValueError(
                    "successful tool result must not include an error",
                )

            return self

        if self.data is not None:
            raise ValueError(
                "failed tool result must not include data",
            )

        if self.error is None or not self.error.strip():
            raise ValueError(
                "failed tool result must include an error",
            )

        return self

    @classmethod
    def ok(
        cls,
        data: JsonValue,
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Self:
        """Create a successful tool result."""

        return cls(
            success=True,
            data=data,
            metadata=metadata or {},
        )

    @classmethod
    def fail(
        cls,
        error: str,
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Self:
        """Create a failed tool result."""

        return cls(
            success=False,
            error=error.strip(),
            metadata=metadata or {},
        )


@runtime_checkable
class Tool(Protocol):
    """Structural contract implemented by executable tools."""

    name: str
    description: str
    input_model: type[ToolInput]
    timeout_seconds: float
    idempotent: bool

    async def run(
        self,
        tool_input: ToolInput,
    ) -> ToolResult:
        """Execute the tool with already validated input."""
        ...
