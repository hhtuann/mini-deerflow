"""Persistent multi-turn Streamlit chat for the Mini DeerFlow runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

import streamlit as st

from mini_deerflow.demo.components import (
    render_agent_graph,
    render_demo_tabs,
    render_trace,
)
from mini_deerflow.demo.jobs import DemoJobManager, JobSnapshot, JobState
from mini_deerflow.demo.live_backend import LiveDemoBackend
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
    map_demo_error,
)
from mini_deerflow.demo.view_models import (
    ChatSessionView,
    ConversationSummaryView,
    DemoRunView,
    sanitize_answer_markdown,
)
from mini_deerflow.tracing import TraceSink

_SERVICE_KEY = "demo-runtime-service"
_MANAGER_KEY = "demo-job-manager"
_CONVERSATIONS_KEY = "demo-conversations"
_SELECTED_THREAD_KEY = "demo-selected-thread"
_SESSION_KEY = "demo-chat-session"
_ERROR_KEY = "demo-user-error"
_HANDLED_JOB_KEY = "demo-handled-job"
_LIVE_TRACES_KEY = "demo-live-traces"
_PENDING_MESSAGE_KEY = "demo-pending-message"
_MODE_KEY = "demo-execution-mode"
_ACTIVE_MODE_KEY = "demo-active-execution-mode"

_LIVE_MODE = "Live web"
_OFFLINE_MODE = "Offline walkthrough"


def _run_async(operation: Awaitable[object]) -> object:
    return asyncio.run(operation)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _strip_streamlit_answer_title(value: str) -> str:
    """Hide the redundant top-level result title in the Streamlit chat UI."""

    lines = value.splitlines()
    if lines and lines[0].strip() in {"# Kết quả", "# Result"}:
        return "\n".join(lines[1:]).lstrip()
    return value


def _default_dependencies(mode: str) -> tuple[DemoRuntimeService, DemoJobManager]:
    """Create local dependencies lazily without doing network work."""

    if mode not in {_LIVE_MODE, _OFFLINE_MODE}:
        raise ValueError("unknown execution mode")
    if _ACTIVE_MODE_KEY not in st.session_state and _SERVICE_KEY in st.session_state:
        st.session_state[_ACTIVE_MODE_KEY] = mode
    if st.session_state.get(_ACTIVE_MODE_KEY) != mode:
        root_name = "live" if mode == _LIVE_MODE else "demo"
        storage_root = Path.cwd() / ".mini-deerflow" / root_name
        backend = (
            LiveDemoBackend(storage_root)
            if mode == _LIVE_MODE
            else OfflineDemoBackend(storage_root)
        )
        st.session_state[_SERVICE_KEY] = DemoRuntimeService(backend)
        st.session_state[_ACTIVE_MODE_KEY] = mode
        for key in (
            _CONVERSATIONS_KEY,
            _SELECTED_THREAD_KEY,
            _SESSION_KEY,
            _HANDLED_JOB_KEY,
            _LIVE_TRACES_KEY,
        ):
            st.session_state.pop(key, None)
    if _MANAGER_KEY not in st.session_state:
        st.session_state[_MANAGER_KEY] = DemoJobManager(
            error_mapper=lambda error, job_id: map_demo_error(error, job_id)
        )
    return st.session_state[_SERVICE_KEY], st.session_state[_MANAGER_KEY]


def _refresh_conversations(
    service: DemoRuntimeService,
) -> tuple[ConversationSummaryView, ...]:
    records = _run_async(service.list_conversations())
    assert isinstance(records, tuple)
    st.session_state[_CONVERSATIONS_KEY] = records
    return records


def _load_selected_conversation(
    service: DemoRuntimeService,
) -> ChatSessionView | None:
    thread_id = st.session_state.get(_SELECTED_THREAD_KEY)
    if not isinstance(thread_id, str):
        st.session_state[_SESSION_KEY] = None
        return None
    session = _run_async(service.load_conversation(thread_id))
    assert isinstance(session, ChatSessionView)
    st.session_state[_SESSION_KEY] = session
    return session


def _submit(
    manager: DemoJobManager,
    *,
    operation: str,
    thread_id: str,
    turn_id: str | None,
    task: Callable[[TraceSink], DemoRunView],
) -> None:
    st.session_state[_ERROR_KEY] = None
    st.session_state[_LIVE_TRACES_KEY] = ()
    manager.submit(
        operation=operation,
        thread_id=thread_id,
        turn_id=turn_id,
        task=task,
    )


def _submit_message(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    message: str,
) -> None:
    selected = st.session_state.get(_SELECTED_THREAD_KEY)
    st.session_state[_PENDING_MESSAGE_KEY] = message
    turn_id = _new_id("turn")
    if isinstance(selected, str):
        follow_up = ContinueDemoCommand(
            thread_id=selected,
            turn_id=turn_id,
            user_message=message,
        )

        def task(trace_sink: TraceSink) -> DemoRunView:
            return cast(
                DemoRunView,
                _run_async(service.continue_thread(follow_up, trace_sink=trace_sink)),
            )

        operation = "continue"
        thread_id = selected
    else:
        thread_id = _new_id("chat")
        first_turn = RunDemoCommand(
            thread_id=thread_id,
            turn_id=turn_id,
            goal=message,
        )

        def task(trace_sink: TraceSink) -> DemoRunView:
            return cast(
                DemoRunView,
                _run_async(service.run(first_turn, trace_sink=trace_sink)),
            )

        operation = "run"
        st.session_state[_SELECTED_THREAD_KEY] = thread_id

    _submit(
        manager,
        operation=operation,
        thread_id=thread_id,
        turn_id=turn_id,
        task=task,
    )


def _submit_resume(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    thread_id: str,
) -> None:
    command = ResumeDemoCommand(thread_id)

    def task(trace_sink: TraceSink) -> DemoRunView:
        return cast(
            DemoRunView,
            _run_async(service.resume(command, trace_sink=trace_sink)),
        )

    _submit(
        manager,
        operation="resume",
        thread_id=thread_id,
        turn_id=None,
        task=task,
    )


def _consume_terminal_snapshot(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    snapshot: JobSnapshot,
) -> bool:
    if snapshot.active or st.session_state.get(_HANDLED_JOB_KEY) == snapshot.job_id:
        return False
    st.session_state[_LIVE_TRACES_KEY] = manager.trace_history()
    st.session_state[_PENDING_MESSAGE_KEY] = None
    st.session_state[_ERROR_KEY] = (
        snapshot.error_message if snapshot.state is JobState.FAILED else None
    )
    try:
        _refresh_conversations(service)
        _load_selected_conversation(service)
    except Exception as error:  # noqa: BLE001 - safe UI boundary
        st.session_state[_ERROR_KEY] = map_demo_error(error, snapshot.job_id)
    st.session_state[_HANDLED_JOB_KEY] = snapshot.job_id
    return True


@st.fragment(run_every=0.5, key="demo-active-job-poll")
def _poll_active_job(service: DemoRuntimeService, manager: DemoJobManager) -> None:
    """Show live progress and refresh the durable transcript when work ends."""

    snapshot = manager.snapshot()
    if snapshot is None:
        return
    if snapshot.active:
        events = manager.trace_history()
        with st.status(
            f"{snapshot.operation.title()} · {snapshot.state.value}",
            state="running",
            expanded=bool(events),
        ):
            st.caption(f"Thread: {snapshot.thread_id}")
        graph_provider = getattr(manager, "execution_graph", None)
        live_graph = graph_provider() if callable(graph_provider) else None
        if live_graph is not None and live_graph.nodes:
            with st.expander("Live agent graph", expanded=True):
                render_agent_graph(
                    live_graph,
                    key_prefix=f"live-{snapshot.job_id}-graph",
                )
        if events:
            with st.expander("Live execution trace", expanded=False):
                render_trace(events, key_prefix=f"live-{snapshot.job_id}")
        return
    _consume_terminal_snapshot(service, manager, snapshot)
    st.rerun(scope="app")


def _render_sidebar(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    *,
    active: bool,
) -> None:
    mode = st.session_state[_ACTIVE_MODE_KEY]
    st.sidebar.title("Mini DeerFlow")
    if mode == _LIVE_MODE:
        st.sidebar.badge("LIVE mode · configured runtime", color="blue")
        st.sidebar.caption(
            "Live mode uses GLM 5.3 for agent reasoning and Wikipedia "
            "for bounded research evidence. Readiness is confirmed by "
            "successful model and Wikipedia-tool outcomes."
        )
    else:
        st.sidebar.badge("OFFLINE · deterministic", color="green")
        st.sidebar.caption("No network calls; intended only for a walkthrough.")

    if st.sidebar.button(
        "New chat",
        type="primary",
        key="demo-new-chat",
        disabled=active,
        width="stretch",
    ):
        st.session_state[_SELECTED_THREAD_KEY] = None
        st.session_state[_SESSION_KEY] = None
        st.session_state[_ERROR_KEY] = None
        st.rerun()

    if st.sidebar.button(
        "Refresh",
        key="demo-refresh-conversations",
        disabled=active,
        width="stretch",
    ):
        try:
            _refresh_conversations(service)
            _load_selected_conversation(service)
            st.session_state[_ERROR_KEY] = None
        except Exception as error:  # noqa: BLE001 - safe UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
        st.rerun()

    st.sidebar.subheader("Chats")
    conversations = tuple(st.session_state.get(_CONVERSATIONS_KEY, ()))
    selected = st.session_state.get(_SELECTED_THREAD_KEY)
    if not conversations:
        st.sidebar.caption("No conversations yet.")
    for record in conversations:
        label = record.title
        if record.thread_id == selected:
            label = f"● {label}"
        if st.sidebar.button(
            label,
            key=f"conversation-{record.thread_id}",
            disabled=active,
            width="stretch",
            help=record.thread_id,
        ):
            st.session_state[_SELECTED_THREAD_KEY] = record.thread_id
            try:
                _load_selected_conversation(service)
                st.session_state[_ERROR_KEY] = None
            except Exception as error:  # noqa: BLE001 - safe UI boundary
                st.session_state[_ERROR_KEY] = map_demo_error(error)
            st.rerun()

    st.sidebar.divider()
    st.sidebar.caption(
        "Local learning MVP. Each turn has isolated evidence, trace, artifact, "
        "budget, and checkpoint state."
    )


def _render_turn(turn) -> None:
    with st.chat_message("user"):
        st.text(turn.user_message)
    with st.chat_message("assistant"):
        if turn.status == "completed" and turn.assistant_message:
            allowed_urls = () if turn.run is None else turn.run.accepted_citations
            answer_markdown = sanitize_answer_markdown(
                turn.assistant_message,
                allowed_urls=allowed_urls,
            )
            st.markdown(
                _strip_streamlit_answer_title(answer_markdown),
                unsafe_allow_html=False,
            )
        elif turn.status == "completed":
            st.info("Turn completed without a displayable answer.")
        elif turn.status == "interrupted":
            st.warning("This turn was interrupted and can be resumed.")
        elif turn.status == "failed":
            st.error(turn.safe_error or "This turn failed safely.")
        else:
            st.info("This turn is still running.")

        if turn.run is not None:
            with st.expander(
                "Xem quá trình Agent",
                expanded=False,
            ):
                render_demo_tabs(
                    turn.run,
                    key_prefix=f"turn-{turn.turn_id}",
                )


def _render_chat(
    service: DemoRuntimeService,
    manager: DemoJobManager,
    *,
    active: bool,
) -> None:
    session = st.session_state.get(_SESSION_KEY)
    if isinstance(session, ChatSessionView):
        st.header(session.conversation.title)
        conversation = session.conversation
        st.caption(
            f"{conversation.thread_id} · session tool calls "
            f"{conversation.session_tool_calls}/{conversation.session_tool_call_limit}"
        )
        for turn in session.turns:
            _render_turn(turn)
    else:
        st.header("New research chat")
        st.caption(
            "Ask a research question. Follow-up messages keep the same public "
            "thread while executing as isolated turns."
        )
        st.info("Start with a specific question that can be verified on the web.")

    snapshot = manager.snapshot()
    has_resumable_turn = (
        isinstance(session, ChatSessionView)
        and bool(session.turns)
        and session.turns[-1].status in {"running", "interrupted"}
    )
    if active:
        pending = st.session_state.get(_PENDING_MESSAGE_KEY)
        if isinstance(pending, str):
            with st.chat_message("user"):
                st.text(pending)
            with st.chat_message("assistant"):
                st.info("Researching with bounded tools…")
        _poll_active_job(service, manager)
    elif has_resumable_turn:
        if st.button(
            "Resume active turn",
            key="demo-resume-turn",
            width="stretch",
        ):
            try:
                _submit_resume(
                    service,
                    manager,
                    session.conversation.thread_id,
                )
            except Exception as error:  # noqa: BLE001 - safe UI boundary
                st.session_state[_ERROR_KEY] = map_demo_error(error)
            st.rerun()
    elif snapshot is not None and snapshot.state is JobState.FAILED:
        traces = tuple(st.session_state.get(_LIVE_TRACES_KEY, ()))
        if traces:
            with st.expander("Failed turn trace"):
                render_trace(traces, key_prefix=f"failed-{snapshot.job_id}")

    prompt = st.chat_input(
        "Ask a follow-up or start a new research task",
        key="demo-chat-input",
        disabled=active or has_resumable_turn,
        submit_mode="disable",
        max_chars=1_000,
    )
    if prompt:
        try:
            _submit_message(service, manager, prompt)
        except Exception as error:  # noqa: BLE001 - safe UI boundary
            st.session_state[_ERROR_KEY] = map_demo_error(error)
        st.rerun()


def render_page(service: DemoRuntimeService, manager: DemoJobManager) -> None:
    """Render the chat from durable conversation projections."""

    if _CONVERSATIONS_KEY not in st.session_state:
        try:
            conversations = _refresh_conversations(service)
            if conversations and _SELECTED_THREAD_KEY not in st.session_state:
                st.session_state[_SELECTED_THREAD_KEY] = conversations[0].thread_id
            _load_selected_conversation(service)
        except Exception as error:  # noqa: BLE001 - safe UI boundary
            st.session_state[_CONVERSATIONS_KEY] = ()
            st.session_state[_ERROR_KEY] = map_demo_error(error)

    snapshot = manager.snapshot()
    active = snapshot is not None and snapshot.active
    if snapshot is not None and not active:
        _consume_terminal_snapshot(service, manager, snapshot)

    _render_sidebar(service, manager, active=active)
    error_message = st.session_state.get(_ERROR_KEY)
    if error_message:
        st.error(error_message)
    _render_chat(service, manager, active=active)


def main() -> None:
    st.set_page_config(page_title="Mini DeerFlow Chat", page_icon="🦌", layout="wide")
    manager = st.session_state.get(_MANAGER_KEY)
    mode_change_disabled = isinstance(manager, DemoJobManager) and manager.is_active
    mode = st.sidebar.selectbox(
        "Execution mode",
        options=(_LIVE_MODE, _OFFLINE_MODE),
        key=_MODE_KEY,
        disabled=mode_change_disabled,
        help="Live mode uses your configured APIs; offline mode never uses the web.",
    )
    service, manager = _default_dependencies(mode)
    render_page(service, manager)


if __name__ == "__main__":
    main()
