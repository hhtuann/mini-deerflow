import { render, screen, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import ExecutionGraph from './ExecutionGraph';
import { graph } from '../test/fixtures';

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
