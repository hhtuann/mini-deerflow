import { useMemo, useState } from 'react';
import { ReactFlow, Background, Controls, MarkerType, Position } from '@xyflow/react';
import dagre from '@dagrejs/dagre';
import '@xyflow/react/dist/style.css';
import type { GraphNode, GraphProjection, GraphStatus, RunView, TraceEvent } from '../types';
import { Empty, Fields, Status } from './UI';

const statuses: GraphStatus[] = ['pending', 'running', 'completed', 'failed', 'cancelled', 'skipped'];
const kinds: { kind: GraphNode['kind']; label: string }[] = [
  { kind: 'run', label: 'Run' }, { kind: 'workflow_node', label: 'Workflow node' },
  { kind: 'delegation', label: 'Delegation' }, { kind: 'branch', label: 'Branch' },
  { kind: 'tool', label: 'Tool' }, { kind: 'fan_in', label: 'Fan-in' },
];

export default function ExecutionGraph({ graph, traces = [], run, isWorkflow = false }: { graph: GraphProjection; traces?: TraceEvent[]; run?: RunView | null; isWorkflow?: boolean }) {
  const [selected, setSelected] = useState('');
  const node = graph.nodes.find((item) => item.id === selected) ?? graph.nodes[0];
  // Exact run+node correlation: never attach an unrelated resume/child trace.
  // Tool/branch payloads remain backend-redacted and are not reconstructed.
  const events = !isWorkflow && node?.kind === 'workflow_node'
    ? traces.filter((event) => event.node != null && node.id === `workflow:${event.run_id}:${event.node}`)
    : [];
  const context = [...events].reverse().find((event) => event.context_estimated_tokens != null || event.context_payload_chars != null);
  const latest = events.at(-1);
  const invocations = !isWorkflow ? traces.filter((event) => event.kind === 'model' && event.node != null && node?.id === `workflow:${event.run_id}:${event.node}`) : [];
  const usage = invocations.at(-1);
  const { nodes, edges } = useMemo(() => {
    const layout = new dagre.graphlib.Graph().setGraph({ rankdir: 'TB', nodesep: 40, ranksep: 65 }).setDefaultEdgeLabel(() => ({}));
    graph.nodes.forEach((item) => layout.setNode(item.id, { width: 180, height: 75 }));
    graph.edges.forEach((edge) => layout.setEdge(edge.source, edge.target));
    dagre.layout(layout);
    return {
      nodes: graph.nodes.map((item) => ({ id: item.id, position: { x: layout.node(item.id).x - 90, y: layout.node(item.id).y - 37.5 },
        data: { label: <><strong>{item.label}</strong><small>{item.kind.replaceAll('_', ' ')} · {item.status}</small></> },
        className: `graph-node status-${item.status}`, sourcePosition: Position.Bottom, targetPosition: Position.Top,
        selected: item.id === node?.id, ariaLabel: `${item.label}, ${item.status}`, })),
      edges: graph.edges.map((edge, index) => ({ id: `${edge.source}-${edge.target}-${edge.relation}-${index}`, source: edge.source, target: edge.target,
        label: edge.label ?? edge.relation.replaceAll('_', ' '), markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: edge.selected ? 'var(--primary)' : 'var(--muted)', strokeWidth: edge.selected ? 2 : 1 }, })),
    };
  }, [graph, node?.id]);
  return <>
    <dl className="graph-metrics" aria-label="Graph metrics">{([
      ['Nodes', graph.metrics.nodes], ['Running', graph.metrics.running], ['Completed', graph.metrics.completed],
      ['Failed', graph.metrics.failed], ['Tool calls', graph.metrics.tool_calls],
      ['Sub-agents', graph.metrics.sub_agents], ['Evidence', graph.metrics.evidence],
    ] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <section className="graph-legend" aria-label="Graph legend">
      <h3>Status legend</h3><ul aria-label="Node statuses">{statuses.map((status) => <li key={status}><Status value={status} /></li>)}</ul>
      <h3>Kind legend</h3><ul aria-label="Node kinds">{kinds.map(({ kind, label }) => <li key={kind}>{label}</li>)}</ul>
      <p className="caption">Each node names its kind and status. Legend entries describe possible categories; counts above reflect this projection.</p>
    </section>
    {graph.nodes.length ? <>
      <div className="graph-canvas" aria-label="Read-only execution graph">
        <ReactFlow nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} deleteKeyCode={null}
          onNodeClick={(_, item) => setSelected(item.id)}
          fitView minZoom={0.1} maxZoom={2} proOptions={{ hideAttribution: false }}>
          <Background /><Controls showInteractive={false} fitViewOptions={{ padding: 0.15 }} />
        </ReactFlow>
      </div>
      <p className="caption">Pan and zoom the canvas, or select a node below using your keyboard.</p>
      <label>Inspect node<select value={node?.id ?? ''} onChange={(event) => setSelected(event.target.value)}>{graph.nodes.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.status}</option>)}</select></label>
      {node && <section className="detail-card node-inspection" aria-label="Node details">
        <h3>{node.label}</h3><Status value={node.status} />
        {isWorkflow ? <p className="caption">Workflow topology only. No real input, prompt, output or usage exists for a possible node. Switch to Current run.</p> : <>
          <h4>Input &amp; context</h4>
          {node.kind === 'workflow_node' && node.label === 'Planner' && run?.goal
            ? <p className="run-goal"><strong>User goal (not LLM prompt):</strong> {run?.goal}</p>
            : <p className="caption">Full node inputs are not captured by the safe execution trace.</p>}
          {context && <Fields values={{ 'Projected context chars': context.context_payload_chars,
            'Context budget chars': context.context_budget_chars, 'Retained context items': context.context_retained_items,
            'Truncated context items': context.context_truncated_items, 'Omitted context items': context.context_omitted_items,
            'Skill': context.active_skill_id }} />}
          <h4>Prompt</h4>
          <p className="caption">Exact system/developer/model prompt: not captured. Raw prompts, secrets and private model reasoning are not exposed by this API.</p>
          <h4>Execution &amp; outcome</h4>
          <Fields values={{ Kind: node.kind, Parent: node.parent_id, Started: node.started_at,
            Completed: node.completed_at, 'Duration (ms)': node.duration_ms,
            'Last trace outcome': latest?.outcome, 'Error category': latest?.error_category, 'Error code': latest?.error_code }} />
          <p>{node.safe_summary ?? 'Raw model/tool response not recorded. See the safe structured fields below for observable results.'}</p>
          {node.details.map((field, index) => <Fields key={index} values={{ [field.label]: field.value }} />)}
          {node.kind === 'workflow_node' && node.label === 'Planner' && !!run?.plan.length && <details>
            <summary>Structured plan result (turn-level, not raw model output)</summary>
            <ol>{run.plan.map((step) => <li key={step.step_number}><strong>{step.title}</strong> — {step.objective} ({step.status})</li>)}</ol>
          </details>}
          {node.kind === 'workflow_node' && node.label === 'Review' && !!run?.reviews.length && <details>
            <summary>Review outcomes (all reviews in this turn)</summary>
            <ul>{run.reviews.map((review) => <li key={review.review_number}>Review {review.review_number}: {review.route} · {review.findings.length} finding(s)</li>)}</ul>
          </details>}
          {node.kind === 'workflow_node' && node.label === 'Synthesis' && !!run?.artifact.finding_summaries.length && <details>
            <summary>Saved report findings (turn-level; not raw model completion)</summary>
            <ul>{run.artifact.finding_summaries.slice(0, 5).map((finding, index) => <li key={index}>{finding}</li>)}</ul>
          </details>}
          <h4>Token usage &amp; cost</h4>
          {usage ? <>
            <p className="caption">Provider-reported usage for the latest model invocation of this node (attempt {usage.model_attempt} of {invocations.length}).</p>
            <Fields values={{ Model: usage.model_name, 'Prompt schema': usage.model_schema,
              'Provider input tokens': usage.usage_reported ? usage.model_input_tokens : 'Not reported',
              'Provider output tokens': usage.usage_reported ? usage.model_output_tokens : 'Not reported',
              'Provider total tokens': usage.usage_reported ? usage.model_total_tokens : 'Not reported',
              'Cached tokens': usage.usage_reported ? (usage.model_cached_tokens ?? 'Not reported') : 'Not reported',
              'Invocation latency (ms)': usage.model_duration_ms,
              'Provider cost': 'Not reported' }} />
            {invocations.length > 1 && <details><summary>All model invocations ({invocations.length})</summary>
              <ul>{invocations.map((item, index) => <li key={index}>Attempt {item.model_attempt}: {item.usage_reported ? `${item.model_input_tokens ?? '—'} in / ${item.model_output_tokens ?? '—'} out / ${item.model_total_tokens ?? '—'} total` : 'usage not reported'} · {item.model_duration_ms ?? '—'} ms</li>)}</ul>
            </details>}
          </> : <Fields values={{ 'Actual prompt tokens': 'Not reported', 'Actual completion tokens': 'Not reported',
            'Actual total tokens': 'Not reported', 'Provider cost': 'Not reported' }} />}
          {context?.context_estimated_tokens != null && <Fields values={{ 'Context estimate (chars / 4; NOT billed tokens)': context.context_estimated_tokens }} />}
          <p className="caption">Only provider-reported usage could count as real tokens or cost. No estimates are presented as measurements.</p>
        </>}
        <details><summary>Technical IDs</summary><Fields values={{ 'Node ID': node.id }} />{node.technical_details.map((field, index) => <Fields key={index} values={{ [field.label]: field.value }} />)}</details>
      </section>}
    </> : <Empty>No structured execution events are available yet.</Empty>}
  </>;
}
