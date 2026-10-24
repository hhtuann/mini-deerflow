import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import Inspector from './Inspector';
import { graph, job, run, trace } from '../test/fixtures';
import type { GraphProjection, JobDTO, RunView } from '../types';

vi.mock('./ExecutionGraph', () => ({ default: ({ graph }: { graph: GraphProjection }) => <div>{graph.nodes.map((node) => <p key={node.id}>{node.label} · {node.status}</p>)}</div> }));

function mount(status: JobDTO['status'], persisted: RunView | null = run) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Inspector mode="offline" thread="chat-one" turn="turn-one" run={persisted} job={{ ...job, status, operation: 'resume', traces: [{ ...trace, run_id: 'resume-only' }], execution_graph: { ...graph, nodes: [graph.nodes[0]], edges: [] } }} /></QueryClientProvider>);
}

it.each(['completed', 'failed'] as const)('prefers persisted turn graph and trace history when a resume job is %s', async (status) => {
  mount(status);
  expect(await screen.findByText('Child beta · failed')).toBeVisible();
  await userEvent.click(screen.getByRole('tab', { name: 'Trace' }));
  expect(screen.getByRole('option', { name: 'run-1' })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'resume-only' })).not.toBeInTheDocument();
});

it.each(['queued', 'running'] as const)('uses snapshots only while the job is %s', async (status) => {
  mount(status);
  expect(await screen.findByText('Parent · completed')).toBeVisible();
  expect(screen.queryByText('Child beta · failed')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: 'Trace' }));
  expect(screen.getByRole('option', { name: 'resume-only' })).toBeInTheDocument();
});

it('falls back to a terminal job snapshot when no persisted turn run is available', async () => {
  mount('completed', null);
  expect(await screen.findByText('Parent · completed')).toBeVisible();
  await userEvent.click(screen.getByRole('tab', { name: 'Trace' }));
  expect(screen.getByRole('option', { name: 'resume-only' })).toBeInTheDocument();
});

it('shows the actual goal in Plan independently of artifact availability', async () => {
  mount('completed', { ...run, goal: 'Compare the actual turn goal, not an artifact summary.', artifact: { ...run.artifact, available: false } });
  await userEvent.click(screen.getByRole('tab', { name: 'Plan' }));
  expect(screen.getByRole('heading', { name: 'Goal' })).toBeVisible();
  expect(screen.getByRole('tabpanel')).toHaveTextContent('Compare the actual turn goal, not an artifact summary.');
});

it('combines run/kind/outcome subsets, treats empty selections as All, and resets each or every filter', async () => {
  const user = userEvent.setup();
  const traces = [
    ['run-a', 'tool', 'succeeded'], ['run-a', 'node', 'succeeded'],
    ['run-b', 'tool', 'failed'], ['run-b', 'review', 'failed'],
    ['run-c', 'tool', 'succeeded'], ['run-a', 'tool', 'started'],
    ['run-b', 'node', 'failed'], ['run-c', 'review', 'started'],
  ].map(([run_id, kind, outcome], index) => ({ ...trace, run_id, kind, outcome, sequence: index + 1 }));
  mount('completed', { ...run, traces });
  await user.click(screen.getByRole('tab', { name: 'Trace' }));
  const runs = screen.getByRole('listbox', { name: 'Run' });
  const kinds = screen.getByRole('listbox', { name: 'Kind' });
  const outcomes = screen.getByRole('listbox', { name: 'Outcome' });
  expect(screen.getByRole('status')).toHaveTextContent('8 of 8 events');
  await user.selectOptions(runs, ['run-a', 'run-b']);
  await user.selectOptions(kinds, ['tool', 'node']);
  await user.selectOptions(outcomes, ['succeeded', 'failed']);
  expect(screen.getByRole('status')).toHaveTextContent('4 of 8 events');
  expect(screen.getByText('#1 tool · succeeded · complete')).toBeVisible();
  expect(screen.getByText('#7 node · failed · complete')).toBeVisible();
  expect(screen.queryByText('#5 tool · succeeded · complete')).not.toBeInTheDocument();
  expect(screen.queryByText('#6 tool · started · complete')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'All runs' }));
  expect(runs).toHaveValue([]);
  expect(screen.getByRole('status')).toHaveTextContent('5 of 8 events');
  await user.deselectOptions(kinds, ['tool', 'node']);
  expect(kinds).toHaveValue([]);
  expect(screen.getByRole('status')).toHaveTextContent('6 of 8 events');
  await user.click(screen.getByRole('button', { name: 'All outcomes' }));
  expect(screen.getByRole('status')).toHaveTextContent('8 of 8 events');
  await user.selectOptions(runs, 'run-a');
  await user.selectOptions(kinds, 'review');
  await user.selectOptions(outcomes, 'failed');
  expect(screen.getByText('No trace events match these filters.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Reset trace filters' }));
  expect(runs).toHaveValue([]);
  expect(kinds).toHaveValue([]);
  expect(outcomes).toHaveValue([]);
  expect(screen.getByRole('status')).toHaveTextContent('8 of 8 events');
});
