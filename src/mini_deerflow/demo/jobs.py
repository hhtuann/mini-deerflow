"""Single-worker background execution for the local Streamlit demo."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from queue import Empty, Queue
from threading import Lock
from typing import Literal, Self

from mini_deerflow.demo.view_models import (
    DemoRunView,
    TraceEventView,
    project_trace_event,
)
from mini_deerflow.tracing import ExecutionTrace, TraceSink

JobOperation = Literal["run", "resume"]
JobTask = Callable[[TraceSink], DemoRunView]
JobErrorMapper = Callable[[BaseException, str], str]


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobAlreadyActiveError(RuntimeError):
    """Raised when a second operation is submitted to a busy session."""


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    operation: JobOperation
    thread_id: str
    state: JobState
    error_message: str | None
    view: DemoRunView | None
    last_completed_view: DemoRunView | None

    @property
    def active(self) -> bool:
        return self.state in {JobState.QUEUED, JobState.RUNNING}

    @property
    def display_view(self) -> DemoRunView | None:
        return self.view or self.last_completed_view


class QueueTraceSink:
    """FIFO trace sink that exposes safe trace views, never runtime events."""

    def __init__(self) -> None:
        self._queue: Queue[TraceEventView] = Queue()
        self._history: list[TraceEventView] = []
        self._history_lock = Lock()

    def emit(self, event: ExecutionTrace) -> None:
        view = project_trace_event(event)
        with self._history_lock:
            self._history.append(view)
        self._queue.put(view)

    def drain(self) -> tuple[TraceEventView, ...]:
        drained: list[TraceEventView] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except Empty:
                return tuple(drained)

    def history(self) -> tuple[TraceEventView, ...]:
        with self._history_lock:
            return tuple(self._history)


class DemoJobManager:
    """Own exactly one worker and at most one active demo operation."""

    def __init__(
        self,
        *,
        error_mapper: JobErrorMapper | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="mini-deerflow-demo",
        )
        self._error_mapper = error_mapper or _default_error_message
        self._lock = Lock()
        self._counter = 0
        self._closed = False
        self._future: Future[DemoRunView] | None = None
        self._trace_sink: QueueTraceSink | None = None
        self._job_id: str | None = None
        self._operation: JobOperation | None = None
        self._thread_id: str | None = None
        self._state: JobState | None = None
        self._error_message: str | None = None
        self._view: DemoRunView | None = None
        self._last_completed_view: DemoRunView | None = None

    def submit(
        self,
        operation: JobOperation,
        thread_id: str,
        task: JobTask,
    ) -> str:
        if operation not in ("run", "resume"):
            raise ValueError("operation must be 'run' or 'resume'")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if not callable(task):
            raise TypeError("task must be callable")

        with self._lock:
            if self._closed:
                raise RuntimeError("job manager is closed")
            if self._state in {JobState.QUEUED, JobState.RUNNING}:
                raise JobAlreadyActiveError("a demo operation is already active")

            self._counter += 1
            job_id = f"demo-job-{self._counter:04d}"
            trace_sink = QueueTraceSink()
            self._trace_sink = trace_sink
            self._job_id = job_id
            self._operation = operation
            self._thread_id = thread_id.strip()
            self._state = JobState.QUEUED
            self._error_message = None
            self._view = None
            self._future = self._executor.submit(
                self._execute,
                job_id,
                task,
                trace_sink,
            )
            return job_id

    def snapshot(self) -> JobSnapshot | None:
        with self._lock:
            if (
                self._job_id is None
                or self._operation is None
                or self._thread_id is None
                or self._state is None
            ):
                return None
            return JobSnapshot(
                job_id=self._job_id,
                operation=self._operation,
                thread_id=self._thread_id,
                state=self._state,
                error_message=self._error_message,
                view=self._view,
                last_completed_view=self._last_completed_view,
            )

    def drain_trace_views(self) -> tuple[TraceEventView, ...]:
        with self._lock:
            sink = self._trace_sink
        return () if sink is None else sink.drain()

    def trace_history(self) -> tuple[TraceEventView, ...]:
        with self._lock:
            sink = self._trace_sink
        return () if sink is None else sink.history()

    @property
    def is_active(self) -> bool:
        snapshot = self.snapshot()
        return snapshot is not None and snapshot.active

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()

    def _execute(
        self,
        job_id: str,
        task: JobTask,
        trace_sink: QueueTraceSink,
    ) -> DemoRunView:
        with self._lock:
            self._state = JobState.RUNNING
        try:
            view = task(trace_sink)
            if not isinstance(view, DemoRunView):
                raise TypeError("demo job must return DemoRunView")
        except BaseException as error:
            message = self._safe_error_message(error, job_id)
            with self._lock:
                self._state = JobState.FAILED
                self._error_message = message
                self._view = None
            raise
        else:
            with self._lock:
                self._state = JobState.SUCCEEDED
                self._error_message = None
                self._view = view
                self._last_completed_view = view
            return view

    def _safe_error_message(self, error: BaseException, job_id: str) -> str:
        try:
            message = self._error_mapper(error, job_id)
        except Exception:  # noqa: BLE001 - a broken mapper must not strand the job
            return _default_error_message(error, job_id)
        if not isinstance(message, str) or not message.strip():
            return _default_error_message(error, job_id)
        # The mapper is a trusted boundary and must return controlled prose.  We
        # still remove control characters and cap the UI-visible result.
        message = _CONTROL_CHARACTERS.sub(" ", message)
        return " ".join(message.split())[:240]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _default_error_message(error: BaseException, job_id: str) -> str:
    del error
    return f"Demo operation failed. Reference: {job_id}."
