from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from mini_deerflow.tools.contracts import ToolResult

ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    ),
]

CompletionSummary = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=4_000,
    ),
]


class ActionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class ToolCallAction(ActionModel):
    type: Literal["tool_call"] = Field(
        alias="action_type",
    )
    tool_name: ToolName
    arguments: dict[str, JsonValue] = Field(
        default_factory=dict,
    )


class CompleteStepAction(ActionModel):
    type: Literal["complete_step"] = Field(
        alias="action_type",
    )
    summary: CompletionSummary
    sources: list[HttpUrl] = Field(
        default_factory=list,
        max_length=20,
    )


AgentAction = Annotated[
    ToolCallAction | CompleteStepAction,
    Field(discriminator="type"),
]


class ToolObservation(ActionModel):
    step_number: int = Field(
        ge=1,
        le=7,
    )
    step_tool_call_number: int = Field(
        ge=1,
    )
    total_tool_call_number: int = Field(
        ge=1,
    )
    action: ToolCallAction
    result: ToolResult

    @model_validator(mode="after")
    def validate_call_numbers(self) -> Self:
        if self.step_tool_call_number > self.total_tool_call_number:
            raise ValueError(
                "step_tool_call_number cannot exceed total_tool_call_number"
            )

        return self


class ActionDecision(RootModel[AgentAction]):
    model_config = ConfigDict(
        frozen=True,
    )


def parse_agent_action(payload: object) -> AgentAction:
    return ActionDecision.model_validate(payload).root
