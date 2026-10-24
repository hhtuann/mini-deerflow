import { lazy, Suspense, useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Download } from 'lucide-react';
import { activeJob, request, conversationPath, segment } from '../api';
import type { GraphProjection, JobDTO, Mode, RunView, TraceEvent } from '../types';
import { Empty, Fields, Status } from './UI';
import { acceptedUrl } from '../links';

const ExecutionGraph = lazy(() => import('./ExecutionGraph'));
const tabs = ['Graph', 'Plan', 'Tools & Delegation', 'Evidence', 'Review', 'Trace', 'Context', 'Artifacts'] as const;
type Tab = typeof tabs[number];
const blankGraph: GraphProjection = { nodes: [], edges: [], metrics: { nodes: 0, running: 0, completed: 0, failed: 0, tool_calls: 0, sub_agents: 0, evidence: 0 } };

function Trace({ traces }: { traces: TraceEvent[] }) {
  const id = useId();
  const [run, setRun] = useState<string[]>([]);
  const [kind, setKind] = useState<string[]>([]);
  const [outcome, setOutcome] = useState<string[]>([]);
  const filtered = traces.filter((event) => (!run.length || run.includes(event.run_id)) && (!kind.length || kind.includes(event.kind)) && (!outcome.length || outcome.includes(event.outcome)));
  return <>
    <p className="caption" id={`${id}-help`}>Select any subset in each list (Ctrl/Command-click or Shift+arrow keys). Empty selection means All. Filters combine across Run, Kind and Outcome.</p>
    <div className="filters">{([
      ['Run', 'runs', 'run_id', run, setRun], ['Kind', 'kinds', 'kind', kind, setKind], ['Outcome', 'outcomes', 'outcome', outcome, setOutcome],
    ] as const).map(([label, plural, field, value, setter]) => <div className="trace-filter" key={field}><label htmlFor={`${id}-${field}`}>{label}</label><select id={`${id}-${field}`} multiple size={4} value={value} aria-describedby={`${id}-help`} onChange={(event) => setter(Array.from(event.target.selectedOptions, (option) => option.value))}>{[...new Set(traces.map((event) => event[field]))].map((item) => <option key={item}>{item}</option>)}</select><button onClick={() => setter([])}>All {plural}</button></div>)}</div>
    <div className="trace-filter-summary"><p className="caption" role="status">{filtered.length} of {traces.length} events</p><button onClick={() => { setRun([]); setKind([]); setOutcome([]); }}>Reset trace filters</button></div>
    {filtered.length ? filtered.map((event, index) => <details className="detail-card" key={`${event.run_id}-${event.sequence}-${index}`}><summary>#{event.sequence} {event.kind} · {event.outcome} · {event.phase}</summary><Fields values={event as unknown as Record<string, string | number | boolean | null>} /></details>) : <Empty>No trace events match these filters.</Empty>}
  </>;
}

export default function Inspector({ run: persistedRun, job, mode, thread, turn, historyState = 'current', onRetryHistory }: { run: RunView | null; job?: JobDTO; mode: Mode; thread?: string; turn?: string; historyState?: 'current' | 'refreshing' | 'unavailable'; onRetryHistory?: () => void }) {
  const id = useId();
  const [tab, setTab] = useState<Tab>('Graph');
  const [graphMode, setGraphMode] = useState<'Current run' | 'Workflow'>('Current run');
  const workflow = useQuery({ queryKey: ['workflow'], queryFn: () => request<GraphProjection>('/workflow'), enabled: tab === 'Graph' && graphMode === 'Workflow', staleTime: Infinity });
  const snapshotOnly = !!(job && activeJob(job)) || historyState !== 'current';
  const run = snapshotOnly ? job?.view ?? null : persistedRun ?? job?.view ?? null;
  const traces = snapshotOnly ? job?.traces ?? [] : persistedRun?.traces ?? job?.traces ?? [];
  const graph = snapshotOnly ? job?.execution_graph ?? blankGraph : persistedRun?.execution_graph ?? job?.execution_graph ?? blankGraph;
  const showingSegment = snapshotOnly || (!persistedRun && !!job);
  return <section className="inspector" aria-label="Execution inspector">
    <div className="inspector-heading"><span className="eyebrow">EXECUTION INSPECTOR</span><h2>Behind the answer</h2><p className="caption">{turn ? `Turn ${turn}` : 'Select a turn to inspect its execution.'}</p>{run && <p className="caption">Historical skill: <strong>{run.active_skill_id ?? 'None'}</strong></p>}</div>
    {showingSegment && <div className="notice" role={historyState === 'unavailable' ? 'alert' : 'status'}>
      <strong>Latest job segment only.</strong> {historyState === 'unavailable' ? 'Saved turn history could not be refreshed.' : historyState === 'refreshing' ? 'Refreshing saved turn history after the terminal job.' : 'Full persisted turn history is not shown in this snapshot.'} Plan, budgets and artifacts shown here belong to this segment, not verified full turn history.
      {historyState !== 'current' && onRetryHistory && <button onClick={onRetryHistory}>Retry saved history</button>}
    </div>}
    <div className="tabs" role="tablist" aria-label="Execution details">{tabs.map((item, index) => <button key={item} id={`${id}-tab-${index}`} role="tab" aria-selected={tab === item} aria-controls={`${id}-panel-${index}`} tabIndex={tab === item ? 0 : -1} onClick={() => setTab(item)} onKeyDown={(event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        setTab(tabs[next]); document.getElementById(`${id}-tab-${next}`)?.focus();
      }
    }}>{item}</button>)}</div>
    <div className="panel" role="tabpanel" id={`${id}-panel-${tabs.indexOf(tab)}`} aria-labelledby={`${id}-tab-${tabs.indexOf(tab)}`} tabIndex={0}>
      {tab === 'Graph' && <>
        <label>Graph view<select value={graphMode} onChange={(event) => setGraphMode(event.target.value as typeof graphMode)}><option>Current run</option><option>Workflow</option></select></label>
        <p className="caption">{graphMode === 'Workflow' ? 'Possible topology only — these routes are not evidence of execution.' : 'Observed execution, reconstructed from server events. Failed children retain their own status.'}</p>
        {graphMode === 'Workflow' && workflow.isError ? <div role="alert">Workflow unavailable. <button onClick={() => void workflow.refetch()}>Retry workflow</button></div> : graphMode === 'Workflow' && !workflow.data ? <Empty>Loading workflow…</Empty> : <Suspense fallback={<Empty>Loading graph…</Empty>}><ExecutionGraph key={graphMode} graph={graphMode === 'Workflow' ? workflow.data ?? blankGraph : graph} /></Suspense>}
      </>}
      {tab === 'Trace' && <Trace traces={traces} />}
      {tab === 'Context' && <>
        <p className="caption">Token counts are approximate character-based heuristics, not model tokenizer measurements. Missing metrics were not reported.</p>
        {traces.filter((event) => event.context_payload_chars != null || event.context_policy_version != null).map((event, index) => <section className="detail-card" key={`${event.run_id}-${event.sequence}-${index}`}><h3>Context · event {event.sequence}</h3><Fields values={{ Run: event.run_id, 'Payload chars': event.context_payload_chars, 'Budget chars': event.context_budget_chars, 'Approximate tokens': event.context_estimated_tokens, 'Retained items': event.context_retained_items, 'Omitted items': event.context_omitted_items, 'Truncated items': event.context_truncated_items, 'Active skill': event.active_skill_id ?? 'Not selected', 'Skill instructions omitted by context budget': event.skill_instructions_omitted, 'Policy identity': event.context_policy_version }} /></section>)}
        {!traces.some((event) => event.context_payload_chars != null || event.context_policy_version != null) && <Empty>No context-budget diagnostics reported for this turn.</Empty>}
      </>}
      {!run && !['Graph', 'Trace', 'Context'].includes(tab) && <Empty>Details will appear when the server saves this turn.</Empty>}
      {run && tab === 'Plan' && <>
        <h3>Goal</h3><p className="run-goal">{run.goal}</p>
        <Fields values={{ 'Workflow status': run.workflow_status, 'Current step': `${run.budget.current_step} / ${run.budget.total_steps}`, 'Step tool calls': `${run.budget.step_tool_calls} / ${run.budget.max_step_tool_calls}`, 'Total tool calls': `${run.budget.total_tool_calls} / ${run.budget.max_total_tool_calls}`, Replans: `${run.budget.replan_cycles} / ${run.budget.max_replan_cycles}`, 'Delegation concurrency limit': run.budget.max_delegation_concurrency }} />
        <ol className="plan-list">{run.plan.map((step) => <li className="detail-card" key={step.step_number}><h3>{step.step_number}. {step.title}</h3><Status value={step.status} /><p>{step.objective}</p><p className="caption">Success criteria: {step.success_criteria}</p></li>)}</ol>
        {!run.plan.length && <Empty>No plan has been recorded.</Empty>}{run.limitations.map((notice, index) => <p className="notice" key={index}>{notice}</p>)}
      </>}
      {run && tab === 'Tools & Delegation' && <>
        {run.delegations.map((wave) => <section className="detail-card" key={wave.delegation_id}><h3>Parent step {wave.parent_step_number}</h3><p className="mono">{wave.delegation_id}</p><Fields values={{ Reserved: wave.reserved_tool_calls, Used: wave.used_tool_calls, Charged: wave.charged_tool_calls }} /><p>Fan-in: {wave.successful_branch_count} successful · {wave.failed_branch_count} failed · {wave.cancelled_branch_count} cancelled</p>{wave.branches.map((branch) => <div className="branch" key={branch.branch_id}><strong>{branch.branch_id}</strong> <Status value={branch.status} /><p>{branch.tool_calls_used} tool calls</p>{branch.finding_summary && <p>{branch.finding_summary}</p>}</div>)}{wave.limitations.map((notice, index) => <p className="notice" key={index}>{notice}</p>)}</section>)}
        <h3>Tool outcomes</h3>{traces.filter((event) => event.tool_name).map((event, index) => <div className="detail-card" key={`${event.run_id}-${event.sequence}-${index}`}><strong>{event.tool_name}</strong> <Status value={event.outcome} /><Fields values={{ Run: event.run_id, Stage: event.phase, Step: event.current_step, 'Duration (ms)': event.duration_ms, 'Error category': event.error_category, 'Error code': event.error_code }} /></div>)}
        {!run.delegations.length && !traces.some((event) => event.tool_name) && <Empty>No tool or delegation activity recorded.</Empty>}
      </>}
      {run && tab === 'Evidence' && <>
        <p className="caption">Accepted source membership is not independent fact-checking. {run.rejected_citation_count} citations rejected.</p>
        {run.evidence.map((item, index) => <section className="detail-card" key={`${item.canonical_url}-${index}`}><h3>{item.title}</h3><span className="status">{item.is_cited ? 'Cited' : 'Not cited'}</span><p>{acceptedUrl(item.canonical_url, run.accepted_citations) ? <a href={item.canonical_url} target="_blank" rel="noopener noreferrer">{item.canonical_url}</a> : <span className="mono">{item.canonical_url}</span>}</p><p>{item.excerpt}</p><details><summary>Source provenance · {item.source_tool}</summary><Fields values={item.provenance} /></details></section>)}
        {!run.evidence.length && <Empty>No successful evidence recorded.</Empty>}
        <h3>Accepted citation URLs</h3>{run.accepted_citations.map((url) => <p className="mono" key={url}>{url}</p>)}
      </>}
      {run && tab === 'Review' && <>
        {run.reviews.map((review) => <section className="detail-card" key={review.review_number}><h3>Review {review.review_number} · {review.route}</h3>{review.findings.map((finding, index) => <div key={index}><strong>{finding.category}</strong><p>{finding.description}</p><p className="caption">Related steps: {finding.related_step_numbers.join(', ') || 'None'}</p></div>)}</section>)}
        {run.replans.map((replan) => <section className="detail-card" key={replan.replan_number}><h3>Replan {replan.replan_number}</h3><p>Replaced steps: {replan.replaced_step_numbers.join(', ')}</p><ul>{replan.replacement_step_titles.map((title, index) => <li key={index}>{title}</li>)}</ul></section>)}
        {!run.reviews.length && !run.replans.length && <Empty>No reviews or replans recorded.</Empty>}{run.limitations.map((notice, index) => <p className="notice" key={index}>{notice}</p>)}
      </>}
      {run && tab === 'Artifacts' && (run.artifact.available ? <>
        <h3>{run.artifact.label}</h3><p className="caption">Research report for this turn. Source is displayed as inert text.</p>
        {thread && turn && !showingSegment && <a className="button primary" href={`/api${conversationPath(mode, thread)}/turns/${segment(turn)}/artifact`} download="report.md"><Download size={18} aria-hidden="true" /> Download Markdown</a>}
        {showingSegment && <p className="caption">Download is available after saved turn history refreshes successfully.</p>}
        <h3>Structured preview</h3><p>{run.goal}</p>{run.artifact.finding_summaries.map((finding, index) => <p key={index}>{finding}</p>)}{run.artifact.citation_urls.map((url) => <p className="mono" key={url}>{url}</p>)}{run.artifact.limitation_notices.map((notice, index) => <p className="notice" key={index}>{notice}</p>)}
        <h3>Exact Markdown source</h3><pre className="source" data-testid="artifact-source">{run.artifact.markdown_source}</pre>
      </> : <Empty>No artifact is available for this turn.</Empty>)}
    </div>
  </section>;
}
