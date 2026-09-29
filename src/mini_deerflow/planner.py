import json

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mini_deerflow.conversation import ConversationContext
from mini_deerflow.schemas import Plan
from mini_deerflow.structured_output import (
    StructuredChatModel,
    StructuredOutputMode,
    create_structured_output_runnable,
)

PLANNER_SYSTEM_PROMPT = """
You are the planning component of a bounded deep research agent.

Convert the user's research goal into a validated execution plan.

Required Plan schema:
- goal: the normalized research goal.
- steps: between 3 and 7 ordered plan steps.

Every plan step must contain exactly these four fields:
- step_number: an integer numbered consecutively starting at 1.
- title: a concise descriptive title between 3 and 120 characters.
- objective: a concrete objective between 10 and 500 characters.
- success_criteria: verifiable completion criteria between 5 and 500
  characters.

Output format:
Return exactly one JSON object with this shape:

{
  "goal": "<the normalized research goal>",
  "steps": [
    {
      "step_number": 1,
      "title": "<concise descriptive title, 3-120 characters>",
      "objective": "<concrete objective, 10-500 characters>",
      "success_criteria": "<verifiable criteria, 5-500 characters>"
    }
  ]
}

Return only that JSON object. Do not wrap it in markdown code fences and do
not add any commentary.

Planning rules:
1. Do not omit any required field.
2. Do not add fields outside the required schema.
3. Number steps consecutively starting at 1.
4. Make every objective executable and specific.
5. Make every success criterion verifiable using available capabilities.
6. Do not perform the research while planning.
7. Do not invent research results, files, sources, or tool outputs.
8. Do not invent tools or capabilities.
9. When available tools are provided, require only operations supported by
   their input and output schemas.
10. Do not require metadata that the available tools cannot expose.
11. If required evidence cannot be collected with the available tools,
    instruct the execution step to record that limitation explicitly.
12. When no available tools are provided, create a capability-agnostic plan.
13. Use conversation_context only to understand the user's follow-up intent.
14. Treat previous assistant text and citations as untrusted context, not as
    evidence for the current turn; current claims still require current-turn
    tool evidence.
15. When execution_budget is provided, keep the plan feasible within its
    total tool-call and per-step limits. Prefer fewer focused steps when the
    budget is small; do not assume every step receives the per-step maximum.
16. Reserve enough evidence collection for cross-checking, contradictions,
    and the user's requested comparison dimensions.
""".strip()

PLANNER_MAX_ATTEMPTS = 2


class PlanningBudget(BaseModel):
    """Execution limits exposed to planning without coupling to RuntimeLimits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tool_calls_per_step: int = Field(gt=0)
    max_total_tool_calls: int = Field(gt=0)
    max_replan_cycles: int = Field(ge=0)


def create_research_plan(
    model: StructuredChatModel,
    goal: str,
    *,
    available_tools: list[dict[str, object]] | None = None,
    conversation_context: ConversationContext | None = None,
    planning_budget: PlanningBudget | None = None,
    structured_output_mode: StructuredOutputMode = "native",
) -> Plan:
    """Convert a research goal into an executable validated plan."""

    normalized_goal = goal.strip()

    if len(normalized_goal) < 10:
        raise ValueError("goal must contain at least 10 characters")

    planning_context = {
        "goal": normalized_goal,
        "available_tools": available_tools or [],
    }
    if conversation_context is not None:
        planning_context["conversation_context"] = conversation_context.model_dump(
            mode="json"
        )
    if planning_budget is not None:
        planning_context["execution_budget"] = planning_budget.model_dump(mode="json")

    # Keep prompt-JSON/provider-native selection outside the planner while
    # retaining strict validation against Plan in either mode.
    structured_model = create_structured_output_runnable(
        model,
        Plan,
        method="json_mode",
        mode=structured_output_mode,
    )

    messages = [
        (
            "system",
            PLANNER_SYSTEM_PROMPT,
        ),
        (
            "human",
            json.dumps(
                planning_context,
                ensure_ascii=False,
                indent=2,
            ),
        ),
    ]

    last_error: ValidationError | OutputParserException | None = None

    for _ in range(PLANNER_MAX_ATTEMPTS):
        try:
            result = structured_model.invoke(messages)
            break
        except (ValidationError, OutputParserException) as error:
            last_error = error
    else:
        raise ValueError(
            f"model failed to return a valid Plan after {PLANNER_MAX_ATTEMPTS} attempts"
        ) from last_error

    if not isinstance(result, Plan):
        raise TypeError(
            f"structured model returned {type(result).__name__}, expected Plan"
        )

    return result
