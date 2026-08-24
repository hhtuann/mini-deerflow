from langchain_openai import ChatOpenAI

from mini_deerflow.schemas import Plan

PLANNER_SYSTEM_PROMPT = """
You are the planning component of a deep research agent.

Convert the user's research goal into a bounded execution plan.

Requirements:
- Create between 3 and 7 steps.
- Number steps consecutively starting at 1.
- Each step must have a concrete objective.
- Each step must have verifiable success criteria.
- Do not perform the research yet.
- Do not invent results or sources.
""".strip()


def create_research_plan(
    model: ChatOpenAI,
    goal: str,
) -> Plan:
    """Convert a research goal into a validated execution plan."""

    normalized_goal = goal.strip()

    if len(normalized_goal) < 10:
        raise ValueError("goal must contain at least 10 characters")

    structured_model = model.with_structured_output(
        Plan,
        method="function_calling",
    )

    result = structured_model.invoke(
        [
            ("system", PLANNER_SYSTEM_PROMPT),
            ("human", normalized_goal),
        ]
    )

    if not isinstance(result, Plan):
        raise TypeError(
            f"structured model returned {type(result).__name__}, expected Plan"
        )

    return result
