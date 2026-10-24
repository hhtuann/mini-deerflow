"""Bounded HTTP job identities over the existing demo worker and safe snapshots."""

import asyncio
from collections import OrderedDict
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException

from mini_deerflow.api.dto import JobDTO, Mode
from mini_deerflow.demo.jobs import DemoJobManager, JobOperation, JobState, JobTask
from mini_deerflow.demo.service import map_demo_error

MAX_JOB_HISTORY = 32


class ApiJobs:
    def __init__(self) -> None:
        self._lock = Lock()
        self.submission_lock = asyncio.Lock()
        self._manager = DemoJobManager(error_mapper=map_demo_error)
        self._history: OrderedDict[str, JobDTO] = OrderedDict()
        self._current: str | None = None
        self._closed = False

    def _refresh(self) -> None:
        if self._current is None:
            return
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return
        previous = self._history[self._current]
        self._history[self._current] = previous.model_copy(
            update={
                "status": (
                    "completed"
                    if snapshot.state == JobState.SUCCEEDED
                    else snapshot.state.value
                ),
                "error_message": snapshot.error_message,
                "view": snapshot.view,
                "turn_id": (
                    snapshot.view.turn_id if snapshot.view else previous.turn_id
                ),
                "traces": self._manager.trace_history(),
                "execution_graph": self._manager.execution_graph(),
            }
        )
        # The queue is for a one-shot UI consumer; HTTP uses repeatable history.
        self._manager.drain_trace_views()

    def check_idle(self) -> None:
        with self._lock:
            if self._closed:
                raise HTTPException(503, "Server is shutting down.")
            if self._manager.is_active:
                raise HTTPException(409, "An operation is already active.")

    def submit(
        self,
        mode: Mode,
        operation: JobOperation,
        thread_id: str,
        turn_id: str,
        task: JobTask,
    ) -> JobDTO:
        with self._lock:
            if self._closed:
                raise HTTPException(503, "Server is shutting down.")
            if self._manager.is_active:
                raise HTTPException(409, "An operation is already active.")
            self._refresh()
            job_id = str(uuid4())
            self._manager.submit(operation, thread_id, task, turn_id=turn_id)
            self._current = job_id
            self._history[job_id] = JobDTO(
                job_id=job_id,
                mode=mode,
                operation=operation,
                thread_id=thread_id,
                turn_id=turn_id,
                status="queued",
                execution_graph=self._manager.execution_graph(),
            )
            while len(self._history) > MAX_JOB_HISTORY:
                self._history.popitem(last=False)
            self._refresh()
            return self._history[job_id]

    def list(self, mode: Mode) -> tuple[JobDTO, ...]:
        with self._lock:
            self._refresh()
            return tuple(job for job in self._history.values() if job.mode == mode)

    def get(self, mode: Mode, job_id: str) -> JobDTO:
        with self._lock:
            self._refresh()
            job = self._history.get(job_id)
            if job is None or job.mode != mode:
                raise HTTPException(404, "Job was not found.")
            return job

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._manager.shutdown(wait=True)
