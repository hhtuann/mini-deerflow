import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mini_deerflow.actions import (
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.evidence import EvidenceProvenance, EvidenceRecord, StepFinding
from mini_deerflow.schemas import Plan

_THREAD_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
)
_ALLOWED_CHECKPOINT_TYPES = (
    Plan,
    ToolObservation,
    CompleteStepAction,
    ToolCallAction,
    EvidenceProvenance,
    EvidenceRecord,
    StepFinding,
)


def _create_checkpoint_serializer() -> JsonPlusSerializer:
    """Create the serializer shared by all persistent checkpoint operations."""

    return JsonPlusSerializer(
        allowed_msgpack_modules=_ALLOWED_CHECKPOINT_TYPES,
    )


class PersistenceError(ValueError):
    """Base error raised for invalid persistence configuration."""


class InvalidThreadIdError(PersistenceError):
    """Raised when a thread identifier violates the public contract."""


class CheckpointPathError(PersistenceError):
    """Raised when the checkpoint database path is not usable."""


class CheckpointStorageError(PersistenceError):
    """Raised when the checkpoint database cannot complete an operation."""


class CheckpointUnavailableError(PersistenceError):
    """Raised when an operation requires a checkpointer."""


class ThreadNotFoundError(PersistenceError):
    """Raised when a thread has no persisted checkpoint."""


class ThreadAlreadyExistsError(PersistenceError):
    """Raised when a new run targets an existing thread."""


class _NormalizingAsyncSqliteSaver(AsyncSqliteSaver):
    """Translate SQLite failures at the saver boundary into domain errors."""

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> CheckpointTuple | None:
        try:
            return await super().aget_tuple(config)
        except sqlite3.Error as error:
            raise CheckpointStorageError(
                "could not read checkpoint data",
            ) from error

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        try:
            async for checkpoint in super().alist(
                config,
                filter=filter,
                before=before,
                limit=limit,
            ):
                yield checkpoint
        except sqlite3.Error as error:
            raise CheckpointStorageError(
                "could not list checkpoint data",
            ) from error

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        try:
            return await super().aput(
                config,
                checkpoint,
                metadata,
                new_versions,
            )
        except sqlite3.Error as error:
            raise CheckpointStorageError(
                "could not write checkpoint data",
            ) from error

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        try:
            await super().aput_writes(
                config,
                writes,
                task_id,
                task_path,
            )
        except sqlite3.Error as error:
            raise CheckpointStorageError(
                "could not write checkpoint data",
            ) from error


def normalize_thread_id(thread_id: str) -> str:
    """Return a validated thread identifier."""

    normalized_thread_id = thread_id.strip()

    if not _THREAD_ID_PATTERN.fullmatch(normalized_thread_id):
        raise InvalidThreadIdError(
            "thread_id must start with a letter or number, be at most "
            "128 characters, and contain only letters, numbers, dots, "
            "underscores, or hyphens"
        )

    return normalized_thread_id


def create_thread_config(
    thread_id: str,
    *,
    recursion_limit: int,
) -> RunnableConfig:
    """Create the LangGraph configuration for one persistent thread."""

    if isinstance(recursion_limit, bool) or not isinstance(
        recursion_limit,
        int,
    ):
        raise TypeError("recursion_limit must be an integer")

    if recursion_limit <= 0:
        raise ValueError("recursion_limit must be greater than zero")

    return {
        "configurable": {
            "thread_id": normalize_thread_id(thread_id),
        },
        "recursion_limit": recursion_limit,
    }


def resolve_checkpoint_path(
    database_path: str | Path,
) -> Path:
    """Resolve the local SQLite path and create its parent directory."""

    try:
        resolved_path = (
            Path(database_path)
            .expanduser()
            .resolve(
                strict=False,
            )
        )

        if resolved_path.exists() and not resolved_path.is_file():
            raise CheckpointPathError(
                "checkpoint database path must reference a file",
            )

        resolved_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError as error:
        raise CheckpointPathError(
            "could not prepare checkpoint database path",
        ) from error

    return resolved_path


@asynccontextmanager
async def open_sqlite_checkpointer(
    database_path: str | Path,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Open a durable SQLite checkpointer and close it on exit."""

    resolved_path = resolve_checkpoint_path(database_path)

    try:
        connection = await aiosqlite.connect(str(resolved_path))
    except (OSError, sqlite3.Error) as error:
        raise CheckpointStorageError(
            "could not open checkpoint database",
        ) from error

    try:
        yield _NormalizingAsyncSqliteSaver(
            connection,
            serde=_create_checkpoint_serializer(),
        )
    finally:
        try:
            await connection.close()
        except (OSError, sqlite3.Error) as error:
            raise CheckpointStorageError(
                "could not close checkpoint database",
            ) from error


async def list_thread_ids(
    checkpointer: BaseCheckpointSaver[str],
) -> tuple[str, ...]:
    """List unique persisted thread identifiers in deterministic order."""

    thread_ids: set[str] = set()

    try:
        async for checkpoint in checkpointer.alist(None):
            configurable = checkpoint.config.get("configurable")

            if not isinstance(configurable, dict):
                continue

            thread_id = configurable.get("thread_id")

            if isinstance(thread_id, str):
                thread_ids.add(thread_id)
    except sqlite3.Error as error:
        raise CheckpointStorageError(
            "could not list checkpoint threads",
        ) from error

    return tuple(sorted(thread_ids))


@runtime_checkable
class AsyncCheckpointReader(Protocol):
    """Minimal async checkpoint lookup required by AgentRuntime."""

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> object | None:
        """Return the latest checkpoint for a thread, if one exists."""
