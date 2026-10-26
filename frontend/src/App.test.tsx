import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { graph, job, session, trace } from './test/fixtures';
import type { GraphProjection, JobDTO } from './types';
import type { SessionSnapshot } from './session';

vi.mock('./components/ExecutionGraph', () => ({ default: ({ graph }: { graph: GraphProjection }) => <div data-testid="rendered-graph" data-node-count={graph.metrics.nodes}><p>Read-only graph loaded</p>{graph.nodes.map((node) => <p key={node.id}>{node.label}</p>)}</div> }));
const catalog = { available: true, skills: [{ id: 'research-brief', description: 'Compact source-backed brief', applicability: 'Focused research', version: '1' }] };
let posts: { path: string; body: Record<string, unknown> }[];
let state = structuredClone(session);
let jobList: typeof job[];
let failConversations = false;

beforeEach(() => {
  window.history.replaceState(null, '', '/?mode=offline');
  posts = []; state = structuredClone(session); jobList = []; failConversations = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input);
    if (init?.method === 'POST') {
      posts.push({ path, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      jobList = [job]; return new Response(JSON.stringify(job), { status: 202 });
    }
    if (path.endsWith('/skills')) return new Response(JSON.stringify(catalog));
    if (path.endsWith('/health')) return new Response(JSON.stringify({ status: 'ok', modes: ['live', 'offline'] }));
    if (path.endsWith('/workflow')) return new Response(JSON.stringify(graph));
    if (path.endsWith('/jobs')) return new Response(JSON.stringify(jobList));
    if (path.endsWith('/conversations')) return new Response(JSON.stringify(path.includes('/live/') ? [] : [state.conversation]), { status: failConversations ? 503 : 200 });
    return new Response(JSON.stringify(state));
  }));
});
const clients: QueryClient[] = [];
afterEach(() => { clients.forEach((client) => client.clear()); clients.length = 0; });
function mount() {
  // Retain the real production cache lifetime/staleness; disable retry only so
  // fault-injection assertions do not wait for exponential retry delays.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 5 * 60_000, staleTime: 1000 }, mutations: { retry: false } } });
  clients.push(client);
  return { ...render(<QueryClientProvider client={client}><App /></QueryClientProvider>), client };
}

function projectedGraph(label: string, nodes: number): GraphProjection {
  return { ...graph, nodes: [{ ...graph.nodes[0], id: label, label }], edges: [], metrics: { ...graph.metrics, nodes } };
}

function prepareResume() {
  const oldRun = state.turns[0].run!;
  oldRun.goal = 'Stale interrupted goal';
  oldRun.execution_graph = projectedGraph('Stale interrupted graph', 61);
  oldRun.traces = Array.from({ length: 61 }, (_, sequence) => ({ ...trace, sequence }));
  oldRun.artifact.markdown_source = '# Stale interrupted artifact';
  state.turns[0].status = 'interrupted';
  state.turns[0].assistant_message = null;
  const fresh = structuredClone(state);
  fresh.turns[0].status = 'completed';
  fresh.turns[0].assistant_message = 'Fresh answer';
  const fullRun = fresh.turns[0].run!;
  fullRun.goal = 'Fresh full turn goal';
  fullRun.execution_graph = { ...projectedGraph('Fresh full-history graph', 92), nodes: [{ ...graph.nodes[1], label: 'Recovered historical failed child' }, ...projectedGraph('Fresh full-history graph', 92).nodes] };
  fullRun.traces = Array.from({ length: 92 }, (_, sequence) => ({ ...trace, sequence }));
  fullRun.artifact.markdown_source = '# Fresh full report';
  const segment = structuredClone(fullRun);
  segment.goal = 'Latest segment goal';
  segment.budget.total_tool_calls = 9;
  segment.execution_graph = projectedGraph('Latest resume segment', 92);
  segment.traces = Array.from({ length: 6 }, (_, sequence) => ({ ...trace, sequence, run_id: 'resume-only' }));
  segment.artifact.markdown_source = '# Latest segment artifact';
  const resume: JobDTO = { ...job, job_id: 'resume-two', operation: 'resume', status: 'running', execution_graph: segment.execution_graph, traces: segment.traces, view: segment };
  return { fresh, resume };
}

describe('research workspace', () => {
  it('submits one new turn, includes metadata selection, then resets the next-turn skill', async () => {
    const user = userEvent.setup(); mount();
    await screen.findByRole('option', { name: 'research-brief' });
    await waitFor(() => expect(screen.getByLabelText('Your research question')).toBeEnabled());
    await user.selectOptions(screen.getByLabelText('Skill for next turn'), 'research-brief');
    expect(screen.getByText(/Compact source-backed brief/)).toBeVisible();
    await user.type(screen.getByLabelText('Your research question'), 'Research a useful topic');
    await user.dblClick(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByRole('heading', { name: 'Saved research' });
    expect(posts).toHaveLength(1);
    expect(posts[0]).toEqual({ path: '/api/offline/conversations', body: { message: 'Research a useful topic', skill_id: 'research-brief' } });
    expect(screen.getByLabelText('Skill for next turn')).toHaveValue('');
    expect(window.location.search).toContain('thread=chat-one');
    expect(screen.getByText('research-brief', { selector: 'strong' })).toBeVisible();
    await user.type(screen.getByLabelText('Ask a follow-up'), 'Please compare the evidence');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({ path: '/api/offline/conversations/chat-one/turns', body: { message: 'Please compare the evidence', skill_id: null } });
  });

  it('reopens server history and isolates mode navigation and polling', async () => {
    const user = userEvent.setup(); mount();
    await user.click(await screen.findByRole('button', { name: /Saved research/ }));
    expect(await screen.findByText(/A sourced answer/)).toBeVisible();
    await user.selectOptions(screen.getByLabelText('Execution mode'), 'live');
    await screen.findByText('LIVE RESEARCH');
    expect(screen.queryByText(/A sourced answer/)).not.toBeInTheDocument();
    expect(window.location.search).not.toContain('thread');
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path) === '/api/live/jobs')).toBe(true));
  });

  it('does not redirect into an old mode when a POST finishes after browser navigation', async () => {
    const implementation = vi.mocked(fetch).getMockImplementation()!;
    let release: ((value: Response) => void) | undefined;
    vi.mocked(fetch).mockImplementation((input, init) => init?.method === 'POST'
      ? new Promise<Response>((resolve) => { release = resolve; })
      : implementation(input, init));
    const user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByLabelText('Your research question')).toBeEnabled());
    await user.type(screen.getByLabelText('Your research question'), 'Research a useful topic');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(release).toBeDefined());
    act(() => { window.history.pushState(null, '', '/?mode=live'); window.dispatchEvent(new PopStateEvent('popstate')); });
    expect(await screen.findByText('LIVE RESEARCH')).toBeVisible();
    await act(async () => { release!(new Response(JSON.stringify(job), { status: 202 })); });
    expect(window.location.search).toBe('?mode=live');
    expect(screen.queryByRole('heading', { name: 'Saved research' })).not.toBeInTheDocument();
  });

  it('retries a safe read error through Refresh', async () => {
    failConversations = true; const user = userEvent.setup(); mount();
    expect(await screen.findByText('Conversation list unavailable. Use Refresh to retry.')).toBeVisible();
    failConversations = false;
    await user.click(screen.getByRole('button', { name: 'Refresh & retry' }));
    expect(await screen.findByRole('button', { name: /Saved research/ })).toBeVisible();
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('disables unresolved composer and resumes the persisted turn without a new message', async () => {
    state.turns[0].status = 'interrupted'; state.turns[0].assistant_message = null;
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); mount();
    const resume = await screen.findByRole('button', { name: 'Resume active turn' });
    expect(screen.getByLabelText('Ask a follow-up')).toBeDisabled();
    await user.click(resume);
    await waitFor(() => expect(posts[0]).toEqual({ path: '/api/offline/conversations/chat-one/resume', body: {} }));
  });

  it('discovers an active job after reload and prevents duplicate operations', async () => {
    jobList = [{ ...job, status: 'running' }];
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one'); mount();
    expect(await screen.findByText(/Submission is disabled while it runs/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  });

  it('retains cached history but defaults to the active follow-up after navigating away and reopening', async () => {
    state.turns[0].run!.execution_graph = projectedGraph('Historical turn one graph', 1);
    jobList = [job];
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); const { client } = mount();
    await user.click(await screen.findByRole('button', { name: 'Inspect turn 1' }));
    expect(await screen.findByText('Historical turn one graph')).toBeVisible();
    const cached = client.getQueryData<SessionSnapshot>(['offline', 'session', 'chat-one']);
    expect(cached?.view.turns).toHaveLength(1);
    jobList = [job, { ...job, job_id: 'followup-two', turn_id: 'turn-two', operation: 'continue', status: 'running', view: null, execution_graph: projectedGraph('Active follow-up graph', 7) }];
    await act(async () => { await client.invalidateQueries({ queryKey: ['offline', 'jobs'] }); });
    await screen.findByText(/Submission is disabled while it runs/);
    // An explicitly selected historical turn stays selected until navigation.
    expect(screen.getByLabelText('Inspect turn')).toHaveValue('turn-one');
    await user.click(screen.getByRole('button', { name: 'New chat' }));
    expect(client.getQueryData(['offline', 'session', 'chat-one'])).toBe(cached);
    await user.click(screen.getByRole('button', { name: /Saved research/ }));
    await waitFor(() => expect(screen.getByLabelText('Inspect turn')).toHaveValue('turn-two'));
    expect(client.getQueryData(['offline', 'session', 'chat-one'])).toBe(cached);
    expect(await screen.findByText('Active follow-up graph')).toBeVisible();
    expect(screen.getByRole('option', { name: /Current job.*turn-two/ })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Inspect turn'), 'turn-one');
    expect(await screen.findByText('Historical turn one graph')).toBeVisible();
    expect(screen.queryByText('Active follow-up graph')).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Inspect turn'), 'turn-two');
    expect(await screen.findByText('Active follow-up graph')).toBeVisible();
  });

  it('keeps a terminal resume on a labeled latest segment during delayed/failed refresh, then restores full history after retry', async () => {
    const { fresh, resume } = prepareResume();
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); const { client } = mount();
    await user.click(await screen.findByRole('button', { name: 'Inspect turn 1' }));
    expect(await screen.findByText('Stale interrupted graph')).toBeVisible();
    jobList = [resume];
    await act(async () => { await client.invalidateQueries({ queryKey: ['offline', 'jobs'] }); });
    expect(await screen.findByText('Latest resume segment')).toBeVisible();
    const original = vi.mocked(fetch).getMockImplementation()!;
    let respond: ((response: Response) => void) | undefined;
    let succeed = false;
    vi.mocked(fetch).mockImplementation((input, init) => String(input) === '/api/offline/conversations/chat-one'
      ? succeed ? Promise.resolve(new Response(JSON.stringify(fresh))) : new Promise<Response>((resolve) => { respond = resolve; })
      : original(input, init));
    jobList = [{ ...resume, status: 'completed' }];
    await act(async () => { await client.invalidateQueries({ queryKey: ['offline', 'jobs'] }); });
    await waitFor(() => expect(respond).toBeDefined());
    expect(screen.getByText(/Refreshing saved turn history after the terminal job/)).toBeVisible();
    expect(screen.getByTestId('rendered-graph')).toHaveAttribute('data-node-count', '92');
    expect(screen.queryByText('Stale interrupted graph')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume active turn' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask a follow-up')).toBeDisabled();
    await user.click(screen.getByRole('tab', { name: 'Plan' }));
    expect(screen.getByText('Latest segment goal')).toBeVisible();
    expect(screen.queryByText('Stale interrupted goal')).not.toBeInTheDocument();
    expect(screen.getByText('9 / 10')).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }));
    expect(screen.getByTestId('artifact-source')).toHaveTextContent('# Latest segment artifact');
    expect(screen.queryByRole('link', { name: 'Download Markdown' })).not.toBeInTheDocument();
    await act(async () => { respond!(new Response('{}', { status: 503 })); });
    expect(await screen.findByText(/Saved turn history could not be refreshed/)).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Graph' }));
    expect(screen.getByText('Latest resume segment')).toBeVisible();
    expect(screen.queryByText('Stale interrupted graph')).not.toBeInTheDocument();
    succeed = true;
    await user.click(screen.getByRole('button', { name: 'Retry saved history' }));
    expect(await screen.findByText('Fresh full-history graph')).toBeVisible();
    expect(screen.getByText('Recovered historical failed child')).toBeVisible();
    expect(screen.queryByText('Latest job segment only.')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText('Ask a follow-up')).toBeEnabled());
    await user.click(screen.getByRole('tab', { name: 'Trace' }));
    expect(screen.getByText('92 of 92 events')).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }));
    expect(screen.getByTestId('artifact-source')).toHaveTextContent('# Fresh full report');
    expect(screen.getByRole('link', { name: 'Download Markdown' })).toBeVisible();
  });

  it('does not reconcile a terminal job with a read that started before the terminal transition', async () => {
    const { fresh, resume } = prepareResume();
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); const { client } = mount();
    await user.click(await screen.findByRole('button', { name: 'Inspect turn 1' }));
    expect(await screen.findByText('Stale interrupted graph')).toBeVisible();
    const original = vi.mocked(fetch).getMockImplementation()!;
    const responses: ((response: Response) => void)[] = [];
    vi.mocked(fetch).mockImplementation((input, init) => String(input) === '/api/offline/conversations/chat-one'
      ? new Promise<Response>((resolve) => { responses.push(resolve); }) : original(input, init));
    act(() => { void client.invalidateQueries({ queryKey: ['offline', 'session', 'chat-one'] }); });
    await waitFor(() => expect(responses).toHaveLength(1));
    jobList = [{ ...resume, status: 'completed' }];
    await act(async () => { await client.invalidateQueries({ queryKey: ['offline', 'jobs'] }); });
    expect(await screen.findByText('Latest resume segment')).toBeVisible();
    await act(async () => { responses[0](new Response(JSON.stringify(state))); });
    await waitFor(() => expect(responses).toHaveLength(2));
    expect(screen.getByText(/Refreshing saved turn history after the terminal job/)).toBeVisible();
    expect(screen.queryByText('Stale interrupted graph')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask a follow-up')).toBeDisabled();
    await act(async () => { responses[1](new Response(JSON.stringify(fresh))); });
    expect(await screen.findByText('Fresh full-history graph')).toBeVisible();
    expect(screen.queryByText('Latest job segment only.')).not.toBeInTheDocument();
    expect(client.getQueryData<SessionSnapshot>(['offline', 'session', 'chat-one'])?.terminalJobs).toContain('resume-two:completed');
  });

  it('opens per-turn execution panels, filters traces and keeps artifacts inert', async () => {
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); mount();
    await user.click(await screen.findByRole('button', { name: 'Inspect turn 1' }));
    await user.click(screen.getByRole('tab', { name: 'Evidence' }));
    expect(screen.getByRole('heading', { name: 'Accepted source' })).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Context' }));
    expect(screen.getByText('Approximate tokens')).toBeVisible();
    expect(screen.getByText('policy-v1')).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Trace' }));
    await user.selectOptions(screen.getByLabelText('Kind'), 'context_budget');
    expect(screen.getByText('1 of 1 events')).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }));
    expect(screen.getByTestId('artifact-source').textContent).toBe(session.turns[0].run?.artifact.markdown_source);
    expect(within(screen.getByTestId('artifact-source')).queryByRole('script')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download Markdown' })).toHaveAttribute('href', '/api/offline/conversations/chat-one/turns/turn-one/artifact');
  });
  it('exposes a keyboard-adjustable desktop Inspector separator without losing chat', async () => {
    window.history.replaceState(null, '', '/?mode=offline&thread=chat-one');
    const user = userEvent.setup(); mount();
    await user.click(await screen.findByRole('button', { name: 'Inspect turn 1' }));
    const handle = screen.getByRole('separator', { name: 'Resize execution inspector' });
    expect(handle).toHaveAttribute('aria-valuenow', '400');
    handle.focus();
    await user.keyboard('{ArrowLeft}');
    expect(handle).toHaveAttribute('aria-valuenow', '424');
    await user.keyboard('{Home}');
    expect(handle).toHaveAttribute('aria-valuenow', '340');
    await user.keyboard('{End}');
    expect(handle).toHaveAttribute('aria-valuenow', '650');
    expect(screen.getByLabelText('Ask a follow-up')).toBeInTheDocument();
  });

});
