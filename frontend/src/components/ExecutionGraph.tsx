import { useMemo, useState } from 'react';
import { ReactFlow, Background, Controls, MarkerType, Position } from '@xyflow/react';
import dagre from '@dagrejs/dagre';
import '@xyflow/react/dist/style.css';
import type { GraphNode, GraphProjection, GraphStatus } from '../types';
import { Empty, Fields, Status } from './UI';

const statuses: GraphStatus[] = ['pending', 'running', 'completed', 'failed', 'cancelled', 'skipped'];
const kinds: { kind: GraphNode['kind']; label: string }[] = [
  { kind: 'run', label: 'Run' }, { kind: 'workflow_node', label: 'Workflow node' },
  { kind: 'delegation', label: 'Delegation' }, { kind: 'branch', label: 'Branch' },
  { kind: 'tool', label: 'Tool' }, { kind: 'fan_in', label: 'Fan-in' },
];

export default function ExecutionGraph({ graph }: { graph: GraphProjection }) {
  const [selected, setSelected] = useState('');
  const node = graph.nodes.find((item) => item.id === selected) ?? graph.nodes[0];
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
      {node && <section className="detail-card" aria-label="Node details"><h3>{node.label}</h3><Status value={node.status} />{node.safe_summary && <p>{node.safe_summary}</p>}
        <Fields values={{ Kind: node.kind, Parent: node.parent_id, Started: node.started_at, Completed: node.completed_at, 'Duration (ms)': node.duration_ms }} />
        {node.details.map((field, index) => <Fields key={index} values={{ [field.label]: field.value }} />)}
        <details><summary>Technical IDs</summary><Fields values={{ 'Node ID': node.id }} />{node.technical_details.map((field, index) => <Fields key={index} values={{ [field.label]: field.value }} />)}</details>
      </section>}
    </> : <Empty>No structured execution events are available yet.</Empty>}
  </>;
}
