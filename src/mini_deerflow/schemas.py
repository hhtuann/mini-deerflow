from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanStep(BaseModel):
    """One independently verifiable step in a research plan."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    step_number: int = Field(
        ge=1,
        le=7,
    )

    title: str = Field(
        min_length=3,
        max_length=120,
    )

    objective: str = Field(
        min_length=10,
        max_length=500,
    )

    success_criteria: str = Field(
        min_length=5,
        max_length=500,
    )


class Plan(BaseModel):
    """A bounded multi-step plan for one research goal."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    goal: str = Field(
        min_length=10,
        max_length=1_000,
    )

    steps: list[PlanStep] = Field(
        min_length=3,
        max_length=7,
    )

    @model_validator(mode="after")
    def validate_step_numbers(self) -> Self:
        actual = [step.step_number for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))

        if actual != expected:
            raise ValueError(
                f"step_number must be consecutive starting at 1; "
                f"expected {expected}, received {actual}"
            )

        return self
