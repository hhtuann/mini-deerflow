import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import ExecutionGraph from './ExecutionGraph';
import { graph, run, trace } from '../test/fixtures';

vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Background: () => null, Controls: () => null,
  MarkerType: { ArrowClosed: 'arrowclosed' }, Position: { Bottom: 'bottom', Top: 'top' },
}));

it('exposes every authoritative backend metric, including zero, and textual status/kind legends', () => {
  render(<ExecutionGraph graph={{ ...graph, metrics: { nodes: 19, running: 0, completed: 7, failed: 2, tool_calls: 13, sub_agents: 5, evidence: 11 } }} />);
  const metrics = screen.getByLabelText('Graph metrics');
  for (const [label, value] of [['Nodes', 19], ['Running', 0], ['Completed', 7], ['Failed', 2], ['Tool calls', 13], ['Sub-agents', 5], ['Evidence', 11]] as const) {
    expect(within(metrics).getByText(label, { selector: 'dt' }).nextElementSibling).toHaveTextContent(String(value));
  }
  const legend = screen.getByRole('region', { name: 'Graph legend' });
  const statuses = within(legend).getByRole('list', { name: 'Node statuses' });
  expect(within(statuses).getAllByRole('listitem').map((item) => item.textContent?.trim())).toEqual(['• pending', '• running', '✓ completed', '× failed', '• cancelled', '• skipped']);
  const kinds = within(legend).getByRole('list', { name: 'Node kinds' });
  expect(within(kinds).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Run', 'Workflow node', 'Delegation', 'Branch', 'Tool', 'Fan-in']);
});

it('correlates a selected workflow node with only its own redacted context metrics', async () => {
  const realNode = { ...graph.nodes[0], id: 'workflow:run-1:planner', kind: 'workflow_node' as const, label: 'Planner',
    details: [{ label: 'Executions', value: '1' }], safe_summary: null };
  const unrelated = { ...graph.nodes[1], id: 'workflow:other:planner', kind: 'workflow_node' as const };
  render(<ExecutionGraph graph={{ ...graph, nodes: [realNode, unrelated], edges: [] }}
    run={{ ...run, goal: "Compare public sources" }} traces={[
      { ...trace, run_id: 'run-1', node: 'planner', context_estimated_tokens: 117, context_payload_chars: 470, error_category: null },
      { ...trace, run_id: 'other', node: 'planner', context_estimated_tokens: 999999, context_payload_chars: 999999, error_category: null },
    ]} />);
  expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('User goal (not LLM prompt): Compare public sources');
  expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('117');
  expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('Structured plan result (turn-level, not raw model output)');
  expect(screen.getByRole('region', { name: 'Node details' })).not.toHaveTextContent('999999');
  expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('Not reported');
  await userEvent.selectOptions(screen.getByLabelText('Inspect node'), 'workflow:other:planner');
  expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('999999');
});

it('never promotes Workflow topology into an executed prompt or billing report', () => {
  render(<ExecutionGraph graph={graph} isWorkflow traces={[{ ...trace, node: 'planner', context_estimated_tokens: 1000 }]} />);
  const details = screen.getByRole('region', { name: 'Node details' });
  expect(details).toHaveTextContent('Workflow topology only');
  expect(details).not.toHaveTextContent('1000');
  expect(details).not.toHaveTextContent('Actual prompt tokens');
});
