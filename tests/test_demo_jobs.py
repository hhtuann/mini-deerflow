from dataclasses import replace
from threading import Event

import pytest

from mini_deerflow.demo.jobs import (
    DemoJobManager,
    JobAlreadyActiveError,
    JobState,
    QueueTraceSink,
)
from mini_deerflow.demo.view_models import (
    ArtifactView,
    BudgetView,
    DemoRunView,
)
from mini_deerflow.tracing import (
    ExecutionTrace,
    TraceKind,
    TraceOutcome,
    TracePhase,
)


def _view(thread_id: str = "jobs-test") -> DemoRunView:
    return DemoRunView(
        thread_id=thread_id,
        operation="run",
        workflow_status="completed",
        goal="A safe deterministic demo goal.",
        plan=(),
        budget=BudgetView(0, 0, 0, 0, 1, 1, 0, 1, 1),
        evidence=(),
        accepted_citations=(),
        rejected_citation_count=0,
        reviews=(),
        replans=(),
        delegations=(),
        traces=(),
        artifact=ArtifactView("report", False, (), (), (), ""),
        limitations=(),
    )


def _event(sequence: int) -> ExecutionTrace:
    return ExecutionTrace(
        run_id="run-1",
        thread_id="jobs-test",
        sequence=sequence,
        kind=TraceKind.RUN,
        phase=TracePhase.OUTCOME,
        outcome=TraceOutcome.SUCCEEDED,
        operation="run",
    )


def test_trace_queue_is_fifo_and_drains_once() -> None:
    sink = QueueTraceSink()
    sink.emit(_event(1))
    sink.emit(_event(2))
    assert [event.sequence for event in sink.drain()] == [1, 2]
    assert sink.drain() == ()
    assert [event.sequence for event in sink.history()] == [1, 2]


def test_job_manager_transitions_rejects_double_submit_and_retains_last_view() -> None:
    started = Event()
    release = Event()

    def gated(_sink):
        started.set()
        assert release.wait(timeout=5)
        return _view()

    with DemoJobManager() as manager:
        assert manager.submit("run", "jobs-test", gated) == "demo-job-0001"
        assert started.wait(timeout=5)
        snapshot = manager.snapshot()
        assert snapshot is not None and snapshot.state is JobState.RUNNING
        with pytest.raises(JobAlreadyActiveError):
            manager.submit("resume", "jobs-test", lambda _sink: _view())
        release.set()
        manager._future.result(timeout=5)  # type: ignore[union-attr]
        snapshot = manager.snapshot()
        assert snapshot is not None and snapshot.state is JobState.SUCCEEDED
        assert snapshot.last_completed_view == _view()

        second_started = Event()
        second_release = Event()

        def second(_sink):
            second_started.set()
            assert second_release.wait(timeout=5)
            return replace(_view(), operation="resume")

        manager.submit("resume", "jobs-test", second)
        assert second_started.wait(timeout=5)
        snapshot = manager.snapshot()
        assert snapshot is not None and snapshot.display_view == _view()
        second_release.set()
        manager._future.result(timeout=5)  # type: ignore[union-attr]


def test_job_manager_exposes_only_controlled_error_message() -> None:
    def fail(_sink):
        raise RuntimeError("SECRET RAW EXCEPTION")

    with DemoJobManager(
        error_mapper=lambda _error, job_id: f"Failed: {job_id}"
    ) as manager:
        manager.submit("run", "jobs-test", fail)
        with pytest.raises(RuntimeError, match="SECRET"):
            manager._future.result(timeout=5)  # type: ignore[union-attr]
        snapshot = manager.snapshot()
        assert snapshot is not None
        assert snapshot.state is JobState.FAILED
        assert snapshot.error_message == "Failed: demo-job-0001"
        assert "SECRET" not in snapshot.error_message
