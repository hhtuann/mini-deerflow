"""Safe graph projections derived from structured execution events.

The projection is intentionally independent from Streamlit.  Renderers receive a
closed, immutable graph view instead of understanding the execution-event taxonomy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from mini_deerflow.tracing import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
)

GraphNodeKind = Literal[
    "workflow_node", "delegation", "branch", "tool", "run", "fan_in"
]
GraphStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "skipped",
]


@dataclass(frozen=True, slots=True)
class GraphField:
    """One allowlisted scalar shown by the node inspector."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ExecutionGraphNode:
    """A normalized, UI-ready execution entity."""

    id: str
    kind: GraphNodeKind
    label: str
    status: GraphStatus
    parent_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    safe_summary: str | None = None
    details: tuple[GraphField, ...] = ()
    technical_details: tuple[GraphField, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionGraphEdge:
    """A normalized relationship between two projected entities."""

    source: str
    target: str
    relation: str
    label: str | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionGraphMetrics:
    nodes: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    tool_calls: int = 0
    sub_agents: int = 0
    evidence: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionGraphProjection:
    """The sole graph shape consumed by the demo UI."""

    nodes: tuple[ExecutionGraphNode, ...] = ()
    edges: tuple[ExecutionGraphEdge, ...] = ()
    metrics: ExecutionGraphMetrics = ExecutionGraphMetrics()


_WORKFLOW_LABELS: Mapping[str, str] = {
    "__start__": "User goal",
    "planner": "Planner",
    "decide_action": "Action selection",
    "execute_tool": "Tool execution",
    "complete_step": "Complete step",
    "budget_exhausted": "Budget exhausted",
    "review": "Review",
    "replan": "Replan",
    "synthesize": "Synthesis",
    "__end__": "Complete",
}

_STATIC_EDGE_SPECS: tuple[tuple[str, str, str | None], ...] = (
    ("__start__", "planner", None),
    ("planner", "decide_action", None),
    ("decide_action", "execute_tool", "tool call"),
    ("decide_action", "complete_step", "complete step"),
    ("decide_action", "budget_exhausted", "budget exhausted"),
    ("execute_tool", "decide_action", "next action"),
    ("complete_step", "review", None),
    ("review", "decide_action", "continue"),
    ("review", "replan", "replan"),
    ("review", "synthesize", "finish"),
    ("replan", "decide_action", None),
    ("budget_exhausted", "decide_action", "continue"),
    ("budget_exhausted", "review", "review"),
    ("budget_exhausted", "synthesize", "finish"),
    ("synthesize", "__end__", None),
)

_TERMINAL_EVENT_TYPES = frozenset(
    {
        ExecutionEventType.RUN_COMPLETED,
        ExecutionEventType.RUN_FAILED,
        ExecutionEventType.NODE_COMPLETED,
        ExecutionEventType.NODE_FAILED,
        ExecutionEventType.TOOL_COMPLETED,
        ExecutionEventType.TOOL_FAILED,
        ExecutionEventType.DELEGATION_COMPLETED,
        ExecutionEventType.BRANCH_COMPLETED,
        ExecutionEventType.BRANCH_FAILED,
        ExecutionEventType.FAN_IN_COMPLETED,
    }
)


@dataclass(slots=True)
class _Entity:
    id: str
    kind: GraphNodeKind
    label: str
    parent_id: str | None
    events: list[ExecutionEvent]

    @property
    def first_sequence(self) -> int:
        return min(event.sequence for event in self.events)

    @property
    def last_sequence(self) -> int:
        return max(event.sequence for event in self.events)


def project_static_workflow() -> ExecutionGraphProjection:
    """Return the reviewer-enabled canonical topology from ``agent_workflow.py``."""

    node_ids = tuple(_WORKFLOW_LABELS)
    nodes = tuple(
        ExecutionGraphNode(
            id=node_id,
            kind="workflow_node",
            label=_WORKFLOW_LABELS[node_id],
            status="pending",
            safe_summary=(
                "Possible workflow node; this view does not represent a run."
            ),
            technical_details=(GraphField("Node ID", node_id),),
        )
        for node_id in node_ids
    )
    edges = tuple(
        ExecutionGraphEdge(
            source=source,
            target=target,
            relation="possible_route" if label else "workflow",
            label=label,
        )
        for source, target, label in _STATIC_EDGE_SPECS
    )
    return ExecutionGraphProjection(
        nodes=nodes,
        edges=edges,
        metrics=_metrics(nodes, tool_calls=0, sub_agents=0, evidence=0),
    )


def project_execution_graph(
    events: Iterable[ExecutionEvent],
) -> ExecutionGraphProjection:
    """Reconstruct a deterministic graph from safe structured events.

    Input position is ignored.  Unique event IDs remove duplicate delivery, and
    root-run sequence numbers determine lifecycle precedence and relationships.
    """

    ordered = _ordered_unique_events(events)
    if not ordered:
        return ExecutionGraphProjection()

    entities: dict[str, _Entity] = {}
    workflow_by_run: dict[str, dict[str, _Entity]] = defaultdict(dict)
    tools_by_call: dict[str, _Entity] = {}
    delegations: dict[tuple[str, str], _Entity] = {}
    branches_by_run: dict[str, _Entity] = {}
    fan_ins: dict[tuple[str, str], _Entity] = {}

    for event in ordered:
        if event.event_type in {
            ExecutionEventType.RUN_STARTED,
            ExecutionEventType.RUN_COMPLETED,
            ExecutionEventType.RUN_FAILED,
        }:
            entity = _entity(
                entities,
                event.run_id,
                kind="run",
                label=_run_label(event),
                parent_id=event.parent_run_id,
            )
            entity.events.append(event)
        elif (
            event.event_type
            in {
                ExecutionEventType.NODE_STARTED,
                ExecutionEventType.NODE_COMPLETED,
                ExecutionEventType.NODE_FAILED,
            }
            and event.node_id is not None
        ):
            graph_id = _workflow_graph_id(event.run_id, event.node_id)
            entity = _entity(
                entities,
                graph_id,
                kind="workflow_node",
                label=_workflow_label(event.node_id),
                parent_id=event.run_id,
            )
            entity.events.append(event)
            workflow_by_run[event.run_id][event.node_id] = entity
        elif (
            event.event_type
            in {
                ExecutionEventType.TOOL_STARTED,
                ExecutionEventType.TOOL_COMPLETED,
                ExecutionEventType.TOOL_FAILED,
                ExecutionEventType.EVIDENCE_PRODUCED,
            }
            and event.tool_call_id is not None
        ):
            graph_id = _tool_graph_id(event.root_run_id, event.tool_call_id)
            entity = _entity(
                entities,
                graph_id,
                kind="tool",
                label=_tool_label(event.tool_name),
                parent_id=None,
            )
            entity.events.append(event)
            tools_by_call[event.tool_call_id] = entity
        elif (
            event.event_type
            in {
                ExecutionEventType.DELEGATION_STARTED,
                ExecutionEventType.DELEGATION_COMPLETED,
            }
            and event.delegation_id is not None
        ):
            key = (event.root_run_id, event.delegation_id)
            graph_id = _delegation_graph_id(*key)
            entity = _entity(
                entities,
                graph_id,
                kind="delegation",
                label="Research delegation",
                parent_id=None,
            )
            entity.events.append(event)
            delegations[key] = entity
        elif (
            event.event_type
            in {
                ExecutionEventType.BRANCH_STARTED,
                ExecutionEventType.BRANCH_COMPLETED,
                ExecutionEventType.BRANCH_FAILED,
            }
            and event.branch_id is not None
        ):
            graph_id = _branch_graph_id(event.root_run_id, event.run_id)
            delegation_parent = (
                None
                if event.delegation_id is None
                else _delegation_graph_id(event.root_run_id, event.delegation_id)
            )
            entity = _entity(
                entities,
                graph_id,
                kind="branch",
                label=f"Sub-agent · {event.branch_id}",
                parent_id=delegation_parent,
            )
            entity.events.append(event)
            branches_by_run[event.run_id] = entity
        elif (
            event.event_type is ExecutionEventType.FAN_IN_COMPLETED
            and event.delegation_id is not None
        ):
            key = (event.root_run_id, event.delegation_id)
            graph_id = _fan_in_graph_id(*key)
            entity = _entity(
                entities,
                graph_id,
                kind="fan_in",
                label="Evidence fan-in",
                parent_id=_delegation_graph_id(*key),
            )
            entity.events.append(event)
            fan_ins[key] = entity

    edges: list[ExecutionGraphEdge] = []
    _attach_workflow_edges(ordered, workflow_by_run, entities, edges)
    _attach_tool_edges(ordered, branches_by_run, tools_by_call, edges)
    _attach_delegation_edges(
        ordered,
        tools_by_call,
        delegations,
        branches_by_run,
        fan_ins,
        edges,
    )
    _attach_selected_routes(ordered, workflow_by_run, edges)

    nodes = tuple(
        _project_entity(entity, ordered)
        for entity in sorted(
            entities.values(),
            key=lambda item: (item.first_sequence, item.kind, item.id),
        )
    )
    projected_edges = _deduplicate_edges(edges)
    tool_calls = len(tools_by_call)
    sub_agents = len(branches_by_run)
    evidence = _evidence_total(ordered)
    return ExecutionGraphProjection(
        nodes=nodes,
        edges=projected_edges,
        metrics=_metrics(
            nodes,
            tool_calls=tool_calls,
            sub_agents=sub_agents,
            evidence=evidence,
        ),
    )


def _ordered_unique_events(
    events: Iterable[ExecutionEvent],
) -> tuple[ExecutionEvent, ...]:
    unique: dict[str, ExecutionEvent] = {}
    for event in events:
        if not isinstance(event, ExecutionEvent):
            raise TypeError("events must contain ExecutionEvent values")
        unique.setdefault(event.event_id, event)
    return tuple(
        sorted(
            unique.values(),
            key=lambda event: (
                event.root_run_id,
                event.sequence,
                event.timestamp,
                event.event_id,
            ),
        )
    )


def _entity(
    entities: dict[str, _Entity],
    graph_id: str,
    *,
    kind: GraphNodeKind,
    label: str,
    parent_id: str | None,
) -> _Entity:
    existing = entities.get(graph_id)
    if existing is not None:
        return existing
    created = _Entity(
        id=graph_id,
        kind=kind,
        label=label,
        parent_id=parent_id,
        events=[],
    )
    entities[graph_id] = created
    return created


def _attach_workflow_edges(
    events: Sequence[ExecutionEvent],
    workflow_by_run: Mapping[str, Mapping[str, _Entity]],
    entities: Mapping[str, _Entity],
    edges: list[ExecutionGraphEdge],
) -> None:
    starts_by_run: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        if event.event_type is ExecutionEventType.NODE_STARTED:
            starts_by_run[event.run_id].append(event)
    for run_id, starts in starts_by_run.items():
        previous: str = run_id if run_id in entities else ""
        for event in sorted(starts, key=lambda item: item.sequence):
            if event.node_id is None:
                continue
            current = _workflow_graph_id(run_id, event.node_id)
            if previous and previous != current:
                edges.append(
                    ExecutionGraphEdge(previous, current, "next", selected=True)
                )
            previous = current


def _attach_tool_edges(
    events: Sequence[ExecutionEvent],
    branches_by_run: Mapping[str, _Entity],
    tools_by_call: Mapping[str, _Entity],
    edges: list[ExecutionGraphEdge],
) -> None:
    node_starts: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        if event.event_type is ExecutionEventType.NODE_STARTED:
            node_starts[event.run_id].append(event)
    tool_starts = [
        event
        for event in events
        if event.event_type is ExecutionEventType.TOOL_STARTED
        and event.tool_call_id is not None
    ]
    for event in tool_starts:
        tool = tools_by_call[event.tool_call_id]
        branch = branches_by_run.get(event.run_id)
        if branch is not None:
            tool.parent_id = branch.id
            edges.append(
                ExecutionGraphEdge(
                    branch.id,
                    tool.id,
                    "calls",
                    label=event.tool_name,
                    selected=True,
                )
            )
            continue
        candidates = [
            item
            for item in node_starts.get(event.run_id, ())
            if item.sequence <= event.sequence and item.node_id is not None
        ]
        if candidates:
            owner = max(candidates, key=lambda item: item.sequence)
            owner_id = _workflow_graph_id(event.run_id, owner.node_id or "")
        else:
            owner_id = event.run_id
        if owner_id:
            tool.parent_id = owner_id
            edges.append(
                ExecutionGraphEdge(
                    owner_id,
                    tool.id,
                    "calls",
                    label=event.tool_name,
                    selected=True,
                )
            )


def _attach_delegation_edges(
    events: Sequence[ExecutionEvent],
    tools_by_call: Mapping[str, _Entity],
    delegations: Mapping[tuple[str, str], _Entity],
    branches_by_run: Mapping[str, _Entity],
    fan_ins: Mapping[tuple[str, str], _Entity],
    edges: list[ExecutionGraphEdge],
) -> None:
    parent_tools = [
        entity
        for entity in tools_by_call.values()
        if any(event.tool_name == "delegate_research" for event in entity.events)
    ]
    for key, delegation in delegations.items():
        parent_tool = _containing_entity(parent_tools, delegation.first_sequence)
        if parent_tool is not None:
            delegation.parent_id = parent_tool.id
            edges.append(
                ExecutionGraphEdge(
                    parent_tool.id,
                    delegation.id,
                    "delegates",
                    selected=True,
                )
            )

        matching_branches = [
            branch
            for branch in branches_by_run.values()
            if any(
                event.root_run_id == key[0] and event.delegation_id == key[1]
                for event in branch.events
            )
        ]
        for branch in matching_branches:
            edges.append(
                ExecutionGraphEdge(
                    delegation.id,
                    branch.id,
                    "spawns",
                    label="sub-agent",
                    selected=True,
                )
            )

        fan_in = fan_ins.get(key)
        if fan_in is None:
            continue
        if matching_branches:
            for branch in matching_branches:
                edges.append(
                    ExecutionGraphEdge(
                        branch.id,
                        fan_in.id,
                        "returns_evidence",
                        label="fan-in",
                        selected=True,
                    )
                )
        else:
            edges.append(
                ExecutionGraphEdge(
                    delegation.id,
                    fan_in.id,
                    "fan_in",
                    selected=True,
                )
            )

        fan_event = min(fan_in.events, key=lambda item: item.sequence)
        next_nodes = [
            event
            for event in events
            if event.event_type is ExecutionEventType.NODE_STARTED
            and event.run_id == fan_event.run_id
            and event.sequence > fan_event.sequence
            and event.node_id is not None
        ]
        if next_nodes:
            next_event = min(next_nodes, key=lambda item: item.sequence)
            edges.append(
                ExecutionGraphEdge(
                    fan_in.id,
                    _workflow_graph_id(next_event.run_id, next_event.node_id or ""),
                    "parent_continues",
                    label="continue",
                    selected=True,
                )
            )


def _attach_selected_routes(
    events: Sequence[ExecutionEvent],
    workflow_by_run: Mapping[str, Mapping[str, _Entity]],
    edges: list[ExecutionGraphEdge],
) -> None:
    for event in events:
        if event.event_type is not ExecutionEventType.ROUTE_SELECTED:
            continue
        from_node = event.metadata.get("from_node")
        to_node = event.metadata.get("to_node")
        if not isinstance(from_node, str) or not isinstance(to_node, str):
            continue
        if from_node not in workflow_by_run.get(event.run_id, {}):
            continue
        if to_node not in workflow_by_run.get(event.run_id, {}):
            continue
        review_verdict = event.metadata.get("review_verdict")
        label = (
            event.route
            or (review_verdict if isinstance(review_verdict, str) else None)
            or to_node.replace("_", " ")
        )
        edges.append(
            ExecutionGraphEdge(
                _workflow_graph_id(event.run_id, from_node),
                _workflow_graph_id(event.run_id, to_node),
                "selected_route",
                label=label,
                selected=True,
            )
        )


def _containing_entity(
    entities: Sequence[_Entity],
    sequence: int,
) -> _Entity | None:
    candidates = [
        entity
        for entity in entities
        if entity.first_sequence <= sequence <= entity.last_sequence
    ]
    return max(candidates, key=lambda item: item.first_sequence, default=None)


def _project_entity(
    entity: _Entity,
    all_events: Sequence[ExecutionEvent],
) -> ExecutionGraphNode:
    ordered = sorted(entity.events, key=lambda event: event.sequence)
    latest = ordered[-1]
    starts = [event for event in ordered if event.status is ExecutionStatus.STARTED]
    terminals = [
        event for event in ordered if event.event_type in _TERMINAL_EVENT_TYPES
    ]
    duration_values = [
        event.duration_ms for event in terminals if event.duration_ms is not None
    ]
    started_at = min((event.timestamp for event in starts), default=None)
    completed_at = max((event.timestamp for event in terminals), default=None)
    status = _normalized_status(latest)
    details = _details_for_entity(entity, all_events)
    technical = _technical_details(entity, latest)
    return ExecutionGraphNode(
        id=entity.id,
        kind=entity.kind,
        label=entity.label,
        status=status,
        parent_id=entity.parent_id,
        started_at=_iso_timestamp(started_at),
        completed_at=_iso_timestamp(completed_at),
        duration_ms=sum(duration_values) if duration_values else None,
        safe_summary=_summary_for_entity(entity, latest),
        details=details,
        technical_details=technical,
    )


def _details_for_entity(
    entity: _Entity,
    all_events: Sequence[ExecutionEvent],
) -> tuple[GraphField, ...]:
    latest = max(entity.events, key=lambda event: event.sequence)
    fields: list[GraphField] = [GraphField("Type", _kind_label(entity.kind))]
    if entity.kind == "workflow_node":
        execution_count = sum(
            event.event_type is ExecutionEventType.NODE_STARTED
            for event in entity.events
        )
        fields.append(GraphField("Executions", str(execution_count)))
    if entity.kind == "branch":
        tool_count = len(
            {
                event.tool_call_id
                for event in all_events
                if event.run_id == latest.run_id and event.tool_call_id is not None
            }
        )
        evidence_count = sum(
            event.evidence_count or 0
            for event in all_events
            if event.run_id == latest.run_id
            and event.event_type is ExecutionEventType.EVIDENCE_PRODUCED
        )
        fields.extend(
            (
                GraphField("Tool calls", str(tool_count)),
                GraphField("Evidence produced", str(evidence_count)),
            )
        )
    elif entity.kind == "delegation":
        fields.extend(_delegation_fields(latest))
    elif entity.kind == "fan_in":
        fields.extend(_fan_in_fields(latest))
    elif entity.kind == "tool":
        fields.extend(_tool_fields(entity))
    elif entity.kind == "run":
        if latest.operation is not None:
            fields.append(GraphField("Operation", latest.operation))
    return tuple(fields)


def _delegation_fields(event: ExecutionEvent) -> tuple[GraphField, ...]:
    values = (
        ("Requested branches", event.branch_count),
        ("Successful branches", event.successful_branch_count),
        ("Failed branches", event.failed_branch_count),
        ("Cancelled branches", event.cancelled_branch_count),
        ("Reserved tool calls", event.reserved_tool_calls),
        ("Used tool calls", event.used_tool_calls),
        ("Charged tool calls", event.charged_tool_calls),
        ("Evidence", event.evidence_count),
    )
    return tuple(
        GraphField(label, str(value)) for label, value in values if value is not None
    )


def _fan_in_fields(event: ExecutionEvent) -> tuple[GraphField, ...]:
    values = (
        ("Successful branches", event.successful_branch_count),
        ("Failed branches", event.failed_branch_count),
        ("Cancelled branches", event.cancelled_branch_count),
        ("Evidence promoted", event.metadata.get("evidence_promoted")),
        ("Limitations produced", event.metadata.get("limitations_produced")),
    )
    return tuple(
        GraphField(label, str(value)) for label, value in values if value is not None
    )


def _tool_fields(entity: _Entity) -> tuple[GraphField, ...]:
    latest = max(entity.events, key=lambda event: event.sequence)
    evidence_count = sum(
        event.evidence_count or 0
        for event in entity.events
        if event.event_type is ExecutionEventType.EVIDENCE_PRODUCED
    )
    if evidence_count == 0:
        evidence_count = max(
            (event.evidence_count or 0 for event in entity.events),
            default=0,
        )
    fields = [GraphField("Tool", latest.tool_name or "Tool")]
    if latest.current_step is not None:
        fields.append(GraphField("Step", str(latest.current_step)))
    fields.append(GraphField("Evidence produced", str(evidence_count)))
    if latest.error_category is not None:
        fields.append(GraphField("Failure category", latest.error_category.value))
    if latest.error_code is not None:
        fields.append(GraphField("Failure code", latest.error_code.value))
    return tuple(fields)


def _technical_details(
    entity: _Entity,
    latest: ExecutionEvent,
) -> tuple[GraphField, ...]:
    values = [GraphField("Graph ID", entity.id), GraphField("Run ID", latest.run_id)]
    optional = (
        ("Parent graph ID", entity.parent_id),
        ("Root run ID", latest.root_run_id),
        ("Parent run ID", latest.parent_run_id),
        ("Thread ID", latest.thread_id),
        ("Turn ID", latest.turn_id),
        ("Node ID", latest.node_id),
        ("Delegation ID", latest.delegation_id),
        ("Branch ID", latest.branch_id),
        ("Tool call ID", latest.tool_call_id),
    )
    values.extend(
        GraphField(label, value) for label, value in optional if value is not None
    )
    return tuple(values)


def _summary_for_entity(
    entity: _Entity,
    latest: ExecutionEvent,
) -> str | None:
    if entity.kind == "delegation" and latest.status is ExecutionStatus.PARTIAL:
        succeeded = latest.successful_branch_count or 0
        failed = latest.failed_branch_count or 0
        cancelled = latest.cancelled_branch_count or 0
        return (
            f"Completed with {succeeded} successful, {failed} failed, "
            f"and {cancelled} cancelled branches."
        )
    if entity.kind == "branch" and latest.status in {
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }:
        category = latest.metadata.get("failure_category")
        return (
            f"Branch ended with {str(category).replace('_', ' ')}."
            if isinstance(category, str)
            else "Branch did not complete successfully."
        )
    if entity.kind == "fan_in":
        evidence = latest.metadata.get("evidence_promoted")
        if isinstance(evidence, int):
            return f"Promoted {evidence} evidence item(s) to the parent run."
    return None


def _normalized_status(event: ExecutionEvent) -> GraphStatus:
    if event.status is ExecutionStatus.STARTED:
        return "running"
    if event.status is ExecutionStatus.FAILED:
        return "failed"
    if event.status is ExecutionStatus.CANCELLED:
        return "cancelled"
    return "completed"


def _deduplicate_edges(
    edges: Iterable[ExecutionGraphEdge],
) -> tuple[ExecutionGraphEdge, ...]:
    unique: dict[tuple[str, str, str, str | None], ExecutionGraphEdge] = {}
    for edge in edges:
        if not edge.source or not edge.target or edge.source == edge.target:
            continue
        unique.setdefault(
            (edge.source, edge.target, edge.relation, edge.label),
            edge,
        )
    return tuple(unique.values())


def _evidence_total(events: Sequence[ExecutionEvent]) -> int:
    child_evidence = sum(
        event.evidence_count or 0
        for event in events
        if event.event_type is ExecutionEventType.EVIDENCE_PRODUCED
    )
    parent_evidence = sum(
        event.evidence_count or 0
        for event in events
        if event.event_type is ExecutionEventType.TOOL_COMPLETED
        and event.branch_id is None
        and event.tool_name != "delegate_research"
    )
    return child_evidence + parent_evidence


def _metrics(
    nodes: Sequence[ExecutionGraphNode],
    *,
    tool_calls: int,
    sub_agents: int,
    evidence: int,
) -> ExecutionGraphMetrics:
    return ExecutionGraphMetrics(
        nodes=len(nodes),
        running=sum(node.status == "running" for node in nodes),
        completed=sum(node.status == "completed" for node in nodes),
        failed=sum(node.status in {"failed", "cancelled"} for node in nodes),
        tool_calls=tool_calls,
        sub_agents=sub_agents,
        evidence=evidence,
    )


def _workflow_graph_id(run_id: str, node_id: str) -> str:
    return f"workflow:{run_id}:{node_id}"


def _delegation_graph_id(root_run_id: str, delegation_id: str) -> str:
    return f"delegation:{root_run_id}:{delegation_id}"


def _branch_graph_id(root_run_id: str, branch_run_id: str) -> str:
    return f"branch:{root_run_id}:{branch_run_id}"


def _tool_graph_id(root_run_id: str, tool_call_id: str) -> str:
    return f"tool:{root_run_id}:{tool_call_id}"


def _fan_in_graph_id(root_run_id: str, delegation_id: str) -> str:
    return f"fan-in:{root_run_id}:{delegation_id}"


def _workflow_label(node_id: str) -> str:
    return _WORKFLOW_LABELS.get(node_id, node_id.replace("_", " ").title())


def _tool_label(tool_name: str | None) -> str:
    if not tool_name:
        return "Tool call"
    return tool_name.replace("_", " ").capitalize()


def _run_label(event: ExecutionEvent) -> str:
    return "Main run" if event.parent_run_id is None else "Sub-agent run"


def _kind_label(kind: GraphNodeKind) -> str:
    return {
        "workflow_node": "Workflow node",
        "delegation": "Delegation",
        "branch": "Sub-agent branch",
        "tool": "Tool call",
        "run": "Run",
        "fan_in": "Evidence fan-in",
    }[kind]


def _iso_timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
