/** Explicit HTTP DTOs matching api/dto.py and the public demo projections. */
export type Mode = 'live' | 'offline';
export type GraphStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'skipped';
export interface GraphField { label: string; value: string }
export interface GraphNode {
  id: string; kind: 'workflow_node' | 'delegation' | 'branch' | 'tool' | 'run' | 'fan_in';
  label: string; status: GraphStatus; parent_id: string | null; started_at: string | null;
  completed_at: string | null; duration_ms: number | null; safe_summary: string | null;
  details: GraphField[]; technical_details: GraphField[];
}
export interface GraphProjection {
  nodes: GraphNode[];
  edges: { source: string; target: string; relation: string; label: string | null; selected: boolean }[];
  metrics: { nodes: number; running: number; completed: number; failed: number; tool_calls: number; sub_agents: number; evidence: number };
}
export interface Skill { id: string; description: string; applicability: string; version: string }
export interface SkillsDTO { skills: Skill[]; available: boolean }
export interface ConversationSummary {
  thread_id: string; title: string; status: string; updated_at: string; session_tool_calls: number; session_tool_call_limit: number;
}
export interface TraceEvent {
  run_id: string; thread_id: string; sequence: number; kind: string; phase: string; outcome: string;
  operation: string | null; node: string | null; tool_name: string | null; route: string | null;
  duration_ms: number | null; current_step: number | null; step_tool_calls: number | null;
  total_tool_calls: number | null; max_step_tool_calls: number | null; max_total_tool_calls: number | null;
  max_replan_cycles: number | null; evidence_count: number | null; citation_count: number | null;
  rejected_citation_count: number | null; artifact_count: number | null; branch_count: number | null;
  successful_branch_count: number | null; failed_branch_count: number | null; cancelled_branch_count: number | null;
  reserved_tool_calls: number | null; used_tool_calls: number | null; charged_tool_calls: number | null;
  context_omitted_items: number | null; context_truncated_items: number | null; context_estimated_tokens: number | null;
  error_category: string | null; error_code: string | null; turn_id: string | null;
  context_payload_chars: number | null; context_budget_chars: number | null; context_retained_items: number | null;
  active_skill_id: string | null; skill_instructions_omitted: boolean | null; context_policy_version: string | null;
}
export interface Evidence {
  canonical_url: string; source_tool: string; title: string; excerpt: string; is_cited: boolean;
  provenance: { tool_name: string; step_number: number; step_tool_call_number: number; total_tool_call_number: number;
    observation_index: number; delegation_id: string | null; branch_id: string | null; branch_tool_call_number: number | null };
}
export interface RunView {
  thread_id: string; turn_id: string | null; operation: string | null; workflow_status: 'completed' | 'incomplete'; goal: string;
  plan: { step_number: number; title: string; objective: string; success_criteria: string; status: 'completed' | 'current' | 'pending' }[];
  budget: { current_step: number; total_steps: number; step_tool_calls: number; total_tool_calls: number;
    max_step_tool_calls: number; max_total_tool_calls: number; replan_cycles: number; max_replan_cycles: number; max_delegation_concurrency: number };
  evidence: Evidence[]; accepted_citations: string[]; rejected_citation_count: number;
  reviews: { review_number: number; route: string; findings: { category: string; description: string; related_step_numbers: number[] }[] }[];
  replans: { replan_number: number; replaced_step_numbers: number[]; replacement_step_titles: string[] }[];
  delegations: { delegation_id: string; parent_step_number: number;
    branches: { branch_id: string; status: string; tool_calls_used: number; finding_summary: string | null }[];
    reserved_tool_calls: number; used_tool_calls: number; charged_tool_calls: number;
    successful_branch_count: number; failed_branch_count: number; cancelled_branch_count: number; limitations: string[] }[];
  traces: TraceEvent[]; execution_graph: GraphProjection; active_skill_id: string | null; limitations: string[];
  artifact: { label: string; available: boolean; finding_summaries: string[]; citation_urls: string[]; limitation_notices: string[]; markdown_source: string };
}
export interface ChatTurn {
  turn_id: string; sequence: number; user_message: string; assistant_message: string | null;
  status: 'running' | 'completed' | 'interrupted' | 'failed'; safe_error: string | null; run: RunView | null;
}
export interface ChatSession { conversation: ConversationSummary; turns: ChatTurn[] }
export interface JobDTO {
  job_id: string; mode: Mode; thread_id: string; turn_id: string | null; operation: 'run' | 'continue' | 'resume' | 'presentation';
  status: 'queued' | 'running' | 'completed' | 'failed'; error_message: string | null;
  traces: TraceEvent[]; execution_graph: GraphProjection; view: RunView | null;
}
export type PresentationLanguage = 'vi' | 'en';
export type PresentationTheme = 'light' | 'dark' | 'academic';
export interface PresentationMetadata {
  artifact_type: 'pptx'; title: string; theme: PresentationTheme; slide_count: number; layouts: string[];
  outline: { index: number; slide_id: string; layout: string; title: string }[];
  byte_size: number; sha256: string;
}
