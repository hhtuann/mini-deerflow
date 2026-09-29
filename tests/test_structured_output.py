import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from mini_deerflow.structured_output import create_structured_output_runnable


class Result(BaseModel):
    answer: str


class PromptJsonModel:
    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages: object) -> AIMessage:
        del messages
        return AIMessage(content=self._content)

    async def ainvoke(self, messages: object) -> AIMessage:
        return self.invoke(messages)


class NativeRunnable:
    def invoke(self, messages: object) -> Result:
        del messages
        return Result(answer="native")


class NativeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[type[BaseModel], str]] = []

    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
    ) -> NativeRunnable:
        self.calls.append((schema, method))
        return NativeRunnable()


def test_prompt_json_mode_parses_sync_and_async_model_responses() -> None:
    runnable = create_structured_output_runnable(
        PromptJsonModel('{"answer": "prompt"}'),
        Result,
        method="json_mode",
        mode="prompt_json",
    )

    assert runnable.invoke([]) == Result(answer="prompt")
    assert asyncio.run(runnable.ainvoke([])) == Result(answer="prompt")


def test_prompt_json_mode_parses_langchain_content_blocks() -> None:
    class ContentBlockModel:
        def invoke(self, messages: object) -> AIMessage:
            del messages
            return AIMessage(
                content=[
                    {"type": "thinking", "thinking": "internal reasoning"},
                    {"type": "text", "text": '{"answer": "prompt"}'},
                ]
            )

        async def ainvoke(self, messages: object) -> AIMessage:
            return self.invoke(messages)

    runnable = create_structured_output_runnable(
        ContentBlockModel(),
        Result,
        method="json_mode",
        mode="prompt_json",
    )

    assert runnable.invoke([]) == Result(answer="prompt")
    assert asyncio.run(runnable.ainvoke([])) == Result(answer="prompt")


def test_prompt_json_mode_rejects_invalid_json_against_the_schema() -> None:
    runnable = create_structured_output_runnable(
        PromptJsonModel('{"unexpected": "value"}'),
        Result,
        method="json_mode",
        mode="prompt_json",
    )

    with pytest.raises(ValidationError):
        runnable.invoke([])


@pytest.mark.parametrize("wrapper_key", ["Result", "result"])
def test_prompt_json_mode_unwraps_single_schema_named_wrapper(wrapper_key: str) -> None:
    runnable = create_structured_output_runnable(
        PromptJsonModel(f'{{"{wrapper_key}": {{"answer": "wrapped"}}}}'),
        Result,
        method="json_mode",
        mode="prompt_json",
    )

    assert runnable.invoke([]) == Result(answer="wrapped")


def test_prompt_json_mode_rejects_unrelated_or_nonexclusive_wrapper() -> None:
    unrelated = create_structured_output_runnable(
        PromptJsonModel('{"payload": {"answer": "wrapped"}}'),
        Result,
        method="json_mode",
        mode="prompt_json",
    )
    with_sibling = create_structured_output_runnable(
        PromptJsonModel('{"result": {"answer": "wrapped"}, "extra": true}'),
        Result,
        method="json_mode",
        mode="prompt_json",
    )

    with pytest.raises(ValidationError):
        unrelated.invoke([])
    with pytest.raises(ValidationError):
        with_sibling.invoke([])


def test_native_mode_preserves_the_existing_langchain_structured_call() -> None:
    model = NativeModel()

    runnable = create_structured_output_runnable(
        model,
        Result,
        method="json_mode",
        mode="native",
    )

    assert runnable.invoke([]) == Result(answer="native")
    assert model.calls == [(Result, "json_mode")]
