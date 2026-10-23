"""Pinned turn budgets must reach future delegated model contexts on resume."""

import asyncio
import json
from unittest.mock import patch

import pytest

from mini_deerflow.actions import ActionDecision, CompleteStepAction, ToolCallAction
from mini_deerflow.answer_synthesis import AnswerSynthesisDraft
from mini_deerflow.config import Settings
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.delegation import (
    BoundedResearcherSubagent,
    BranchFinding,
    BranchResult,
    DelegateResearchTool,
    ScopedResearchTask,
    build_research_task_context,
    parse_delegation_record,
)
from mini_deerflow.review import ReviewDecision, ReviewVerdict
from mini_deerflow.runtime import open_default_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import ToolInput, ToolRegistry


def _arguments():
    return {
        "delegation_id": "pinned-wave",
        "tasks": [
            {
                "branch_id": branch,
                "objective": f"Research {branch} independently. " + "detail " * 60,
                "success_criteria": "Record a bounded independent finding.",
                "tool_call_budget": 1,
            }
            for branch in ("alpha", "beta")
        ],
    }


class _UnusedWebTool:
    name = "web_search"
    description = "Offline research capability."
    input_model = ToolInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input):
        raise AssertionError("No provider call was requested")


class _RecordingSelector:
    def __init__(self):
        self.contexts = []

    async def select_action(self, context):
        self.contexts.append(context)
        return CompleteStepAction(
            action_type="complete_step", summary="Bounded independent finding."
        )


def test_dispatch_budget_override_and_factory_fallback_are_call_local():
    async def scenario():
        researcher_budget = ContextBudget(max_total_chars=40_000)
        tool_budget = ContextBudget(max_total_chars=30_000)
        pinned_budget = ContextBudget(max_total_chars=20_000)
        registry = ToolRegistry([_UnusedWebTool()])
        selector = _RecordingSelector()
        researcher = BoundedResearcherSubagent(
            selector,
            registry,
            context_budget=researcher_budget,
        )
        tool = DelegateResearchTool(researcher, registry, context_budget=tool_budget)

        # Interleave calls through the same objects: each wave owns its policy.
        overridden, fallback = await asyncio.gather(
            tool.run_with_parent_budget(
                _arguments(),
                parent_step_number=1,
                remaining_tool_calls=3,
                context_budget=pinned_budget,
            ),
            tool.run_with_parent_budget(
                _arguments(),
                parent_step_number=1,
                remaining_tool_calls=3,
            ),
        )
        for result in (overridden, fallback):
            record = parse_delegation_record(result)
            assert record is not None
            assert record.fan_in.successful_branches == ["alpha", "beta"]
        assert sorted(
            context.context_projection.budget_chars for context in selector.contexts
        ) == [
            20_000,
            20_000,
            30_000,
            30_000,
        ]
        # Direct callers still get the researcher's own constructor default.
        context = build_research_task_context(
            ScopedResearchTask.model_validate(_arguments()["tasks"][0]),
            registry,
            researcher_budget,
        )
        await researcher.research(context)
        assert selector.contexts[-1].context_projection.budget_chars == 40_000

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "signature", ["legacy", "keyword", "kwargs", "positional_only"]
)
def test_injected_researcher_protocol_remains_compatible(signature):
    captured = []
    budget = ContextBudget(max_total_chars=20_000)

    def result(context, received_budget):
        captured.append((context.context_projection.budget_chars, received_budget))
        return BranchResult(
            branch_id=context.task.branch_id,
            status="success",
            finding=BranchFinding(summary="Injected researcher finding."),
        )

    class Researcher:
        if signature == "legacy":

            async def research(self, context):
                return result(context, None)
        elif signature == "keyword":

            async def research(self, context, *, context_budget=None):
                return result(context, context_budget)
        elif signature == "kwargs":

            async def research(self, context, **kwargs):
                return result(context, kwargs["context_budget"])
        else:

            async def research(self, context, context_budget=None, /):
                return result(context, context_budget)

    tool = DelegateResearchTool(Researcher(), ToolRegistry([_UnusedWebTool()]))
    output = asyncio.run(
        tool.run_with_parent_budget(
            _arguments(),
            parent_step_number=1,
            remaining_tool_calls=3,
            context_budget=budget,
        )
    )
    record = parse_delegation_record(output)
    assert record is not None
    assert record.fan_in.successful_branches == ["alpha", "beta"]
    expected = budget if signature in {"keyword", "kwargs"} else None
    assert captured == [(budget.max_total_chars, expected)] * 2


class _Model:
    def __init__(self):
        self.captures = []
        self.delegated = False

    def with_structured_output(self, schema, *, method):
        assert method == "json_mode"
        model = self

        class Runnable:
            def invoke(self, messages):
                for message in reversed(messages):
                    content = (
                        message[1] if isinstance(message, tuple) else message.content
                    )
                    if not isinstance(content, str):
                        continue
                    if content.lstrip().startswith("{"):
                        raw = content
                        break
                    for tag in ("action_context", "review_context", "answer_context"):
                        opening = f"<{tag}>\n"
                        if opening in content:
                            raw = content.split(opening, 1)[1].split(f"\n</{tag}>", 1)[
                                0
                            ]
                            break
                    else:
                        continue
                    break
                else:
                    raise AssertionError("No production JSON model payload")
                data = json.loads(raw)
                model.captures.append((schema, raw, data))
                if schema is Plan:
                    return Plan(
                        goal=data["goal"],
                        steps=[
                            PlanStep(
                                step_number=number,
                                title="Independent research",
                                objective="Record independent research findings.",
                                success_criteria="The bounded outcome is recorded.",
                            )
                            for number in range(1, 4)
                        ],
                    )
                if schema is ActionDecision:
                    child = data["step"]["title"].startswith("Delegated branch ")
                    if not child and not model.delegated:
                        model.delegated = True
                        return ActionDecision(
                            ToolCallAction(
                                action_type="tool_call",
                                tool_name="delegate_research",
                                arguments=_arguments(),
                            )
                        )
                    return ActionDecision(
                        CompleteStepAction(
                            action_type="complete_step",
                            summary="Bounded outcome recorded.",
                        )
                    )
                if schema is ReviewDecision:
                    return ReviewDecision(
                        ReviewVerdict(
                            verdict="continue" if data["remaining_steps"] else "finish",
                            rationale="The bounded step has completed.",
                        )
                    )
                if schema is AnswerSynthesisDraft:
                    return AnswerSynthesisDraft(
                        language=data["language"],
                        limitations=["Offline research only."],
                    )
                raise AssertionError(f"Unexpected schema: {schema}")

            async def ainvoke(self, messages):
                return self.invoke(messages)

        return Runnable()


def test_reopened_pending_delegation_uses_pinned_budget_for_real_child_model_contexts(
    tmp_path,
):
    async def scenario():
        original = ContextBudget(
            max_total_chars=30_000, max_item_chars=2_000, max_excerpt_chars=700
        )
        reopened = ContextBudget(
            max_total_chars=1_500, max_item_chars=200, max_excerpt_chars=100
        )
        settings = Settings(
            api_key="offline-test", structured_output_mode="native", _env_file=None
        )
        database = tmp_path / "resume.sqlite"
        model = _Model()
        async with open_default_agent_runtime(
            settings,
            tmp_path / "workspace",
            database,
            context_budget=original,
            model_factory=lambda _: model,
        ) as runtime:
            # Fail after decide_action has checkpointed the real pending wave,
            # before either child can run. Resume must execute that wave.
            with (
                patch.object(
                    DelegateResearchTool,
                    "run_with_parent_budget",
                    side_effect=RuntimeError("pending wave"),
                ),
                pytest.raises(RuntimeError, match="pending wave"),
            ):
                await runtime.run(
                    "Compare two independent research perspectives.", thread_id="pinned"
                )
            assert not any(
                data.get("step", {}).get("title", "").startswith("Delegated branch ")
                for _, _, data in model.captures
            )

        resumed_model = _Model()
        resumed_model.delegated = True
        async with open_default_agent_runtime(
            settings,
            tmp_path / "workspace",
            database,
            context_budget=reopened,
            model_factory=lambda _: resumed_model,
        ) as runtime:
            result = await runtime.resume(thread_id="pinned")
            assert result["context_budget"] == original
            (record,) = result["delegations"]
            assert record.fan_in.successful_branches == ["alpha", "beta"]
            children = [
                (raw, data)
                for schema, raw, data in resumed_model.captures
                if schema is ActionDecision
                and data["step"]["title"].startswith("Delegated branch ")
            ]
            assert len(children) == 2
            for raw, data in children:
                assert (
                    data["context_projection"]["budget_chars"]
                    == original.max_total_chars
                )
                assert reopened.max_total_chars < len(raw) <= original.max_total_chars
            assert Plan not in [schema for schema, _, _ in resumed_model.captures]
            count = len(resumed_model.captures)
            assert (await runtime.resume(thread_id="pinned"))["final_answer"] == result[
                "final_answer"
            ]
            assert len(resumed_model.captures) == count

    asyncio.run(scenario())
