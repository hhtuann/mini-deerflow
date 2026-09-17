import ast
import tomllib
from dataclasses import replace
from pathlib import Path

from streamlit.testing.v1 import AppTest

from mini_deerflow.demo.jobs import JobSnapshot, JobState
from mini_deerflow.demo.view_models import (
    ArtifactView,
    BudgetView,
    DemoRunView,
    TraceEventView,
)

APP_PATH = Path(__file__).parents[1] / "src" / "mini_deerflow" / "demo" / "app.py"
STREAMLIT_CONFIG_PATH = Path(__file__).parents[1] / ".streamlit" / "config.toml"


class _UnusedService:
    async def list_threads(self) -> tuple[str, ...]:
        raise AssertionError("thread refresh was not expected")


class _SnapshotManager:
    def __init__(self, snapshot: JobSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> JobSnapshot | None:
        return self._snapshot

    def drain_trace_views(self) -> tuple[object, ...]:
        return ()


class _LiveSnapshotManager(_SnapshotManager):
    def __init__(
        self, snapshot: JobSnapshot, live_traces: tuple[TraceEventView, ...]
    ) -> None:
        super().__init__(snapshot)
        self._live_traces = live_traces

    def drain_trace_views(self) -> tuple[TraceEventView, ...]:
        return self._live_traces


def _safe_view() -> DemoRunView:
    return DemoRunView(
        thread_id="ui-safe-thread",
        operation="run",
        workflow_status="completed",
        goal="A deterministic mentor demonstration goal.",
        plan=(),
        budget=BudgetView(0, 0, 0, 0, 4, 8, 0, 1, 2),
        evidence=(),
        accepted_citations=(),
        rejected_citation_count=2,
        reviews=(),
        replans=(),
        delegations=(),
        traces=(),
        artifact=ArtifactView(
            label="report",
            available=True,
            finding_summaries=("Safe finding.",),
            citation_urls=(),
            limitation_notices=("Safe limitation.",),
            markdown_source="# Safe report\n\nNo active content.",
        ),
        limitations=("Local demo only.",),
    )


def _safe_trace() -> TraceEventView:
    return TraceEventView(
        run_id="run-0001",
        thread_id="ui-safe-thread",
        sequence=1,
        kind="workflow",
        phase="run",
        outcome="success",
        operation="run",
        node="planner",
        tool_name=None,
        route=None,
        duration_ms=1,
        current_step=1,
        step_tool_calls=0,
        total_tool_calls=0,
        max_step_tool_calls=4,
        max_total_tool_calls=8,
        max_replan_cycles=1,
        evidence_count=0,
        citation_count=0,
        rejected_citation_count=0,
        artifact_count=0,
        branch_count=0,
        successful_branch_count=0,
        failed_branch_count=0,
        cancelled_branch_count=0,
        reserved_tool_calls=0,
        used_tool_calls=0,
        charged_tool_calls=0,
        context_omitted_items=0,
        context_truncated_items=0,
        context_estimated_tokens=0,
        error_category=None,
        error_code=None,
    )


def _seed_dependencies(app: AppTest, manager: _SnapshotManager) -> None:
    app.session_state["demo-runtime-service"] = _UnusedService()
    app.session_state["demo-job-manager"] = manager
    app.session_state["demo-thread-options"] = ("ui-safe-thread",)


def test_streamlit_initial_surface_has_exact_controls_and_tabs() -> None:
    app = AppTest.from_file(APP_PATH).run(timeout=10)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Overview",
        "Evidence & Citations",
        "Delegation",
        "Trace",
        "Artifact",
    ]
    assert [button.label for button in app.sidebar.button] == [
        "Run",
        "Resume selected thread",
        "Refresh threads",
    ]
    assert app.sidebar.button[1].disabled
    labels = [item.label for item in app.sidebar.text_input]
    labels += [item.label for item in app.sidebar.text_area]
    assert labels == ["New thread ID", "Goal"]
    visible = "\n".join(item.value for item in app.text)
    assert "Scenario: Mentor walkthrough v1" in visible


def test_streamlit_invalid_thread_is_controlled_and_does_not_start_job() -> None:
    app = AppTest.from_file(APP_PATH).run(timeout=10)
    app.sidebar.text_input(key="demo-new-thread-id").set_value("not valid!")
    app.sidebar.button(key="demo-run").click().run(timeout=10)
    assert not app.exception
    assert app.error[0].value == "Input validation failed."
    assert app.sidebar.button(key="demo-run").disabled is False


def test_streamlit_disables_controls_for_a_running_job_and_retains_view() -> None:
    view = _safe_view()
    snapshot = JobSnapshot(
        job_id="demo-job-0001",
        operation="resume",
        thread_id=view.thread_id,
        state=JobState.RUNNING,
        error_message=None,
        view=None,
        last_completed_view=view,
    )
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(snapshot))
    app.session_state["demo-last-completed-view"] = view
    app.run(timeout=10)

    assert not app.exception
    assert all(button.disabled for button in app.sidebar.button)
    assert app.sidebar.text_input(key="demo-new-thread-id").disabled
    assert app.sidebar.text_area(key="demo-goal").disabled
    assert any(code.value.startswith("# Safe report") for code in app.code)


def test_streamlit_completed_view_renders_safe_source_without_canaries() -> None:
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(None))
    app.session_state["demo-last-completed-view"] = _safe_view()
    app.run(timeout=10)

    assert not app.exception
    visible = "\n".join(
        str(element.value)
        for collection in (app.text, app.code, app.info, app.caption)
        for element in collection
    )
    assert "# Safe report" in visible
    assert "Safe finding." in visible
    for forbidden in ("SECRET", "RAW_PROVIDER_PAYLOAD", "BETA_FALSE", ".env"):
        assert forbidden not in visible


def test_trace_filters_initialize_after_empty_to_completed_transition() -> None:
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(None))
    app.run(timeout=10)
    assert not app.exception
    assert len(app.multiselect) == 0

    app.session_state["demo-last-completed-view"] = replace(
        _safe_view(), traces=(_safe_trace(),)
    )
    app.run(timeout=10)

    assert not app.exception
    assert [filter_widget.value for filter_widget in app.multiselect] == [
        ["run-0001"],
        ["workflow"],
        ["success"],
    ]


def test_running_job_shows_only_live_trace_with_thread_identity() -> None:
    completed_view = replace(_safe_view(), traces=(_safe_trace(),))
    live_trace = replace(
        _safe_trace(),
        run_id="run-live",
        thread_id="thread-live",
        sequence=2,
        kind="tool",
    )
    snapshot = JobSnapshot(
        job_id="demo-job-0002",
        operation="run",
        thread_id="thread-live",
        state=JobState.RUNNING,
        error_message=None,
        view=None,
        last_completed_view=completed_view,
    )
    manager = _LiveSnapshotManager(snapshot, (live_trace,))
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, manager)
    app.session_state["demo-last-completed-view"] = completed_view
    app.run(timeout=10)

    assert not app.exception
    trace_table = app.dataframe[-1].value
    assert trace_table["Thread"].tolist() == ["thread-live"]
    assert trace_table["Run"].tolist() == ["run-live"]


def test_running_job_does_not_fall_back_to_previous_trace_before_first_event() -> None:
    completed_view = replace(_safe_view(), traces=(_safe_trace(),))
    snapshot = JobSnapshot(
        job_id="demo-job-0002",
        operation="run",
        thread_id="thread-live",
        state=JobState.RUNNING,
        error_message=None,
        view=None,
        last_completed_view=completed_view,
    )
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _LiveSnapshotManager(snapshot, ()))
    app.session_state["demo-last-completed-view"] = completed_view
    app.run(timeout=10)

    assert not app.exception
    assert any(text.value == "No trace events are available yet." for text in app.text)
    assert not any("Thread" in frame.value.columns for frame in app.dataframe)


def test_active_fragment_owns_status_and_five_tab_rendering() -> None:
    module = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    fragment = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_poll_active_job"
    )
    called_names = {
        node.func.id
        for node in ast.walk(fragment)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "render_status_strip" in called_names
    assert "render_demo_tabs" in called_names


def test_failed_thread_keeps_its_status_and_trace_without_stale_progress() -> None:
    completed_view = replace(
        _safe_view(),
        thread_id="thread-a",
        traces=(replace(_safe_trace(), thread_id="thread-a", run_id="run-a"),),
    )
    failed_trace = replace(
        _safe_trace(),
        thread_id="thread-b",
        run_id="run-b",
        outcome="failed",
        error_category="runtime",
        error_code="runtime_failure",
    )
    snapshot = JobSnapshot(
        job_id="demo-job-0002",
        operation="run",
        thread_id="thread-b",
        state=JobState.FAILED,
        error_message="Demo operation failed. Reference: demo-job-0002.",
        view=None,
        last_completed_view=completed_view,
    )
    manager = _LiveSnapshotManager(snapshot, (failed_trace,))
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, manager)
    app.session_state["demo-last-completed-view"] = completed_view
    app.run(timeout=10)

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric[:4]}
    assert {key: metrics[key] for key in ("Operation", "State", "Thread")} == {
        "Operation": "Run",
        "State": "failed",
        "Thread": "thread-b",
    }
    assert metrics["Plan progress"] != "0 / 0"
    assert any(code.value.startswith("# Safe report") for code in app.code)
    assert any(
        "Other tabs retain the last completed safe result for thread thread-a."
        in caption.value
        for caption in app.caption
    )
    trace_table = app.dataframe[-1].value
    assert trace_table["Thread"].tolist() == ["thread-b"]
    assert trace_table["Run"].tolist() == ["run-b"]
    assert trace_table["Outcome"].tolist() == ["failed"]


def test_project_streamlit_config_disables_usage_telemetry() -> None:
    with STREAMLIT_CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config["browser"]["gatherUsageStats"] is False
