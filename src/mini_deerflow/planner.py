import json

from langchain_core.exceptions import OutputParserException
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from mini_deerflow.schemas import Plan

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
""".strip()

PLANNER_MAX_ATTEMPTS = 2


def create_research_plan(
    model: ChatOpenAI,
    goal: str,
    *,
    available_tools: list[dict[str, object]] | None = None,
) -> Plan:
    """Convert a research goal into an executable validated plan."""

    normalized_goal = goal.strip()

    if len(normalized_goal) < 10:
        raise ValueError("goal must contain at least 10 characters")

    planning_context = {
        "goal": normalized_goal,
        "available_tools": available_tools or [],
    }

    # json_mode: the GLM endpoint intermittently drops required fields such
    # as title from tool-call arguments under function_calling, so the plan
    # is requested as a plain JSON object and still validated against Plan.
    structured_model = model.with_structured_output(
        Plan,
        method="json_mode",
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
