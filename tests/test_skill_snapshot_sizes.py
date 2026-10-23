"""Encoded skill boundaries and fidelity through the durable runtime."""

import asyncio
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.config import Settings
from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.persistence import open_sqlite_checkpointer
from mini_deerflow.runtime import (
    build_agent_runtime,
    create_default_agent_runtime,
    open_default_agent_runtime,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.skills import (
    MAX_SKILL_INSTRUCTION_BYTES,
    MAX_SKILL_SNAPSHOT_JSON_CHARS,
    SkillContext,
    SkillsError,
    SkillsRegistry,
)
from mini_deerflow.tools import ToolRegistry


class _UnusedModel:
    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, *args, **kwargs):
        raise AssertionError("Rejected activation must not invoke the model")

    async def ainvoke(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)


def _write_skill(root, instructions, *, description="Snapshot boundary test"):
    directory = root / "snapshot"
    directory.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": "snapshot",
        "description": description,
        "applicability": "Test durable instruction fidelity.",
        "version": "1.0.0",
    }
    (directory / "SKILL.md").write_bytes(
        ("---\n" + json.dumps(metadata) + "\n---\n" + instructions).encode("utf-8")
    )
    return SkillsRegistry(root)


def test_newline_expansion_fails_during_activation_before_reservation(tmp_path):
    async def scenario():
        instructions = "X" + "\n" * 32766 + "X"
        assert len(instructions.encode("utf-8")) == 32768
        registry = _write_skill(tmp_path / "skills", instructions)
        assert registry.discover()[0].id == "snapshot"
        settings = Settings(
            api_key="offline-test", structured_output_mode="native", _env_file=None
        )
        database = tmp_path / "conversation.sqlite"
        repository = SQLiteConversationRepository(database)
        async with open_default_agent_runtime(
            settings,
            tmp_path / "workspace",
            database,
            skills_registry=registry,
            model_factory=lambda _: _UnusedModel(),
        ) as runtime:
            with (
                patch.object(
                    SQLiteConversationRepository,
                    "create_conversation_with_turn",
                    autospec=True,
                    side_effect=SQLiteConversationRepository.create_conversation_with_turn,
                ) as reserve,
                pytest.raises(SkillsError, match="serialized skill snapshot"),
            ):
                await runtime.run(
                    "Research a bounded snapshot boundary.",
                    thread_id="escaping",
                    turn_id="first",
                    skill_id="snapshot",
                )
            reserve.assert_not_called()
        assert await repository.list_conversations() == ()

    asyncio.run(scenario())


def _encoded(skill):
    return json.dumps(skill.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _boundary_skill(root, size=56000):
    # Interior controls survive strip; description escaping is part of the limit.
    description = "D" + "\x01" * 510 + "D"
    body = "X" + '\\"\x01\n' * 4000 + "X"
    template = SkillContext(
        id="snapshot", fingerprint="0" * 64, instructions=body, description=description
    )
    body += "x" * (size - len(_encoded(template)))
    registry = _write_skill(root, body, description=description)
    content = (root / "snapshot" / "SKILL.md").read_bytes()
    expected = template.model_copy(
        update={
            "instructions": body,
            "fingerprint": hashlib.sha256(content).hexdigest(),
        }
    )
    assert len(_encoded(expected)) == size
    assert len(body.encode("utf-8")) <= 32768
    return registry, expected


@pytest.mark.parametrize("size", [56000, 56001])
def test_complete_escaped_snapshot_boundary(tmp_path, size):
    assert MAX_SKILL_INSTRUCTION_BYTES == 32768
    assert MAX_SKILL_SNAPSHOT_JSON_CHARS == 56000
    registry, expected = _boundary_skill(tmp_path / "skills", size)
    # Both body-only and metadata-omitting accounting would accept 56001.
    assert len(json.dumps(expected.instructions)) < 56000
    without_description = expected.model_dump(mode="json", exclude={"description"})
    assert len(json.dumps(without_description, ensure_ascii=False)) < 56000
    if size == 56000:
        assert registry.activate("snapshot") == expected
    else:
        with pytest.raises(SkillsError, match="serialized skill snapshot"):
            registry.activate("snapshot")


@pytest.mark.parametrize("body", ["a" * 32768, "😀" * 8192], ids=["ascii", "emoji"])
def test_raw_utf8_byte_boundary(tmp_path, body):
    assert len(body.encode("utf-8")) == 32768
    registry = _write_skill(tmp_path / "skills", body)
    assert registry.activate("snapshot").instructions == body
    # A single ASCII byte crosses the UTF-8 boundary even for multibyte text.
    for oversized in (body + "x", " " + body, body + "\n"):
        registry = _write_skill(tmp_path / "skills", oversized)
        assert len(oversized.encode("utf-8")) == 32769
        with pytest.raises(SkillsError, match="instructions exceed the byte limit"):
            registry.activate("snapshot")


def _plan(goal):
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=n,
                title=f"Step {n}",
                objective="Check snapshot fidelity.",
                success_criteria="Snapshot is retained.",
            )
            for n in range(1, 4)
        ],
    )


class _Selector:
    def __init__(self, *, interrupt=False):
        self.interrupt = interrupt
        self.contexts = []

    async def select_action(self, context):
        self.contexts.append(context)
        if self.interrupt and context.step.step_number == 2:
            raise RuntimeError("controlled interruption")
        return CompleteStepAction(type="complete_step", summary="Checked snapshot.")


def _runtime(checkpointer, repository, registry, selector, planner=_plan):
    return build_agent_runtime(
        planner,
        selector,
        ToolRegistry(),
        checkpointer=checkpointer,
        conversation_repository=repository,
        skills_registry=registry,
    )


@pytest.mark.parametrize("retry_skill", [None, "snapshot"])
def test_followup_rejection_leaves_ledger_and_execution_untouched(
    tmp_path, retry_skill
):
    async def scenario():
        database = tmp_path / "followup.sqlite"
        repository = SQLiteConversationRepository(database)
        registry, _ = _boundary_skill(tmp_path / "skills", 56001)
        selector = _Selector()
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(checkpointer, repository, registry, selector)
            await runtime.run("Initial research.", thread_id="chat", turn_id="first")
            before = await repository.get_conversation("chat")
            turns = await repository.list_turns("chat")
            count = len(selector.contexts)
            blocked = create_default_agent_runtime(
                Settings(
                    api_key="offline-test",
                    structured_output_mode="native",
                    _env_file=None,
                ),
                tmp_path / "workspace",
                checkpointer=checkpointer,
                conversation_repository=repository,
                skills_registry=registry,
                model_factory=lambda _: _UnusedModel(),
            )
            with (
                patch.object(
                    SQLiteConversationRepository, "reserve_follow_up"
                ) as reserve,
                patch.object(blocked.graph, "ainvoke") as execute,
                pytest.raises(SkillsError, match="serialized skill snapshot"),
            ):
                await blocked.continue_thread(
                    "chat", "Follow up.", turn_id="retry", skill_id="snapshot"
                )
            reserve.assert_not_called()
            execute.assert_not_called()
            assert len(selector.contexts) == count
            assert await repository.get_conversation("chat") == before
            assert await repository.list_turns("chat") == turns
            if retry_skill:
                _write_skill(registry.root, "Valid replacement.")
                registry.discover()
            result = await runtime.continue_thread(
                "chat", "Follow up.", turn_id="retry", skill_id=retry_skill
            )
            assert (result["active_skill"] is not None) == bool(retry_skill)
            assert (await repository.get_turn("chat", "retry")).status == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "boundary,expected_json_chars",
    [("encoded", 56000), ("emoji", 8354)],
    ids=["encoded-56000", "utf8-32768"],
)
def test_boundary_checkpoint_survives_interruption_close_and_reopen(
    tmp_path, boundary, expected_json_chars
):
    async def scenario():
        database = tmp_path / "checkpoint.sqlite"
        if boundary == "encoded":
            registry, expected = _boundary_skill(tmp_path / "skills")
            assert registry.activate("snapshot") == expected
        else:
            body = "😀" * 8192
            assert len(body.encode("utf-8")) == 32768
            registry = _write_skill(tmp_path / "skills", body)
            expected = registry.activate("snapshot")
            assert expected.instructions == body
            assert (
                expected.fingerprint
                == hashlib.sha256(
                    (registry.root / "snapshot" / "SKILL.md").read_bytes()
                ).hexdigest()
            )
        assert len(_encoded(expected)) == expected_json_chars
        repository = SQLiteConversationRepository(database)
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(
                checkpointer, repository, registry, _Selector(interrupt=True)
            )
            with pytest.raises(RuntimeError, match="controlled interruption"):
                await runtime.run(
                    "Research fidelity.",
                    thread_id="chat",
                    turn_id="first",
                    skill_id="snapshot",
                )
            turn = await repository.get_turn("chat", "first")
            assert turn.status == "interrupted"
            assert json.loads(turn.run_context_json)[
                "active_skill"
            ] == expected.model_dump(mode="json")
            snapshot = await runtime.load_conversation("chat")
            assert (
                SkillContext.model_validate(snapshot.turns[0].state["active_skill"])
                == expected
            )
        # Neither reactivation nor replanning is allowed on checkpoint recovery.
        (registry.root / "snapshot" / "SKILL.md").unlink()
        selector = _Selector()
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(
                checkpointer,
                SQLiteConversationRepository(database),
                SkillsRegistry(registry.root),
                selector,
                planner=lambda _: pytest.fail("Resume replanned"),
            )
            result = await runtime.resume(thread_id="chat")
            assert result["active_skill"] == expected
            assert len(_encoded(result["active_skill"])) == expected_json_chars
            assert [c.step.step_number for c in selector.contexts] == [2, 3]
            assert all(
                c.active_skill.fingerprint == expected.fingerprint
                for c in selector.contexts
            )
            persisted = await repository.get_turn("chat", "first")
            assert persisted.status == "completed"
            assert persisted.run_context_json == turn.run_context_json
            assert (
                SkillContext.model_validate(
                    json.loads(persisted.run_context_json)["active_skill"]
                )
                == expected
            )
            snapshot = await runtime.load_conversation("chat")
            assert (
                SkillContext.model_validate(snapshot.turns[0].state["active_skill"])
                == expected
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_reopened_schema_roundtrips_boundary_and_keeps_60000_check(tmp_path, legacy):
    async def scenario():
        database = tmp_path / "schema.sqlite"
        repository = SQLiteConversationRepository(database)
        prior = await repository.create_conversation_with_turn(
            thread_id="prior",
            turn_id="first",
            user_message="Preserve this row.",
            session_tool_call_limit=100,
            reserved_tool_calls=20,
        )
        with sqlite3.connect(database) as connection:
            if legacy:
                connection.execute("ALTER TABLE turns DROP COLUMN run_context_json")
                assert "run_context_json" not in {
                    row[1] for row in connection.execute("PRAGMA table_info(turns)")
                }
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='turns'"
            ).fetchone()[0]
        reopened = SQLiteConversationRepository(database)
        assert await reopened.get_turn("prior", "first") == prior
        registry, expected = _boundary_skill(tmp_path / "skills")
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(checkpointer, reopened, registry, _Selector())
            await runtime.run(
                "Roundtrip boundary.",
                thread_id="boundary",
                turn_id="first",
                skill_id="snapshot",
            )
        turn = await SQLiteConversationRepository(database).get_turn(
            "boundary", "first"
        )
        assert (
            SkillContext.model_validate(
                json.loads(turn.run_context_json)["active_skill"]
            )
            == expected
        )
        with sqlite3.connect(database) as connection:
            migrated = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='turns'"
            ).fetchone()[0]
            assert "CHECK(length(run_context_json) <= 60000)" in migrated
            if not legacy:
                assert migrated == schema
            # Exercise SQLite independently of repository/model validation.
            connection.execute(
                "UPDATE turns SET run_context_json=? WHERE thread_id='prior'",
                ('"' + "x" * 59998 + '"',),
            )
            connection.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(
                    "UPDATE turns SET run_context_json=? WHERE thread_id='prior'",
                    ('"' + "x" * 59999 + '"',),
                )
            connection.rollback()
        assert await reopened.get_turn("prior", "first") == prior
        assert type(turn).model_validate(
            {**turn.model_dump(), "run_context_json": "x" * 60000}
        )
        with pytest.raises(ValueError):
            type(turn).model_validate(
                {**turn.model_dump(), "run_context_json": "x" * 60001}
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("checkpoint_exists", [False, True])
def test_historical_over_activation_limit_recovers_from_precheckpoint_ledger(
    tmp_path, checkpoint_exists
):
    async def scenario():
        database = tmp_path / "historical.sqlite"
        registry, historical = _boundary_skill(tmp_path / "skills", 57000)
        with pytest.raises(SkillsError, match="serialized skill snapshot"):
            registry.activate("snapshot")
        repository = SQLiteConversationRepository(database)
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(
                checkpointer, repository, registry, _Selector(interrupt=True)
            )
            # Seed a previously legal durable reservation, before any checkpoint.
            turn = await repository.create_conversation_with_turn(
                thread_id="historical",
                turn_id="first",
                user_message="Recover history.",
                session_tool_call_limit=100,
                reserved_tool_calls=20,
                run_context_json=runtime._run_context_json(historical),
            )
            assert 56000 < len(turn.run_context_json) < 60000
            assert (await runtime.load_conversation("historical")).turns[
                0
            ].state is None
            if checkpoint_exists:
                with pytest.raises(RuntimeError, match="controlled interruption"):
                    await runtime.resume(thread_id="historical")
                interrupted = await repository.get_turn("historical", "first")
                assert interrupted.status == "interrupted"
                assert interrupted.run_context_json == turn.run_context_json
                snapshot = await runtime.load_conversation("historical")
                assert (
                    SkillContext.model_validate(snapshot.turns[0].state["active_skill"])
                    == historical
                )
        selector = _Selector()
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = _runtime(
                checkpointer, SQLiteConversationRepository(database), registry, selector
            )
            with patch.object(
                registry,
                "activate",
                side_effect=AssertionError("Historical activation"),
            ):
                result = await runtime.resume(thread_id="historical")
            assert result["active_skill"] == historical
            assert [c.step.step_number for c in selector.contexts] == (
                [2, 3] if checkpoint_exists else [1, 2, 3]
            )
            assert all(
                c.active_skill.fingerprint == historical.fingerprint
                for c in selector.contexts
            )
            persisted = await repository.get_turn("historical", "first")
            assert persisted.status == "completed"
            assert persisted.run_context_json == turn.run_context_json
            assert (
                SkillContext.model_validate(
                    json.loads(persisted.run_context_json)["active_skill"]
                )
                == historical
            )
            snapshot = await runtime.load_conversation("historical")
            assert (
                SkillContext.model_validate(snapshot.turns[0].state["active_skill"])
                == historical
            )

    asyncio.run(scenario())
