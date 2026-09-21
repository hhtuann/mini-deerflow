"""Durable conversation and turn ledger for true multi-turn execution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mini_deerflow.evidence import is_research_report

ConversationStatus = Literal["running", "completed", "interrupted", "failed"]
TurnStatus = Literal["running", "completed", "interrupted", "failed"]
MessageRole = Literal["user", "assistant"]

SafeIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class ConversationRepositoryError(RuntimeError):
    """Base error for durable conversation operations."""


class ConversationNotFoundError(ConversationRepositoryError):
    """Raised when a conversation does not exist."""


class TurnNotFoundError(ConversationRepositoryError):
    """Raised when a turn does not exist."""


class ConversationAlreadyExistsError(ConversationRepositoryError):
    """Raised when a first turn targets an existing conversation."""


class ActiveTurnError(ConversationRepositoryError):
    """Raised when a conversation already has active work."""


class SessionBudgetExceededError(ConversationRepositoryError):
    """Raised when another full turn cannot fit the session budget."""


class InvalidConversationTransitionError(ConversationRepositoryError):
    """Raised for an invalid durable turn-state transition."""


class IdempotencyConflictError(ConversationRepositoryError):
    """Raised when one turn identifier is reused for different input."""


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: SafeIdentifier
    turn_id: SafeIdentifier
    role: MessageRole
    content: str = Field(min_length=1, max_length=20_000)


class ConversationContext(BaseModel):
    """Bounded prior messages projected into exactly one new turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ConversationMessage, ...] = ()
    omitted_turns: int = Field(default=0, ge=0)
    estimated_characters: int = Field(default=0, ge=0)


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: SafeIdentifier
    title: str = Field(min_length=1, max_length=80)
    status: ConversationStatus
    created_at: str
    updated_at: str
    session_tool_calls: int = Field(ge=0)
    session_tool_call_limit: int = Field(ge=1)


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: SafeIdentifier
    turn_id: SafeIdentifier
    sequence: int = Field(ge=1)
    user_message: str = Field(min_length=1, max_length=20_000)
    assistant_response: str | None = Field(default=None, max_length=200_000)
    status: TurnStatus
    checkpoint_namespace: SafeIdentifier
    created_at: str
    completed_at: str | None = None
    total_tool_calls: int = Field(default=0, ge=0)
    reserved_tool_calls: int = Field(default=0, ge=0)
    trace_events: tuple[str, ...] = ()
    safe_error: str | None = Field(default=None, max_length=240)


def sanitize_conversation_title(message: str, *, limit: int = 64) -> str:
    """Create a deterministic plain-text title from the first user message."""

    if not isinstance(message, str):
        raise TypeError("message must be text")
    text = _WHITESPACE.sub(" ", _CONTROL_CHARACTERS.sub(" ", message)).strip()
    if not text:
        return "New conversation"
    bounded = text[:limit].rstrip()
    return bounded if len(text) <= limit else bounded.rstrip(" .") + "…"


class SQLiteConversationRepository:
    """SQLite-backed source of truth for conversations and ordered turns."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    async def create_conversation_with_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        user_message: str,
        session_tool_call_limit: int,
        reserved_tool_calls: int,
    ) -> TurnRecord:
        thread_id = _identifier(thread_id, "thread_id")
        turn_id = _identifier(turn_id, "turn_id")
        user_message = _message(user_message)
        _positive(session_tool_call_limit, "session_tool_call_limit")
        _non_negative(reserved_tool_calls, "reserved_tool_calls")
        if reserved_tool_calls > session_tool_call_limit:
            raise SessionBudgetExceededError("turn reservation exceeds session budget")
        now = _now()
        namespace = turn_id
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            existing = await _fetchone(
                connection,
                "SELECT thread_id FROM conversations WHERE thread_id = ?",
                (thread_id,),
            )
            if existing is not None:
                await connection.rollback()
                raise ConversationAlreadyExistsError(
                    f"conversation {thread_id!r} already exists"
                )
            await connection.execute(
                """
                INSERT INTO conversations(
                    thread_id, title, status, created_at, updated_at,
                    session_tool_calls, session_tool_call_limit
                ) VALUES (?, ?, 'running', ?, ?, 0, ?)
                """,
                (
                    thread_id,
                    sanitize_conversation_title(user_message),
                    now,
                    now,
                    session_tool_call_limit,
                ),
            )
            await connection.execute(
                """
                INSERT INTO turns(
                    thread_id, turn_id, sequence, user_message, status,
                    checkpoint_namespace, created_at, reserved_tool_calls
                ) VALUES (?, ?, 1, ?, 'running', ?, ?, ?)
                """,
                (
                    thread_id,
                    turn_id,
                    user_message,
                    namespace,
                    now,
                    reserved_tool_calls,
                ),
            )
            await connection.commit()
        return await self.get_turn(thread_id, turn_id)

    async def reserve_follow_up(
        self,
        *,
        thread_id: str,
        turn_id: str,
        user_message: str,
        reserved_tool_calls: int,
    ) -> tuple[TurnRecord, bool]:
        thread_id = _identifier(thread_id, "thread_id")
        turn_id = _identifier(turn_id, "turn_id")
        user_message = _message(user_message)
        _non_negative(reserved_tool_calls, "reserved_tool_calls")
        now = _now()
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            duplicate = await _fetchone(
                connection,
                "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            )
            if duplicate is not None:
                await connection.rollback()
                if duplicate["user_message"] != user_message:
                    raise IdempotencyConflictError(
                        "turn_id is already bound to a different user message"
                    )
                return _turn_from_row(duplicate), False
            conversation = await _fetchone(
                connection,
                "SELECT * FROM conversations WHERE thread_id = ?",
                (thread_id,),
            )
            if conversation is None:
                await connection.rollback()
                raise ConversationNotFoundError(
                    f"conversation {thread_id!r} does not exist"
                )
            active = await _fetchone(
                connection,
                """
                SELECT turn_id FROM turns
                WHERE thread_id = ? AND status IN ('running', 'interrupted')
                LIMIT 1
                """,
                (thread_id,),
            )
            if active is not None:
                await connection.rollback()
                raise ActiveTurnError("conversation already has an active turn")
            remaining = int(conversation["session_tool_call_limit"]) - int(
                conversation["session_tool_calls"]
            )
            if reserved_tool_calls > remaining:
                await connection.rollback()
                raise SessionBudgetExceededError(
                    "conversation tool-call budget cannot admit another turn"
                )
            sequence_row = await _fetchone(
                connection,
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM turns WHERE thread_id = ?",
                (thread_id,),
            )
            assert sequence_row is not None
            sequence = int(sequence_row["next_sequence"])
            namespace = turn_id
            await connection.execute(
                """
                INSERT INTO turns(
                    thread_id, turn_id, sequence, user_message, status,
                    checkpoint_namespace, created_at, reserved_tool_calls
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    thread_id,
                    turn_id,
                    sequence,
                    user_message,
                    namespace,
                    now,
                    reserved_tool_calls,
                ),
            )
            await connection.execute(
                "UPDATE conversations SET status = 'running', updated_at = ? "
                "WHERE thread_id = ?",
                (now, thread_id),
            )
            await connection.commit()
        return await self.get_turn(thread_id, turn_id), True

    async def get_conversation(self, thread_id: str) -> ConversationRecord:
        thread_id = _identifier(thread_id, "thread_id")
        async with self._connection() as connection:
            row = await _fetchone(
                connection,
                "SELECT * FROM conversations WHERE thread_id = ?",
                (thread_id,),
            )
        if row is None:
            raise ConversationNotFoundError(f"conversation {thread_id!r} not found")
        return _conversation_from_row(row)

    async def list_conversations(self) -> tuple[ConversationRecord, ...]:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, thread_id"
            )
            rows = await cursor.fetchall()
        return tuple(_conversation_from_row(row) for row in rows)

    async def list_turns(self, thread_id: str) -> tuple[TurnRecord, ...]:
        thread_id = _identifier(thread_id, "thread_id")
        await self.get_conversation(thread_id)
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM turns WHERE thread_id = ? ORDER BY sequence",
                (thread_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    async def get_turn(self, thread_id: str, turn_id: str) -> TurnRecord:
        thread_id = _identifier(thread_id, "thread_id")
        turn_id = _identifier(turn_id, "turn_id")
        async with self._connection() as connection:
            row = await _fetchone(
                connection,
                "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            )
        if row is None:
            raise TurnNotFoundError(f"turn {turn_id!r} not found")
        return _turn_from_row(row)

    async def get_active_turn(self, thread_id: str) -> TurnRecord | None:
        thread_id = _identifier(thread_id, "thread_id")
        async with self._connection() as connection:
            row = await _fetchone(
                connection,
                """
                SELECT * FROM turns WHERE thread_id = ?
                AND status IN ('running', 'interrupted')
                ORDER BY sequence DESC LIMIT 1
                """,
                (thread_id,),
            )
        return None if row is None else _turn_from_row(row)

    async def complete_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        assistant_response: str,
        total_tool_calls: int,
    ) -> TurnRecord:
        return await self._finish_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            assistant_response=assistant_response,
            safe_error=None,
            total_tool_calls=total_tool_calls,
        )

    async def mark_turn_interrupted(
        self,
        *,
        thread_id: str,
        turn_id: str,
        total_tool_calls: int = 0,
    ) -> TurnRecord:
        return await self._set_nonterminal_or_failed(
            thread_id,
            turn_id,
            status="interrupted",
            safe_error=None,
            total_tool_calls=total_tool_calls,
        )

    async def mark_turn_failed(
        self,
        *,
        thread_id: str,
        turn_id: str,
        safe_error: str = "Turn execution failed.",
        total_tool_calls: int = 0,
    ) -> TurnRecord:
        return await self._set_nonterminal_or_failed(
            thread_id,
            turn_id,
            status="failed",
            safe_error=_safe_error(safe_error),
            total_tool_calls=total_tool_calls,
        )

    async def load_context(
        self,
        thread_id: str,
        *,
        before_sequence: int | None = None,
        max_characters: int = 12_000,
        max_message_characters: int = 4_000,
        retained_turns: int = 12,
    ) -> ConversationContext:
        turns = [
            turn
            for turn in await self.list_turns(thread_id)
            if turn.status == "completed"
            and turn.assistant_response
            and (before_sequence is None or turn.sequence < before_sequence)
        ]
        omitted = max(0, len(turns) - retained_turns)
        turns = turns[-retained_turns:]
        projected: list[ConversationMessage] = []
        for turn in turns:
            projected.extend(
                (
                    ConversationMessage(
                        message_id=f"turn-{turn.sequence}-user",
                        turn_id=turn.turn_id,
                        role="user",
                        content=_bounded(turn.user_message, max_message_characters),
                    ),
                    ConversationMessage(
                        message_id=f"turn-{turn.sequence}-assistant",
                        turn_id=turn.turn_id,
                        role="assistant",
                        content=_bounded(
                            (
                                "A prior research turn completed. Its internal "
                                "execution report is omitted from conversational "
                                "context."
                                if is_research_report(turn.assistant_response)
                                else turn.assistant_response or ""
                            ),
                            max_message_characters,
                        ),
                    ),
                )
            )
        while (
            projected and sum(len(item.content) for item in projected) > max_characters
        ):
            removed_turn_id = projected[0].turn_id
            projected = [item for item in projected if item.turn_id != removed_turn_id]
            omitted += 1
        return ConversationContext(
            messages=tuple(projected),
            omitted_turns=omitted,
            estimated_characters=sum(len(item.content) for item in projected),
        )

    async def store_trace_events(
        self,
        thread_id: str,
        turn_id: str,
        trace_events: tuple[str, ...],
    ) -> None:
        # Validate every item is a JSON object before it reaches durable history.
        for event in trace_events:
            parsed = json.loads(event)
            if not isinstance(parsed, dict):
                raise TypeError("trace event must encode a JSON object")
        async with self._connection() as connection:
            cursor = await connection.execute(
                "UPDATE turns SET trace_json = ? WHERE thread_id = ? AND turn_id = ?",
                (json.dumps(trace_events), thread_id, turn_id),
            )
            if cursor.rowcount != 1:
                raise TurnNotFoundError(f"turn {turn_id!r} not found")
            await connection.commit()

    async def _finish_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: Literal["completed"],
        assistant_response: str,
        safe_error: str | None,
        total_tool_calls: int,
    ) -> TurnRecord:
        _non_negative(total_tool_calls, "total_tool_calls")
        now = _now()
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await _fetchone(
                connection,
                "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            )
            if row is None:
                await connection.rollback()
                raise TurnNotFoundError(f"turn {turn_id!r} not found")
            if row["status"] == "completed":
                await connection.rollback()
                return _turn_from_row(row)
            if row["status"] not in ("running", "interrupted"):
                await connection.rollback()
                raise InvalidConversationTransitionError(
                    f"cannot complete a {row['status']} turn"
                )
            conversation = await _fetchone(
                connection,
                "SELECT * FROM conversations WHERE thread_id = ?",
                (thread_id,),
            )
            assert conversation is not None
            try:
                _validate_terminal_charge(row, conversation, total_tool_calls)
            except ConversationRepositoryError:
                await connection.rollback()
                raise
            await connection.execute(
                """
                UPDATE turns SET status = ?, assistant_response = ?,
                    completed_at = ?, total_tool_calls = ?, safe_error = ?
                WHERE thread_id = ? AND turn_id = ?
                """,
                (
                    status,
                    assistant_response,
                    now,
                    total_tool_calls,
                    safe_error,
                    thread_id,
                    turn_id,
                ),
            )
            await connection.execute(
                """
                UPDATE conversations SET status = ?, updated_at = ?,
                    session_tool_calls = session_tool_calls + ?
                WHERE thread_id = ?
                """,
                (status, now, total_tool_calls, thread_id),
            )
            await connection.commit()
        return await self.get_turn(thread_id, turn_id)

    async def _set_nonterminal_or_failed(
        self,
        thread_id: str,
        turn_id: str,
        *,
        status: Literal["interrupted", "failed"],
        safe_error: str | None,
        total_tool_calls: int,
    ) -> TurnRecord:
        _non_negative(total_tool_calls, "total_tool_calls")
        now = _now()
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await _fetchone(
                connection,
                "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            )
            if row is None:
                await connection.rollback()
                raise TurnNotFoundError(f"turn {turn_id!r} not found")
            if row["status"] == "completed":
                await connection.rollback()
                raise InvalidConversationTransitionError(
                    "a completed turn cannot become nonterminal"
                )
            if row["status"] == "failed":
                await connection.rollback()
                if status == "failed":
                    return _turn_from_row(row)
                raise InvalidConversationTransitionError(
                    "a failed turn cannot become nonterminal"
                )
            if status == "failed":
                conversation = await _fetchone(
                    connection,
                    "SELECT * FROM conversations WHERE thread_id = ?",
                    (thread_id,),
                )
                assert conversation is not None
                try:
                    _validate_terminal_charge(row, conversation, total_tool_calls)
                except ConversationRepositoryError:
                    await connection.rollback()
                    raise
            await connection.execute(
                """
                UPDATE turns SET status = ?, total_tool_calls = ?, safe_error = ?
                WHERE thread_id = ? AND turn_id = ?
                """,
                (status, total_tool_calls, safe_error, thread_id, turn_id),
            )
            if status == "failed":
                await connection.execute(
                    """
                    UPDATE conversations SET status = ?, updated_at = ?,
                        session_tool_calls = session_tool_calls + ?
                    WHERE thread_id = ?
                    """,
                    (status, now, total_tool_calls, thread_id),
                )
            else:
                await connection.execute(
                    "UPDATE conversations SET status = ?, updated_at = ? "
                    "WHERE thread_id = ?",
                    (status, now, thread_id),
                )
            await connection.commit()
        return await self.get_turn(thread_id, turn_id)

    def _connection(self) -> _ConnectionContext:
        return _ConnectionContext(self.database_path)


class _ConnectionContext:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(str(self._path))
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await _initialize_schema(connection)
        self._connection = connection
        return connection

    async def __aexit__(self, *args: object) -> None:
        assert self._connection is not None
        await self._connection.close()


async def _initialize_schema(connection: aiosqlite.Connection) -> None:
    await connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            thread_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','interrupted','failed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            session_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(session_tool_calls >= 0),
            session_tool_call_limit INTEGER NOT NULL CHECK(session_tool_call_limit > 0)
        );
        CREATE TABLE IF NOT EXISTS turns (
            thread_id TEXT NOT NULL REFERENCES conversations(thread_id) ON DELETE CASCADE,
            turn_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            user_message TEXT NOT NULL,
            assistant_response TEXT,
            status TEXT NOT NULL CHECK(status IN ('running','completed','interrupted','failed')),
            checkpoint_namespace TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            total_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(total_tool_calls >= 0),
            reserved_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_tool_calls >= 0),
            trace_json TEXT NOT NULL DEFAULT '[]',
            safe_error TEXT,
            PRIMARY KEY(thread_id, turn_id),
            UNIQUE(thread_id, sequence),
            UNIQUE(thread_id, checkpoint_namespace)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn_per_conversation
        ON turns(thread_id) WHERE status IN ('running','interrupted');
        """
    )
    await connection.commit()


async def _fetchone(
    connection: aiosqlite.Connection,
    query: str,
    parameters: tuple[object, ...],
) -> aiosqlite.Row | None:
    cursor = await connection.execute(query, parameters)
    return await cursor.fetchone()


def _conversation_from_row(row: aiosqlite.Row) -> ConversationRecord:
    return ConversationRecord(
        thread_id=row["thread_id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        session_tool_calls=row["session_tool_calls"],
        session_tool_call_limit=row["session_tool_call_limit"],
    )


def _validate_terminal_charge(
    turn: aiosqlite.Row,
    conversation: aiosqlite.Row,
    total_tool_calls: int,
) -> None:
    if total_tool_calls < int(turn["total_tool_calls"]):
        raise InvalidConversationTransitionError(
            "terminal tool-call total cannot decrease"
        )
    if total_tool_calls > int(turn["reserved_tool_calls"]):
        raise SessionBudgetExceededError(
            "terminal tool-call total exceeds the turn reservation"
        )
    if int(conversation["session_tool_calls"]) + total_tool_calls > int(
        conversation["session_tool_call_limit"]
    ):
        raise SessionBudgetExceededError(
            "terminal tool-call total exceeds the session limit"
        )


def _turn_from_row(row: aiosqlite.Row) -> TurnRecord:
    raw_traces = json.loads(row["trace_json"])
    return TurnRecord(
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        sequence=row["sequence"],
        user_message=row["user_message"],
        assistant_response=row["assistant_response"],
        status=row["status"],
        checkpoint_namespace=row["checkpoint_namespace"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        total_tool_calls=row["total_tool_calls"],
        reserved_tool_calls=row["reserved_tool_calls"],
        trace_events=tuple(str(item) for item in raw_traces),
        safe_error=row["safe_error"],
    )


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _message(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("user_message must be text")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 20_000:
        raise ValueError("user_message must contain between 1 and 20000 characters")
    return normalized


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = " …[truncated]"
    return value[: max(1, limit - len(marker))] + marker


def _safe_error(value: str) -> str:
    return _WHITESPACE.sub(" ", _CONTROL_CHARACTERS.sub(" ", value)).strip()[:240]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
