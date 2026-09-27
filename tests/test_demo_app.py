import ast
import tomllib
from dataclasses import replace
from pathlib import Path

from streamlit.testing.v1 import AppTest

from mini_deerflow.demo.jobs import DemoJobManager, JobSnapshot, JobState
from mini_deerflow.demo.view_models import (
    ArtifactView,
    BudgetView,
    ChatSessionView,
    ChatTurnView,
    ConversationSummaryView,
    DemoRunView,
    TraceEventView,
)

APP_PATH = Path(__file__).parents[1] / "src" / "mini_deerflow" / "demo" / "app.py"
STREAMLIT_CONFIG_PATH = Path(__file__).parents[1] / ".streamlit" / "config.toml"


class _UnusedService:
    async def list_conversations(self) -> tuple[ConversationSummaryView, ...]:
        return ()

    async def load_conversation(self, _thread_id: str) -> ChatSessionView:
        raise AssertionError("conversation reload was not expected")


class _SnapshotManager:
    def __init__(
        self,
        snapshot: JobSnapshot | None,
        traces: tuple[TraceEventView, ...] = (),
    ) -> None:
        self._snapshot = snapshot
        self._traces = traces

    def snapshot(self) -> JobSnapshot | None:
        return self._snapshot

    def trace_history(self) -> tuple[TraceEventView, ...]:
        return self._traces


class _ChatService:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str, str]] = []
        self.session: ChatSessionView | None = None

    async def list_conversations(self) -> tuple[ConversationSummaryView, ...]:
        return () if self.session is None else (self.session.conversation,)

    async def load_conversation(self, thread_id: str) -> ChatSessionView:
        assert self.session is not None
        assert thread_id == self.session.conversation.thread_id
        return self.session

    async def run(self, command, *, trace_sink=None) -> DemoRunView:
        del trace_sink
        self.commands.append(("run", command.thread_id, command.turn_id))
        summary = ConversationSummaryView(
            thread_id=command.thread_id,
            title=command.goal,
            status="completed",
            updated_at="2026-09-20T00:00:00+00:00",
            session_tool_calls=1,
            session_tool_call_limit=100,
        )
        view = replace(
            _safe_view(command.turn_id, "# First generated answer"),
            thread_id=command.thread_id,
        )
        self.session = ChatSessionView(
            conversation=summary,
            turns=(
                ChatTurnView(
                    turn_id=command.turn_id,
                    sequence=1,
                    user_message=command.goal,
                    assistant_message="## Answer\n\nFirst public answer.",
                    status="completed",
                    safe_error=None,
                    run=view,
                ),
            ),
        )
        return view

    async def continue_thread(self, command, *, trace_sink=None) -> DemoRunView:
        del trace_sink
        assert self.session is not None
        self.commands.append(("continue", command.thread_id, command.turn_id))
        assert command.thread_id == self.session.conversation.thread_id
        view = replace(
            _safe_view(command.turn_id, "# Follow-up generated answer"),
            thread_id=command.thread_id,
        )
        turn = ChatTurnView(
            turn_id=command.turn_id,
            sequence=2,
            user_message=command.user_message,
            assistant_message="## Answer\n\nFollow-up public answer.",
            status="completed",
            safe_error=None,
            run=view,
        )
        self.session = replace(
            self.session,
            conversation=replace(
                self.session.conversation,
                session_tool_calls=2,
            ),
            turns=self.session.turns + (turn,),
        )
        return view


def _safe_trace(turn_id: str = "turn-one") -> TraceEventView:
    return TraceEventView(
        run_id=f"run-{turn_id}",
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
        artifact_count=1,
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
        turn_id=turn_id,
    )


def _safe_view(turn_id: str, research_report: str) -> DemoRunView:
    return DemoRunView(
        thread_id="ui-safe-thread",
        operation="run" if turn_id == "turn-one" else "continue",
        workflow_status="completed",
        goal=f"Safe goal for {turn_id}.",
        plan=(),
        budget=BudgetView(0, 0, 0, 0, 4, 8, 0, 1, 2),
        evidence=(),
        accepted_citations=(),
        rejected_citation_count=0,
        reviews=(),
        replans=(),
        delegations=(),
        traces=(_safe_trace(turn_id),),
        artifact=ArtifactView(
            label="report",
            available=True,
            finding_summaries=("Safe finding.",),
            citation_urls=(),
            limitation_notices=("Safe limitation.",),
            markdown_source=research_report,
        ),
        limitations=("Local demo only.",),
        turn_id=turn_id,
    )


def _chat_session() -> ChatSessionView:
    summary = ConversationSummaryView(
        thread_id="ui-safe-thread",
        title="A persistent research conversation",
        status="completed",
        updated_at="2026-09-20T00:00:00+00:00",
        session_tool_calls=4,
        session_tool_call_limit=100,
    )
    return ChatSessionView(
        conversation=summary,
        turns=(
            ChatTurnView(
                turn_id="turn-one",
                sequence=1,
                user_message="Find the first public fact.",
                assistant_message="## Answer\n\nFirst answer only.",
                status="completed",
                safe_error=None,
                run=_safe_view(
                    "turn-one",
                    "# Research Report\n\nFIRST_INTERNAL_REPORT_CANARY",
                ),
            ),
            ChatTurnView(
                turn_id="turn-two",
                sequence=2,
                user_message="Now compare it with the second fact.",
                assistant_message="## Answer\n\nFollow-up answer only.",
                status="completed",
                safe_error=None,
                run=_safe_view(
                    "turn-two",
                    "# Research Report\n\nSECOND_INTERNAL_REPORT_CANARY",
                ),
            ),
        ),
    )


def _seed_dependencies(
    app: AppTest,
    manager: _SnapshotManager,
    session: ChatSessionView | None = None,
) -> None:
    app.session_state["demo-runtime-service"] = _UnusedService()
    app.session_state["demo-job-manager"] = manager
    app.session_state["demo-conversations"] = (
        () if session is None else (session.conversation,)
    )
    app.session_state["demo-selected-thread"] = (
        None if session is None else session.conversation.thread_id
    )
    app.session_state["demo-chat-session"] = session


def test_streamlit_initial_surface_is_an_honest_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_DEERFLOW_API_KEY", "test-model-key")
    monkeypatch.setenv("MINI_DEERFLOW_JINA_API_KEY", "test-jina-key")
    app = AppTest.from_file(APP_PATH).run(timeout=10)

    assert not app.exception
    assert [button.label for button in app.sidebar.button] == ["New chat", "Refresh"]
    assert [selectbox.label for selectbox in app.sidebar.selectbox] == [
        "Execution mode"
    ]
    assert app.sidebar.selectbox[0].value == "Live web"
    assert len(app.chat_input) == 1
    assert app.chat_input[0].disabled is False
    assert not app.tabs
    assert any(
        "Jina key is required" in caption.value for caption in app.sidebar.caption
    )


def test_streamlit_renders_two_turns_from_durable_chat_projection() -> None:
    session = _chat_session()
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(None), session)
    app.run(timeout=10)

    assert not app.exception
    assert len(app.chat_message) == 4
    assert [tab.label for tab in app.tabs] == [
        "Execution",
        "Trace",
        "Research report",
    ] * 2
    visible_markdown = "\n".join(str(item.value) for item in app.markdown)
    assert "First answer only" in visible_markdown
    assert "Follow-up answer only" in visible_markdown
    assert "FIRST_INTERNAL_REPORT_CANARY" not in visible_markdown
    assert "SECOND_INTERNAL_REPORT_CANARY" not in visible_markdown
    assert len(app.expander) == 2
    assert [expander.label for expander in app.expander] == [
        "Xem quá trình Agent",
        "Xem quá trình Agent",
    ]
    assert '"Xem quá trình Agent"' in APP_PATH.read_text(encoding="utf-8")
    assert any("FIRST_INTERNAL_REPORT_CANARY" in str(item.value) for item in app.code)
    assert "4/100" in "\n".join(item.value for item in app.caption)


def test_streamlit_disables_new_work_while_a_turn_is_running() -> None:
    session = _chat_session()
    live_trace = replace(_safe_trace("turn-three"), outcome="started")
    snapshot = JobSnapshot(
        job_id="demo-job-0001",
        operation="continue",
        thread_id=session.conversation.thread_id,
        state=JobState.RUNNING,
        error_message=None,
        view=None,
        last_completed_view=session.turns[-1].run,
        turn_id="turn-three",
    )
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(snapshot, (live_trace,)), session)
    app.session_state["demo-pending-message"] = "Research one more public fact."
    app.run(timeout=10)

    assert not app.exception
    assert all(button.disabled for button in app.sidebar.button)
    assert app.chat_input[0].disabled is True
    # The just-submitted message and assistant status remain visible while the
    # durable worker turn is active.
    assert len(app.chat_message) == 6
    trace_table = app.dataframe[-1].value
    assert trace_table["Thread"].tolist() == ["ui-safe-thread"]
    assert trace_table["Turn"].tolist() == ["turn-three"]


def test_completed_ledger_message_renders_safely_without_checkpoint_view() -> None:
    session = _chat_session()
    fallback_turn = replace(
        session.turns[0],
        assistant_message="![remote](https://untrusted.invalid/pixel) plain answer",
        run=None,
    )
    session = replace(session, turns=(fallback_turn,))
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(None), session)
    app.run(timeout=10)

    assert not app.exception
    rendered_markdown = "\n".join(str(item.value) for item in app.markdown)
    assert "plain answer" in rendered_markdown
    assert "untrusted.invalid" not in rendered_markdown
    assert not app.image


def test_persisted_running_turn_requires_resume_before_new_message() -> None:
    session = _chat_session()
    running = replace(
        session.turns[-1],
        status="running",
        assistant_message=None,
        run=None,
    )
    session = replace(session, turns=session.turns[:-1] + (running,))
    app = AppTest.from_file(APP_PATH)
    _seed_dependencies(app, _SnapshotManager(None), session)
    app.run(timeout=10)

    assert not app.exception
    assert app.button(key="demo-resume-turn").label == "Resume active turn"
    assert app.chat_input[0].disabled is True


def test_streamlit_chat_input_creates_then_continues_the_same_thread() -> None:
    service = _ChatService()
    manager = DemoJobManager()
    try:
        app = AppTest.from_file(APP_PATH)
        app.session_state["demo-runtime-service"] = service
        app.session_state["demo-job-manager"] = manager
        app.session_state["demo-conversations"] = ()
        app.session_state["demo-selected-thread"] = None
        app.session_state["demo-chat-session"] = None
        app.run(timeout=10)

        app.chat_input[0].set_value("Research the first public fact.").run(timeout=10)
        assert manager._future is not None
        manager._future.result(timeout=5)
        app.run(timeout=10)

        app.chat_input[0].set_value("Compare it with another public fact.").run(
            timeout=10
        )
        assert manager._future is not None
        manager._future.result(timeout=5)
        app.run(timeout=10)

        assert [operation for operation, _, _ in service.commands] == [
            "run",
            "continue",
        ]
        assert service.commands[0][1] == service.commands[1][1]
        assert service.commands[0][2] != service.commands[1][2]
        assert len(app.chat_message) == 4
    finally:
        manager.shutdown()


def test_streamlit_uses_native_chat_primitives_and_no_legacy_goal_form() -> None:
    module = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    attribute_calls = {
        node.func.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert {"chat_message", "chat_input"} <= attribute_calls
    assert "text_area" not in attribute_calls
    assert "use_container_width" not in APP_PATH.read_text(encoding="utf-8")


def test_project_streamlit_config_disables_usage_telemetry() -> None:
    with STREAMLIT_CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config["browser"]["gatherUsageStats"] is False
