"""One-page localhost Streamlit application for the deterministic mentor demo."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import streamlit as st

from mini_deerflow.demo.components import render_demo_tabs, render_status_strip
from mini_deerflow.demo.jobs import DemoJobManager, JobSnapshot, JobState
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
    map_demo_error,
)
from mini_deerflow.demo.view_models import DemoRunView, TraceEventView
from mini_deerflow.tracing import TraceSink

SAMPLE_GOAL = "Demonstrate the bounded Mini DeerFlow MVP with verifiable evidence."
DEFAULT_THREAD_ID = "day-15-mentor-demo"

_SERVICE_KEY = "demo-runtime-service"
_MANAGER_KEY = "demo-job-manager"
_THREADS_KEY = "demo-thread-options"
_VIEW_KEY = "demo-last-completed-view"
_TRACES_KEY = "demo-live-traces"
_MESSAGE_KEY = "demo-user-message"
_ERROR_KEY = "demo-user-error"
_HANDLED_JOB_KEY = "demo-handled-job"


def _run_async(operation: Awaitable[object]) -> object:
    """Run a short async façade operation from the Streamlit script thread."""

    return asyncio.run(operation)


def _refresh_threads(service: DemoRuntimeService) -> tuple[str, ...]:
    threads = _run_async(service.list_threads())
    assert isinstance(threads, tuple)
    st.session_state[_THREADS_KEY] = threads
    return threads


def _default_dependencies() -> tuple[DemoRuntimeService, DemoJobManager]:
    """Create local dependencies lazily; importing this module has no side effects."""

    if _SERVICE_KEY not in st.session_state:
        demo_root = Path.cwd() / ".mini-deerflow" / "demo"
        st.session_state[_SERVICE_KEY] = DemoRuntimeService(
            OfflineDemoBackend(demo_root)
        )
    if _MANAGER_KEY not in st.session_state:
        st.session_state[_MANAGER_KEY] = DemoJobManager(
            error_mapper=lambda error, job_id: map_demo_error(
                error,
                reference_id=job_id,
            )
        )
    return st.session_state[_SERVICE_KEY], st.session_state[_MANAGER_KEY]


def _submit(
    manager: DemoJobManager,
    *,
    operation: str,
    thread_id: str,
    task: Callable[[TraceSink], DemoRunView],
) -> None:
    st.session_state[_ERROR_KEY] = None
    st.session_state[_MESSAGE_KEY] = None
    st.session_state[_TRACES_KEY] = ()
    manager.submit(operation=operation, thread_id=thread_id, task=task)


def _submit_run(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    *,
    thread_id: str,
    goal: str,
) -> None:
    command = RunDemoCommand(thread_id=thread_id, goal=goal)

    def task(trace_sink: TraceSink) -> DemoRunView:
        return cast(
            DemoRunView,
            _run_async(service.run(command, trace_sink=trace_sink)),
        )

    _submit(
        manager,
        operation="run",
        thread_id=command.thread_id,
        task=task,
    )


def _submit_resume(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    *,
    thread_id: str,
) -> None:
    command = ResumeDemoCommand(thread_id=thread_id)

    def task(trace_sink: TraceSink) -> DemoRunView:
        return cast(
            DemoRunView,
            _run_async(service.resume(command, trace_sink=trace_sink)),
        )

    _submit(
        manager,
        operation="resume",
        thread_id=command.thread_id,
        task=task,
    )


def _remember_traces(new_events: tuple[TraceEventView, ...]) -> None:
    previous = tuple(st.session_state.get(_TRACES_KEY, ()))
    st.session_state[_TRACES_KEY] = previous + new_events


def _consume_terminal_snapshot(
    service: DemoRuntimeService,
    snapshot: JobSnapshot,
) -> bool:
    """Copy one terminal result into UI state, including very fast jobs."""

    if snapshot.active or st.session_state.get(_HANDLED_JOB_KEY) == snapshot.job_id:
        return False
    if snapshot.state is JobState.SUCCEEDED and snapshot.view is not None:
        st.session_state[_VIEW_KEY] = snapshot.view
        st.session_state[_TRACES_KEY] = tuple(snapshot.view.traces)
        st.session_state[_MESSAGE_KEY] = (
            f"{snapshot.operation.title()} completed for thread {snapshot.thread_id}."
        )
        st.session_state[_ERROR_KEY] = None
        try:
            _refresh_threads(service)
        except Exception as error:  # noqa: BLE001 - converted at the UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
    elif snapshot.state is JobState.FAILED:
        st.session_state[_ERROR_KEY] = snapshot.error_message
    st.session_state[_HANDLED_JOB_KEY] = snapshot.job_id
    return True


@st.fragment(run_every=0.5, key="demo-active-job-poll")
def _poll_active_job(
    service: DemoRuntimeService,
    manager: DemoJobManager,
) -> None:
    """Poll and redraw the complete active-job surface as traces arrive."""

    _remember_traces(manager.drain_trace_views())
    snapshot = manager.snapshot()
    if snapshot is None:
        return
    if snapshot.active:
        render_status_strip(
            operation=snapshot.operation.title(),
            state=snapshot.state.value,
            thread_id=snapshot.thread_id,
        )
        view = st.session_state.get(_VIEW_KEY)
        if isinstance(view, DemoRunView):
            st.caption(
                f"Overview, evidence, delegation, and artifact retain the last "
                f"completed safe result for thread {view.thread_id}; Trace shows "
                f"the active {snapshot.operation} for thread {snapshot.thread_id}."
            )
        render_demo_tabs(
            view if isinstance(view, DemoRunView) else None,
            live_traces=tuple(st.session_state.get(_TRACES_KEY, ())),
        )
        return

    _consume_terminal_snapshot(service, snapshot)
    st.rerun(scope="app")


def _render_sidebar(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    *,
    active: bool,
) -> None:
    st.sidebar.header("Demo controls")
    st.sidebar.badge("Offline deterministic — no network", color="green")
    st.sidebar.text("Scenario: Mentor walkthrough v1")

    thread_id = st.sidebar.text_input(
        "New thread ID",
        value=DEFAULT_THREAD_ID,
        key="demo-new-thread-id",
        disabled=active,
    )
    goal = st.sidebar.text_area(
        "Goal",
        value=SAMPLE_GOAL,
        key="demo-goal",
        disabled=active,
    )

    threads = tuple(st.session_state.get(_THREADS_KEY, ()))
    selected_thread = st.sidebar.selectbox(
        "Existing thread",
        options=threads,
        index=0 if threads else None,
        key="demo-selected-thread",
        disabled=active or not threads,
        placeholder="No persisted threads",
    )

    if st.sidebar.button(
        "Run",
        type="primary",
        key="demo-run",
        disabled=active,
        use_container_width=True,
    ):
        try:
            _submit_run(
                service,
                manager,
                thread_id=thread_id,
                goal=goal,
            )
        except Exception as error:  # noqa: BLE001 - converted at the UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
        st.rerun()

    if st.sidebar.button(
        "Resume selected thread",
        key="demo-resume",
        disabled=active or selected_thread is None,
        use_container_width=True,
    ):
        try:
            assert selected_thread is not None
            _submit_resume(
                service,
                manager,
                thread_id=selected_thread,
            )
        except Exception as error:  # noqa: BLE001 - converted at the UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
        st.rerun()

    if st.sidebar.button(
        "Refresh threads",
        key="demo-refresh-threads",
        disabled=active,
        use_container_width=True,
    ):
        try:
            _refresh_threads(service)
            st.session_state[_MESSAGE_KEY] = "Thread list refreshed."
            st.session_state[_ERROR_KEY] = None
        except Exception as error:  # noqa: BLE001 - converted at the UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
        st.rerun()

    st.sidebar.info(
        "Local learning demo. Not production-ready. Not DeerFlow upstream parity."
    )


def render_page(
    service: DemoRuntimeService,
    manager: DemoJobManager,
) -> None:
    """Render the complete one-page UI using injected dependencies."""

    st.set_page_config(page_title="Mini DeerFlow Mentor Demo", layout="wide")
    st.title("Mini DeerFlow Mentor Demo")
    st.caption("A bounded, localhost-only walkthrough of the learning MVP.")

    if _THREADS_KEY not in st.session_state:
        try:
            _refresh_threads(service)
        except Exception as error:  # noqa: BLE001 - converted at the UI boundary
            st.session_state[_THREADS_KEY] = ()
            st.session_state[_ERROR_KEY] = map_demo_error(error)

    snapshot = manager.snapshot()
    active = snapshot is not None and snapshot.active
    if snapshot is not None and not active:
        # A job can finish before the first timed-fragment poll. Drain its
        # already-redacted queue so a failed operation still has a timeline.
        _remember_traces(manager.drain_trace_views())
        _consume_terminal_snapshot(service, snapshot)
    _render_sidebar(service, manager, active=active)

    message = st.session_state.get(_MESSAGE_KEY)
    error_message = st.session_state.get(_ERROR_KEY)
    if message:
        st.success(message)
    if error_message:
        st.error(error_message)

    view = st.session_state.get(_VIEW_KEY)
    if active:
        _poll_active_job(service, manager)
        return

    if snapshot is not None and snapshot.state is JobState.FAILED:
        render_status_strip(
            operation=snapshot.operation.title(),
            state=snapshot.state.value,
            thread_id=snapshot.thread_id,
        )
        if isinstance(view, DemoRunView):
            st.caption(
                f"Status and Trace refer to the failed {snapshot.operation} for "
                f"thread {snapshot.thread_id}. Other tabs retain the last completed "
                f"safe result for thread {view.thread_id}."
            )
        render_demo_tabs(
            view if isinstance(view, DemoRunView) else None,
            live_traces=tuple(st.session_state.get(_TRACES_KEY, ())),
        )
    elif isinstance(view, DemoRunView):
        render_status_strip(
            operation=(
                snapshot.operation.title() if snapshot is not None else "Completed"
            ),
            state=(
                snapshot.state.value if snapshot is not None else view.workflow_status
            ),
            thread_id=view.thread_id,
            current_step=view.budget.current_step,
            total_steps=view.budget.total_steps,
        )
        render_demo_tabs(view)
    else:
        render_status_strip(operation="-", state="idle", thread_id="")
        render_demo_tabs(None)


def main() -> None:
    """Create default local dependencies and render the Streamlit page."""

    service, manager = _default_dependencies()
    render_page(service, manager)


if __name__ == "__main__":
    main()
