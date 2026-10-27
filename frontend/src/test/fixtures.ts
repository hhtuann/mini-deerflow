import type { ChatSession, GraphProjection, JobDTO, RunView, TraceEvent } from '../types';

export const graph: GraphProjection = {
  nodes: [
    { id: 'parent', label: 'Parent', kind: 'run', status: 'completed', parent_id: null, started_at: null, completed_at: null, duration_ms: 200, safe_summary: 'Parent completed with partial child failure.', details: [], technical_details: [] },
    { id: 'child', label: 'Child beta', kind: 'branch', status: 'failed', parent_id: 'parent', started_at: null, completed_at: null, duration_ms: 10, safe_summary: 'Failed finding was not promoted.', details: [], technical_details: [] },
  ], edges: [{ source: 'parent', target: 'child', relation: 'delegation', label: null, selected: true }],
  metrics: { nodes: 2, running: 0, completed: 1, failed: 1, tool_calls: 2, sub_agents: 1, evidence: 1 },
};
export const trace: TraceEvent = {
  run_id: 'run-1', thread_id: 'chat-one', sequence: 1, kind: 'context_budget', phase: 'complete', outcome: 'compacted', operation: 'run',
  node: null, tool_name: null, route: null, duration_ms: 2, current_step: 1, step_tool_calls: 1, total_tool_calls: 2,
  max_step_tool_calls: 3, max_total_tool_calls: 10, max_replan_cycles: 1, evidence_count: 1, citation_count: 1,
  rejected_citation_count: 0, artifact_count: 1, branch_count: 2, successful_branch_count: 1, failed_branch_count: 1, cancelled_branch_count: 0,
  reserved_tool_calls: 2, used_tool_calls: 2, charged_tool_calls: 2, context_omitted_items: 1, context_truncated_items: 2, context_estimated_tokens: 100,
  error_category: null, error_code: null, turn_id: 'turn-one', context_payload_chars: 400, context_budget_chars: 1000, context_retained_items: 3,
  active_skill_id: 'research-brief', skill_instructions_omitted: true, context_policy_version: 'policy-v1',
  model_schema: null, model_name: null, model_attempt: null, model_duration_ms: null,
  model_input_tokens: null, model_output_tokens: null, model_total_tokens: null,
  model_cached_tokens: null, usage_reported: null,
};
export const run: RunView = {
  thread_id: 'chat-one', turn_id: 'turn-one', operation: 'run', workflow_status: 'completed', goal: 'Research a useful topic',
  plan: [{ step_number: 1, title: 'Find trustworthy evidence', objective: 'Compare sources', success_criteria: 'One accepted source', status: 'completed' }],
  budget: { current_step: 1, total_steps: 1, step_tool_calls: 1, total_tool_calls: 2, max_step_tool_calls: 3, max_total_tool_calls: 10, replan_cycles: 0, max_replan_cycles: 1, max_delegation_concurrency: 2 },
  evidence: [{ canonical_url: 'https://example.org/source', source_tool: 'search', title: 'Accepted source', excerpt: 'Bounded source excerpt.', is_cited: true, provenance: { tool_name: 'search', step_number: 1, step_tool_call_number: 1, total_tool_call_number: 1, observation_index: 0, delegation_id: null, branch_id: null, branch_tool_call_number: null } }],
  accepted_citations: ['https://example.org/source'], rejected_citation_count: 1, reviews: [{ review_number: 1, route: 'finish', findings: [{ category: 'limitation', description: 'One child failed.', related_step_numbers: [1] }] }], replans: [],
  delegations: [{ delegation_id: 'wave-1', parent_step_number: 1, branches: [{ branch_id: 'beta', status: 'failed', tool_calls_used: 1, finding_summary: null }], reserved_tool_calls: 2, used_tool_calls: 1, charged_tool_calls: 1, successful_branch_count: 0, failed_branch_count: 1, cancelled_branch_count: 0, limitations: ['Failed child excluded.'] }],
  traces: [trace], execution_graph: graph, active_skill_id: 'research-brief', limitations: ['Fixed offline scenario.'],
  artifact: { available: true, label: 'Research report', finding_summaries: ['Safe finding'], citation_urls: ['https://example.org/source'], limitation_notices: ['Partial failure'], markdown_source: '# Exact report\n<script>inert()</script>\n' },
};
export const session: ChatSession = { conversation: { thread_id: 'chat-one', title: 'Saved research', status: 'completed', updated_at: '2026-10-24', session_tool_calls: 2, session_tool_call_limit: 30 }, turns: [{ turn_id: 'turn-one', sequence: 1, user_message: 'Research a useful topic', assistant_message: 'A sourced answer [source](https://example.org/source)', status: 'completed', safe_error: null, run }] };
export const job: JobDTO = { job_id: 'job-one', mode: 'offline', thread_id: 'chat-one', turn_id: 'turn-one', operation: 'run', status: 'completed', error_message: null, traces: [trace], execution_graph: graph, view: run };
