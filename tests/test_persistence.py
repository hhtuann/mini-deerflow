import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from pydantic import BaseModel

from mini_deerflow.actions import (
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.evidence import EvidenceProvenance, EvidenceRecord, StepFinding
from mini_deerflow.persistence import (
    _ALLOWED_CHECKPOINT_TYPES,
    CheckpointPathError,
    CheckpointStorageError,
    InvalidThreadIdError,
    PersistenceError,
    ThreadAlreadyExistsError,
    _create_checkpoint_serializer,
    create_thread_config,
    list_thread_ids,
    normalize_thread_id,
    open_sqlite_checkpointer,
    resolve_checkpoint_path,
)
from mini_deerflow.review import ReplanRecord, ReviewFinding, ReviewVerdict
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools.contracts import ToolResult


class UnregisteredCheckpointValue(BaseModel):
    value: str


class FakeCheckpointLister:
    def __init__(self, thread_ids: tuple[str, ...]) -> None:
        self.thread_ids = thread_ids
        self.alist_call_count = 0
        self.received_config: RunnableConfig | None = None

    async def alist(
        self,
        config: RunnableConfig | None,
    ) -> AsyncIterator[CheckpointTuple]:
        self.alist_call_count += 1
        self.received_config = config

        for thread_id in self.thread_ids:
            checkpoint_config: RunnableConfig = {
                "configurable": {
                    "thread_id": thread_id,
                },
            }
            yield cast(
                CheckpointTuple,
                SimpleNamespace(config=checkpoint_config),
            )


class FailingCheckpointLister:
    def __init__(self, error: sqlite3.Error) -> None:
        self.error = error

    async def alist(
        self,
        config: RunnableConfig | None,
    ) -> AsyncIterator[CheckpointTuple]:
        del config
        yield cast(
            CheckpointTuple,
            SimpleNamespace(
                config={"configurable": {"thread_id": "partial-thread"}},
            ),
        )
        raise self.error


def test_normalize_thread_id_strips_valid_identifier() -> None:
    assert normalize_thread_id("  Day-08.thread_1  ") == "Day-08.thread_1"


def test_thread_already_exists_error_belongs_to_persistence_hierarchy() -> None:
    assert issubclass(ThreadAlreadyExistsError, PersistenceError)
    assert issubclass(ThreadAlreadyExistsError, ValueError)


def test_checkpoint_storage_error_belongs_to_persistence_hierarchy() -> None:
    assert issubclass(CheckpointStorageError, PersistenceError)
    assert issubclass(CheckpointStorageError, ValueError)


def test_list_thread_ids_deduplicates_and_sorts_public_checkpoints() -> None:
    checkpointer = FakeCheckpointLister(
        (
            "research-002",
            "research-001",
            "research-002",
        )
    )

    thread_ids = asyncio.run(
        list_thread_ids(cast(BaseCheckpointSaver[str], checkpointer))
    )

    assert thread_ids == (
        "research-001",
        "research-002",
    )
    assert checkpointer.alist_call_count == 1
    assert checkpointer.received_config is None


def test_list_thread_ids_returns_empty_tuple_without_checkpoints() -> None:
    checkpointer = FakeCheckpointLister(())

    thread_ids = asyncio.run(
        list_thread_ids(cast(BaseCheckpointSaver[str], checkpointer))
    )

    assert thread_ids == ()
    assert checkpointer.alist_call_count == 1
    assert checkpointer.received_config is None


def test_list_thread_ids_normalizes_sqlite_error_without_partial_result() -> None:
    original_error = sqlite3.OperationalError("deterministic list failure")
    checkpointer = FailingCheckpointLister(original_error)

    with pytest.raises(
        CheckpointStorageError,
        match="list checkpoint threads",
    ) as raised_error:
        asyncio.run(list_thread_ids(cast(BaseCheckpointSaver[str], checkpointer)))

    assert raised_error.value.__cause__ is original_error


@pytest.mark.parametrize(
    "thread_id",
    [
        "",
        "   ",
        "-starts-with-symbol",
        "contains space",
        "../another-thread",
        "thread/slash",
        "a" * 129,
    ],
)
def test_normalize_thread_id_rejects_invalid_identifier(
    thread_id: str,
) -> None:
    with pytest.raises(
        InvalidThreadIdError,
        match="thread_id",
    ):
        normalize_thread_id(thread_id)


def test_create_thread_config_returns_langgraph_shape() -> None:
    config = create_thread_config(
        "day-08-demo",
        recursion_limit=60,
    )

    assert config == {
        "configurable": {
            "thread_id": "day-08-demo",
        },
        "recursion_limit": 60,
    }


@pytest.mark.parametrize(
    "recursion_limit",
    [
        True,
        1.5,
        "60",
        None,
    ],
)
def test_create_thread_config_rejects_non_integer_limit(
    recursion_limit: object,
) -> None:
    with pytest.raises(
        TypeError,
        match="recursion_limit",
    ):
        create_thread_config(
            "day-08-demo",
            recursion_limit=recursion_limit,
        )


@pytest.mark.parametrize(
    "recursion_limit",
    [
        0,
        -1,
    ],
)
def test_create_thread_config_rejects_non_positive_limit(
    recursion_limit: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="greater than zero",
    ):
        create_thread_config(
            "day-08-demo",
            recursion_limit=recursion_limit,
        )


def test_resolve_checkpoint_path_creates_parent_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "checkpoints.sqlite"

    resolved_path = resolve_checkpoint_path(database_path)

    assert resolved_path.is_absolute()
    assert resolved_path.parent.is_dir()
    assert not resolved_path.exists()


def test_resolve_checkpoint_path_rejects_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoints.sqlite"
    database_path.mkdir()

    with pytest.raises(
        CheckpointPathError,
        match="must reference a file",
    ):
        resolve_checkpoint_path(database_path)


def test_resolve_checkpoint_path_normalizes_parent_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "checkpoints.sqlite"
    original_error = OSError("deterministic parent creation failure")

    def fail_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok
        raise original_error

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(
        CheckpointPathError,
        match="prepare checkpoint database path",
    ) as raised_error:
        resolve_checkpoint_path(database_path)

    assert raised_error.value.__cause__ is original_error


def test_checkpoint_serializer_round_trips_allowed_domain_types(
    caplog: pytest.LogCaptureFixture,
) -> None:
    serializer = _create_checkpoint_serializer()
    plan = Plan(
        goal="Research checkpoint serializer behavior.",
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective="Collect evidence for the serializer behavior.",
                success_criteria="The domain value survives a round trip.",
            )
            for number in range(1, 4)
        ],
    )
    tool_call = ToolCallAction(
        type="tool_call",
        tool_name="search",
        arguments={"query": "checkpoint serialization"},
    )
    completed_step = CompleteStepAction(
        type="complete_step",
        summary="Recorded enough evidence to complete this research step.",
        sources=["https://example.com/evidence"],
    )
    observation = ToolObservation(
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        action=tool_call,
        result=ToolResult.ok(
            {"summary": "The serializer preserved the tool result."},
        ),
    )
    provenance = EvidenceProvenance(
        tool_name="web_search",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    evidence = EvidenceRecord(
        url="https://example.com/evidence#fragment",
        source_tool="web_search",
        title="Checkpoint evidence",
        excerpt="Evidence survives serialization.",
        provenance=provenance,
    )
    finding = StepFinding(
        step_number=1,
        summary="Recorded a checkpoint-backed evidence finding.",
        citations=["https://example.com/evidence"],
    )
    verdict = ReviewVerdict(
        verdict="replan",
        rationale="Remaining steps cannot close the recorded evidence gap.",
        findings=[
            ReviewFinding(
                category="gap",
                description="One goal dimension lacks any collected evidence.",
                related_step_numbers=[2],
            ),
        ],
    )
    replan_record = ReplanRecord(
        replan_number=1,
        replaced_step_numbers=[2, 3],
        replacement_steps=[
            PlanStep(
                step_number=2,
                title="Revised remaining step",
                objective="Collect the missing evidence dimension.",
                success_criteria="The reviewed gap is closed.",
            )
        ],
        review_rationale="Remaining steps cannot close the recorded evidence gap.",
    )

    assert serializer._allowed_msgpack_modules is not True
    assert serializer._allowed_msgpack_modules == {
        (domain_type.__module__, domain_type.__name__)
        for domain_type in _ALLOWED_CHECKPOINT_TYPES
    }

    for expected in (
        plan,
        observation,
        completed_step,
        tool_call,
        provenance,
        evidence,
        finding,
        verdict,
        replan_record,
    ):
        restored = serializer.loads_typed(serializer.dumps_typed(expected))

        assert type(restored) is type(expected)
        assert restored == expected

    assert not any(
        "Deserializing unregistered type" in record.getMessage()
        for record in caplog.records
    )


def test_checkpoint_serializer_rejects_unregistered_custom_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    serializer = _create_checkpoint_serializer()
    value = UnregisteredCheckpointValue(value="must remain untrusted")

    restored = serializer.loads_typed(serializer.dumps_typed(value))

    assert restored == {"value": "must remain untrusted"}
    assert not isinstance(restored, UnregisteredCheckpointValue)
    assert any(
        "Blocked deserialization" in record.getMessage()
        and "UnregisteredCheckpointValue" in record.getMessage()
        for record in caplog.records
    )


def test_open_sqlite_checkpointer_initializes_and_closes_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoints.sqlite"

    async def run_scenario() -> set[str]:
        async with open_sqlite_checkpointer(database_path) as checkpointer:
            config = create_thread_config(
                "day-08-demo",
                recursion_limit=60,
            )

            assert await checkpointer.aget_tuple(config) is None

            async with checkpointer.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ) as cursor:
                rows = await cursor.fetchall()

        with pytest.raises(
            ValueError,
            match="no active connection",
        ):
            await checkpointer.conn.execute("SELECT 1")

        return {row[0] for row in rows}

    table_names = asyncio.run(run_scenario())

    assert database_path.is_file()
    assert {"checkpoints", "writes"} <= table_names


def test_open_sqlite_checkpointer_normalizes_connection_error_without_yielding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoints.sqlite"
    original_error = sqlite3.OperationalError("deterministic connection failure")
    saver_was_yielded = False

    def fail_connect(database: str) -> None:
        del database
        raise original_error

    monkeypatch.setattr(
        "mini_deerflow.persistence.aiosqlite.connect",
        fail_connect,
    )

    async def run_scenario() -> CheckpointStorageError:
        nonlocal saver_was_yielded

        with pytest.raises(
            CheckpointStorageError,
            match="open checkpoint database",
        ) as raised_error:
            async with open_sqlite_checkpointer(database_path):
                saver_was_yielded = True

        return raised_error.value

    actual_error = asyncio.run(run_scenario())

    assert saver_was_yielded is False
    assert actual_error.__cause__ is original_error


def test_open_sqlite_checkpointer_normalizes_connection_close_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoints.sqlite"
    original_error = sqlite3.OperationalError("deterministic close failure")
    saver_was_yielded = False

    class FailingCloseConnection:
        async def close(self) -> None:
            raise original_error

    async def connect(database: str) -> FailingCloseConnection:
        del database
        return FailingCloseConnection()

    monkeypatch.setattr(
        "mini_deerflow.persistence.aiosqlite.connect",
        connect,
    )

    async def run_scenario() -> CheckpointStorageError:
        nonlocal saver_was_yielded

        with pytest.raises(
            CheckpointStorageError,
            match="close checkpoint database",
        ) as raised_error:
            async with open_sqlite_checkpointer(database_path):
                saver_was_yielded = True

        return raised_error.value

    actual_error = asyncio.run(run_scenario())

    assert saver_was_yielded is True
    assert actual_error.__cause__ is original_error
