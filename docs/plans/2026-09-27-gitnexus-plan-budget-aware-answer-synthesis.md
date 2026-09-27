# GitNexus Engineering Plan

> Task: Xử lý budget, tổng hợp mâu thuẫn, bảo toàn tiếng Việt, citation cấp claim và bảng so sánh; bổ sung test và hoàn thiện kiến trúc synthesis/review.
> Evidence verified at commit 2e0834abe4f985c58751be33d6967c0c113af32f; GitNexus index refreshed this session (--index-only --pdg).
> Evidence provenance schema 2; global dirty digest ae300e2be90c7f880bd9bf9aff64c047147ec34e80ac8785f9e8fde1f0a4114b; cited-path manifest 30 sorted entries; exact generated plan path excluded.

## 1. Objective

Thiết kế luồng kết thúc có kiểm soát để agent luôn tạo câu trả lời công khai hữu ích ngay cả khi cạn budget: phân biệt kết quả hoàn chỉnh/một phần, review cuối không lặp, tổng hợp bằng chứng và mâu thuẫn theo claim, trả lời tiếng Việt cho câu hỏi tiếng Việt, sinh bảng cho yêu cầu so sánh, và chỉ phát citation ánh xạ tới evidence hợp lệ. [inferred]

Không thay đổi schema PlanStep; truyền budget metadata riêng và phân bổ động để tránh migration plan/checkpoint không cần thiết. [inferred]

## 2. Current Behaviour

1. planner_node chỉ truyền goal và conversation_context; create_research_plan nhận tool catalog nhưng không nhận RuntimeLimits, nên plan 3–7 bước không biết tổng số tool call hay quota mỗi bước. [verified: src/mini_deerflow/agent_workflow.py:219, src/mini_deerflow/planner.py:72, src/mini_deerflow/runtime.py:773]
2. build_action_context tính remaining_step_tool_calls và remaining_total_tool_calls độc lập; không dành quota cho các bước còn lại. [verified: src/mini_deerflow/decision.py:82]
3. route_after_decision đưa cả cạn quota bước và quota tổng vào budget_exhausted; node chỉ ghi lỗi/xoá pending action, rồi graph đi thẳng sang synthesis. [verified: src/mini_deerflow/agent_workflow.py:282, src/mini_deerflow/agent_workflow.py:910, src/mini_deerflow/agent_workflow.py:1161]
4. Test test_budget_exhaustion_skips_review_like_day_09 đang đóng đinh việc bỏ qua reviewer khi cạn budget. [verified: tests/test_review_loop.py:661]
5. synthesize_node gom finding của mọi review verdict trong lịch sử thành review_notes. Review cũ đã được giải quyết vẫn có thể xuất hiện như limitation cuối. [verified: src/mini_deerflow/agent_workflow.py:964]
6. build_user_facing_answer tách một StepFinding thành nhiều câu rồi gắn nguyên danh sách citation cho từng câu; discrepancy dựa vào từ khoá và fragment sau “Kết luận:”/“Conclusion:” được ưu tiên. Đây là nguồn citation spam và kết luận trung gian bị nâng thành kết luận toàn cục. [verified: src/mini_deerflow/evidence.py:540, src/mini_deerflow/evidence.py:590, src/mini_deerflow/evidence.py:628]
7. render_user_answer có heading tiếng Việt cố định nhưng không dịch nội dung finding, không có schema bảng và không mang complete/partial status. [verified: src/mini_deerflow/evidence.py:236, src/mini_deerflow/evidence.py:722]
8. Demo dựng lại answer từ findings và coi state có final_answer là completed, kể cả answer bị cắt vì budget. [verified: src/mini_deerflow/demo/view_models.py:523, src/mini_deerflow/demo/view_models.py:632]
9. CLI đã reconfigure stdout/stderr UTF-8. Mojibake quan sát được chưa chứng minh lỗi core; cần test đúng boundary trước khi sửa encoding. [verified: src/mini_deerflow/cli.py:53] [inferred]

## 3. Relevant Architecture

- runtime.py là composition root cho model, planner, action selector, reviewer, replanner và build_agent_runtime; answer synthesizer mới phải được bind tại đây với cùng ContextBudget. [verified: src/mini_deerflow/runtime.py:648, src/mini_deerflow/runtime.py:693]
- agent_workflow.py sở hữu state machine plan → decide → execute/complete/delegate → review/replan → synthesize → artifact. Budget/review changes phải giữ checkpoint và không lặp tool đã hoàn tất. [verified: src/mini_deerflow/agent_workflow.py:144]
- decision.py, review.py và context_budget.py tạo projection chỉ dành cho LLM trong khi full state giữ nguyên cho checkpoint/audit/render; synthesis context phải theo mẫu này. [verified: src/mini_deerflow/context_budget.py:1, src/mini_deerflow/decision.py:82, src/mini_deerflow/review.py:328]
- structured_output.py và LLMReviewer là mẫu cho structured output, validation, retry giới hạn và provider failure. [verified: src/mini_deerflow/structured_output.py:1, src/mini_deerflow/llm_reviewer.py:19]
- evidence.py là trust boundary cho canonical URL, provenance, finding, public answer và renderer. URL do model đề xuất không được đi thẳng ra output. [verified: src/mini_deerflow/evidence.py:172, src/mini_deerflow/evidence.py:221]
- state.py là checkpoint contract; field mới phải optional/read-with-default. final_answer string vẫn giữ cho ledger/UI. [verified: src/mini_deerflow/state.py:24, tests/test_runtime_resume.py:186]
- demo/view_models.py là projection boundary; sanitizer chỉ giữ link trong evidence allowlist. [verified: src/mini_deerflow/demo/view_models.py:632, src/mini_deerflow/demo/view_models.py:692]

## 4. GitNexus Findings

- context(name=build_agent_workflow) + impact(upstream, depth=3): direct caller build_agent_runtime; risk CRITICAL vì nằm trên 10 execution flows. [graph]
- context(name=build_user_facing_answer) + impact: direct caller render_user_answer; risk CRITICAL; callees gồm claim normalization, finding split, internal-text filter và merge. [graph]
- context(name=render_user_answer) + impact: direct callers synthesize_node và demo _answer_markdown; risk CRITICAL. [graph]
- context(name=create_default_agent_runtime) + impact: direct caller open_default_agent_runtime; risk CRITICAL, 11 process memberships. [graph]
- context(name=create_research_plan) + impact: indexed direct caller run_research_planner, risk LOW; runtime partial là caller động được source-verify. [graph] [verified: src/mini_deerflow/runtime.py:773]
- query(search_query="budget exhaustion final review synthesis public answer contradiction stale review notes claim citations output language comparison table persistence demo rendering tests") xác định cụm trung tâm workflow synthesis, evidence renderer và demo projection; tests chính ở review loop, evidence và demo view models. [graph]
- Index được refresh với PDG ở commit đã pin. Analyzer báo callable-value-flow candidate cap tại runtime.py/cli.py và vài test, nên quan hệ dynamic được source-verify thay vì coi empty edge là an toàn. [graph]

## 5. Statement-Level PDG Findings

- pdg_query controls trên route_after_decision: điều kiện dòng 305 kiểm soát budget_exhausted (306) và execute_tool (308). Budget classification phải tính một lần và dùng cho cả ActionContext lẫn route. [graph] [inferred]
- pdg_query flows trên build_user_facing_answer, variable review_notes: review_notes chảy tới limitations dòng 693–698 sau lọc/dedup, không có active/resolved lifecycle. [graph]
- pdg_query flows trên agent_workflow.py, variable review_notes: findings của toàn bộ review_verdicts được flatten ở dòng 990–1004 rồi truyền renderer ở 1005–1009. Public synthesis phải dùng latest snapshot; full history chỉ dành cho report/audit. [graph] [inferred]
- budget_exhausted_node chỉ mutate errors và pending_action; không ghi status/reason. Edge vô điều kiện sang synthesis khiến reviewer không xác nhận gap cuối. [verified: src/mini_deerflow/agent_workflow.py:910, src/mini_deerflow/agent_workflow.py:1161]
- synthesize_node tạo public answer, internal report và artifact. Ordering phải là validate structure/citations → render final string → report → artifact; synthesis failure phải fallback mà không mất report/checkpoint. [verified: src/mini_deerflow/agent_workflow.py:964] [inferred]

## 6. Proposed Changes

### 6.1 Budget-aware planning và quota thực thi

- planner.py / create_research_plan, PLANNER_SYSTEM_PROMPT: nhận planning-budget payload gồm hard limit mỗi bước, tổng tool calls và replan cycles; yêu cầu plan ưu tiên các trục có giá trị cao trong budget nhưng vẫn giữ schema 3–7 bước. Không thêm field vào PlanStep. [inferred]
- runtime.py / RuntimeLimits, create_default_agent_runtime: bind limits vào planner và answer synthesizer; truyền synthesizer xuống workflow như dependency optional để runtime custom/test cũ vẫn chạy deterministic. [inferred]
- decision.py / ActionContext, build_action_context: thêm allocated_step_tool_calls và remaining_allocated_step_tool_calls; quota động là min(hard per-step, ceil(remaining total / remaining steps)), sau đó trừ call đã dùng. Một pure helper phải được workflow route dùng lại. [inferred]
- agent_workflow.py / route_after_decision, budget_exhausted_node, review_node: phân biệt quota bước hết nhưng tổng còn với tổng budget hết. Trường hợp đầu đóng bước partial, ghi limitation, tăng current_step, reset counter và review trước khi tiếp tục. Trường hợp hai ghi terminal reason, gọi review cuối nếu có reviewer, rồi ép finish để không replan/loop khi không còn call. [inferred]
- Tool-call budget không bao gồm model calls của review/synthesis. Nếu cần LLM-call budget phải là follow-up riêng. [verified: src/mini_deerflow/runtime.py:86] [inferred]

### 6.2 Completion metadata và review snapshot

- state.py / AgentState, create_initial_state: thêm completion_status (running|complete|partial), finalization_reason nullable và public_answer nullable. Checkpoint cũ đọc bằng state.get; initial state mới khai báo đủ field. [inferred]
- review.py / ReviewContext, ReviewVerdict: giữ full history cho audit nhưng định nghĩa verdict mới nhất là snapshot đầy đủ các vấn đề còn mở. Reviewer không được thêm citation. [inferred]
- llm_reviewer.py / LLMReviewer: prompt yêu cầu unresolved-current snapshot; terminal finalization phải đánh giá gap/contradiction cuối nhưng không đề nghị thêm công việc. Runtime vẫn ép finish nếu model vi phạm. [inferred]
- agent_workflow.py / synthesize_node: chỉ latest review snapshot đi vào public synthesis; full history vẫn ở research_report. completion_status=partial cho budget/replan exhaustion. [inferred]

### 6.3 Structured synthesis, claim citations và contradiction handling

- Tạo src/mini_deerflow/answer_synthesis.py (absence đã pin): structured-output adapter theo mẫu LLMReviewer, context gồm goal, language hint, completion metadata, projected findings, numbered successful evidence và latest unresolved review. [inferred]
- Draft nội bộ không nhận URL tự do. Mỗi claim/discrepancy/comparison row chỉ trả evidence_indices; validator ánh xạ sang EvidenceRecord.canonical_url, từ chối out-of-range/failed evidence, dedup theo thứ tự rồi tạo UserFacingAnswer. Model không thể phát minh URL. [inferred]
- Prompt: trả lời trực tiếp; phân tách fact/opinion; không nâng progress summary thành kết luận; hợp nhất claim tương đương; nếu evidence mâu thuẫn thì nêu cả hai giá trị với citation riêng và không tự chọn; không lộ trace/budget internals; output cùng ngôn ngữ goal. [inferred]
- ContextBudget áp dụng cho synthesis bằng projector hiện có; evidence identity/provenance không bị cắt, text có marker truncation và payload quá hard bound fail trước model call. [verified: src/mini_deerflow/context_budget.py:207] [inferred]
- Provider/schema failure ghi error có kiểm soát rồi dùng deterministic safe fallback; vẫn tạo report/checkpoint. Fallback không ưu tiên _conclusion_fragments. [inferred]

### 6.4 Vietnamese, claim rendering và bảng so sánh

- evidence.py / AnswerClaim, UserFacingAnswer, build_user_facing_answer, render_user_answer: mở rộng model với language, completion_status, discrepancies, limitations và optional comparison table gồm subjects + rows (criterion, hai giá trị, assessment, citations). [inferred]
- Renderer deterministic: heading theo language; goal tiếng Việt bắt buộc vi; marker theo từng claim/row; source list chỉ từ citation được dùng; escape pipe, CR/LF và active Markdown trong cell; hiện banner “kết quả một phần” khi budget cạn. [inferred]
- Đường mới dùng evidence index từng claim, không gắn toàn bộ citation của finding vào mọi câu. Legacy adapter vẫn mở checkpoint cũ nhưng lọc thêm câu “Step closed with the step tool budget exhausted...”. [inferred]
- Language hint dùng heuristic deterministic tối thiểu vi/en cộng instruction cho model; validator yêu cầu draft language khớp hint. Ngôn ngữ ngoài vi/en là open question. [inferred]

### 6.5 Persistence, demo và CLI boundary

- demo/view_models.py / _answer_markdown, project_demo_run: ưu tiên rerender state.public_answer; chỉ rebuild legacy khi field vắng. complete → completed, running|partial → incomplete. [inferred]
- cli.py: không recode source string. Bổ sung regression test code points/UTF-8; chỉ sửa CLI nếu test chứng minh lỗi tại boundary. [verified: src/mini_deerflow/cli.py:53] [inferred]
- final_answer vẫn là rendered string; public_answer là dữ liệu để rerender. Resume checkpoint đã finalize không gọi lại synthesizer; checkpoint cũ dùng legacy projection. [inferred]

## 7. Implementation Sequence

1. Rebase assumptions lên worktree hiện tại; bảo toàn các thay đổi unstaged ở planner.py, schemas.py và các test được provenance đánh dấu. Chạy impact lại ngay trước từng edit. [verified: provenance manifest]
2. Mở rộng state và test initial/strict checkpoint serialization; chưa đổi routing.
3. Thêm pure budget allocation helper, ActionContext fields và planner budget input; unit-test công thức trước graph.
4. Đổi routing: quota bước → partial step + review/continue; quota tổng → terminal review + forced finish. Cập nhật test “skips review” và guard no-loop.
5. Tạo structured answer-synthesis boundary với context budget, evidence-index validation, language contract và fallback.
6. Mở rộng public model/renderer cho claim citations, contradiction, partial banner và table; tách legacy adapter.
7. Nối synthesizer vào runtime/workflow; latest review đi public, full history đi report; persist structured + rendered answer.
8. Cập nhật demo status/projection và CLI Unicode test; chạy resume tests để chứng minh không lặp tool/synthesis.
9. Thêm acceptance “Ronaldo và Messi ai mạnh hơn?” với evidence giả lập có mâu thuẫn và budget giới hạn; kiểm tra tiếng Việt, bảng, citation từng hàng, partial status và không lộ execution metadata.
10. Chạy targeted/full tests, Ruff và GitNexus detect-changes; xử lý HIGH/CRITICAL chưa có regression test. Không commit nếu người dùng chưa yêu cầu.

## 8. Test Strategy

### New tests: tests/test_answer_synthesis.py

- Vietnamese goal + English findings → Vietnamese public answer, no trace leakage.
- Two evidence values conflict → discrepancy retains both claims with separate citations.
- Invented/out-of-range evidence index → claim/row rejected or safe fallback; invented URL absent.
- Comparison request → table has subjects, escaped cells, row-level citations and sources only for used evidence.
- Non-comparison request → no artificial empty table.
- Provider/schema failure → deterministic fallback, controlled error and report remains renderable.
- Payload over ContextBudget → compaction or controlled pre-invocation failure.

### Existing tests to update

- tests/test_decision.py: allocated quota equals min(hard remainder, ceil(remaining total / remaining steps)); under-use expands later quota; zero total stays zero.
- tests/test_planner.py: budget metadata in planning context; prompt retains exact Plan schema.
- tests/test_agent_workflow.py: per-step exhaustion advances partial when total remains; total exhaustion terminates partial; hard counters never exceeded.
- tests/test_review_loop.py: replace skip-review expectation with exactly-one terminal review; terminal continue/replan coerces finish; latest verdict feeds public limitations while full history remains in report.
- tests/test_llm_reviewer.py: terminal/unresolved-snapshot prompt; reviewer cannot add citation or reopen execution.
- tests/test_context_budget.py: synthesis projection preserves evidence identity and hard serialized bound.
- tests/test_evidence.py: claim-specific citations, discrepancies, partial notice, Vietnamese body, table escaping and legacy internal-text filter; remove “every split sentence inherits every citation”.
- tests/test_runtime_composition.py: capture synthesizer dependency and budget-bound planner; structured call list includes answer schema.
- tests/test_state.py: initial state fields and mutable isolation.
- tests/test_demo_view_models.py: structured answer preferred; legacy still works; partial is not completed; sanitizer remains effective.
- tests/test_runtime_resume.py: strict msgpack round-trip public_answer/status; finalized resume does not invoke tool/model again.
- tests/test_cli.py: stdout preserves exact Vietnamese Unicode without double encoding.
- tests/test_final_acceptance.py: synthesis response, bounded payload, canonical citations, no canary leakage, persisted structured answer and interruption/resume idempotency.

### Verification commands

~~~powershell
uv run pytest -q tests/test_decision.py tests/test_planner.py tests/test_agent_workflow.py tests/test_review_loop.py
uv run pytest -q tests/test_answer_synthesis.py tests/test_evidence.py tests/test_context_budget.py
uv run pytest -q tests/test_runtime_composition.py tests/test_state.py tests/test_demo_view_models.py tests/test_cli.py
uv run pytest -q tests/test_runtime_resume.py tests/test_final_acceptance.py
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node .gitnexus/run.cjs detect-changes --scope all --repo .
~~~

Các lệnh pytest/Ruff được công bố trong README và dependencies nằm trong pyproject.toml. [verified: README.md:399, pyproject.toml:29]

## 9. Risk and Impact Analysis

- CRITICAL — build_agent_workflow: direct dependent build_agent_runtime. Sai edge có thể loop, bỏ checkpoint hoặc lặp tool. Mitigation: pure classifier, exactly-one terminal review, recursion test và resume acceptance. [graph]
- CRITICAL — build_user_facing_answer: direct dependent render_user_answer. Lỗi citation/escaping lan tới CLI/demo. Mitigation: legacy adapter tách biệt, validate evidence indices trước public model. [graph]
- CRITICAL — render_user_answer: direct dependents synthesize_node và _answer_markdown. Mitigation: deterministic renderer và demo sanitizer regression. [graph]
- CRITICAL — create_default_agent_runtime: direct dependent open_default_agent_runtime. Thêm model dependency có thể đổi bind/call count. Mitigation: optional injection, composition tests, provider fallback. [graph]
- LOW — create_research_plan: indexed caller run_research_planner; runtime partial là dynamic caller source-verified. Mitigation: optional budget argument và exact payload tests. [graph] [verified]
- Checkpoint migration: model mới có thể bị strict msgpack từ chối. Mitigation: optional read và strict child-process round-trip. [inferred]
- Performance: thêm một LLM synthesis call mỗi finalized turn; context bounded và chỉ chạy một lần sau terminal review. [inferred]
- Concurrency: không thêm shared mutable global; delegation fan-in phải xong trước synthesis. [verified: tests/test_final_acceptance.py:371] [inferred]
- Security: evidence/prompt untrusted; draft không sở hữu URL/Markdown link; canonical allowlist + sanitizer vẫn là lớp cuối. [verified: src/mini_deerflow/demo/view_models.py:692] [inferred]
- Dirty worktree: nhiều file dự kiến sửa đã unstaged trước plan. Executor phải diff và tích hợp, không overwrite/revert. [verified: provenance manifest]

## 10. Files Expected to Change

| File | Symbols | Reason |
| ---- | ------- | ------ |
| src/mini_deerflow/answer_synthesis.py | new adapter/models | Structured synthesis, projection, validation, fallback. |
| src/mini_deerflow/evidence.py | AnswerClaim, UserFacingAnswer, build_user_facing_answer, render_user_answer | Claim citations, discrepancy, partial status, table, localization. |
| src/mini_deerflow/agent_workflow.py | build_agent_workflow, route_after_decision, budget_exhausted_node, review_node, synthesize_node | Budget routes, terminal review, latest-review synthesis. |
| src/mini_deerflow/decision.py | ActionContext, build_action_context | Dynamic allocation visible to selector. |
| src/mini_deerflow/planner.py | create_research_plan, prompt | Budget-aware plan. |
| src/mini_deerflow/review.py | ReviewContext, ReviewVerdict | Terminal/current unresolved contract. |
| src/mini_deerflow/llm_reviewer.py | LLMReviewer | Final-review prompt. |
| src/mini_deerflow/context_budget.py | projection helpers | Bounded synthesis context. |
| src/mini_deerflow/runtime.py | build_agent_runtime, create_default_agent_runtime | Inject synthesizer and planner limits. |
| src/mini_deerflow/state.py | AgentState, create_initial_state | Persist structured answer/status. |
| src/mini_deerflow/demo/view_models.py | _answer_markdown, project_demo_run | Structured answer and partial status. |
| src/mini_deerflow/cli.py | output boundary if test fails | Verify/preserve UTF-8. |
| tests/test_answer_synthesis.py | new | Synthesis contract. |
| tests/test_decision.py, tests/test_planner.py | existing | Allocation/planning budget. |
| tests/test_agent_workflow.py, tests/test_review_loop.py | existing | Routing/final review/no-loop. |
| tests/test_llm_reviewer.py, tests/test_context_budget.py | existing | Review snapshot/context bound. |
| tests/test_evidence.py, tests/test_demo_view_models.py, tests/test_cli.py | existing | Output/citation/table/Vietnamese/UI. |
| tests/test_runtime_composition.py, tests/test_state.py, tests/test_runtime_resume.py, tests/test_final_acceptance.py | existing | Composition, persistence, resume, E2E. |

## 11. Reusable Implementation Context

~~~json
{
  "implementation_context": {
    "task_summary": "Implement budget-aware finalization and evidence-grounded Vietnamese answer synthesis with claim citations, contradictions and comparison tables.",
    "acceptance_criteria": [
      "Budget exhaustion yields exactly one final review and a clearly partial answer without loops.",
      "Vietnamese comparative questions produce a direct Vietnamese answer and a useful comparison table.",
      "Every factual claim/table row cites only validated successful evidence; invented URLs never render.",
      "Contradictory evidence remains explicit instead of being silently selected or duplicated.",
      "Old and new checkpoints resume without repeating completed tool or synthesis work."
    ],
    "evidence_provenance": {
  "schema_version": 2,
  "head_commit": "2e0834abe4f985c58751be33d6967c0c113af32f",
  "generated_plan_path": "docs/plans/2026-09-27-gitnexus-plan-budget-aware-answer-synthesis.md",
  "global_dirty_digest": {
    "algorithm": "sha256",
    "canonicalization": "gitnexus-evidence-provenance-v2 NUL-framed UTF-8 records",
    "value": "ae300e2be90c7f880bd9bf9aff64c047147ec34e80ac8785f9e8fde1f0a4114b"
  },
  "cited_path_manifest": [
    {
      "path": "README.md",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:f49cb17639c3cad8d7b1a2dccb8dc14e91e8c983d5c652baff72108adb38c8e8",
      "index_digest": "sha256:f49cb17639c3cad8d7b1a2dccb8dc14e91e8c983d5c652baff72108adb38c8e8",
      "worktree_digest": "sha256:f49cb17639c3cad8d7b1a2dccb8dc14e91e8c983d5c652baff72108adb38c8e8",
      "untracked_digest": "absent"
    },
    {
      "path": "pyproject.toml",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:991a46c4010ea3457af1ac78fa5fdb7dc6f95a2f41dc84784de435499e567cbf",
      "index_digest": "sha256:991a46c4010ea3457af1ac78fa5fdb7dc6f95a2f41dc84784de435499e567cbf",
      "worktree_digest": "sha256:991a46c4010ea3457af1ac78fa5fdb7dc6f95a2f41dc84784de435499e567cbf",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/agent_workflow.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:e8d7d05a0e64e2858a4c2f8cbd09cf186ebee8662e5fa3cf10340d26f7e686da",
      "index_digest": "sha256:e8d7d05a0e64e2858a4c2f8cbd09cf186ebee8662e5fa3cf10340d26f7e686da",
      "worktree_digest": "sha256:e8d7d05a0e64e2858a4c2f8cbd09cf186ebee8662e5fa3cf10340d26f7e686da",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/answer_synthesis.py",
      "object_kind": {
        "head": "absent",
        "index": "absent",
        "worktree": "absent",
        "untracked": "absent"
      },
      "state": "absent",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "absent",
      "index_digest": "absent",
      "worktree_digest": "absent",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/cli.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:9ece1d9a30d23b3afca9fdd02a0296aeab7be7c83a5c05c33994b9bc32336980",
      "index_digest": "sha256:9ece1d9a30d23b3afca9fdd02a0296aeab7be7c83a5c05c33994b9bc32336980",
      "worktree_digest": "sha256:9ece1d9a30d23b3afca9fdd02a0296aeab7be7c83a5c05c33994b9bc32336980",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/context_budget.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:e42bc66617f1ac626f6d1bcd4bccf196a5e3a7fd8fd43b30ce91982c96c67c07",
      "index_digest": "sha256:e42bc66617f1ac626f6d1bcd4bccf196a5e3a7fd8fd43b30ce91982c96c67c07",
      "worktree_digest": "sha256:e42bc66617f1ac626f6d1bcd4bccf196a5e3a7fd8fd43b30ce91982c96c67c07",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/decision.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:7fdba387b090d835e89987e1b0990248ec2c01594884e596cd8d5da6618e77f3",
      "index_digest": "sha256:7fdba387b090d835e89987e1b0990248ec2c01594884e596cd8d5da6618e77f3",
      "worktree_digest": "sha256:7fdba387b090d835e89987e1b0990248ec2c01594884e596cd8d5da6618e77f3",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/demo/view_models.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:577363a5095c73841b24c58668b2222aa69f4f4596bfe2377d4bd81fff83a9c5",
      "index_digest": "sha256:577363a5095c73841b24c58668b2222aa69f4f4596bfe2377d4bd81fff83a9c5",
      "worktree_digest": "sha256:577363a5095c73841b24c58668b2222aa69f4f4596bfe2377d4bd81fff83a9c5",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/evidence.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:4097646f053727197c05519a901462fe7e4e52041e8b4d4e63d1713bcace47b9",
      "index_digest": "sha256:4097646f053727197c05519a901462fe7e4e52041e8b4d4e63d1713bcace47b9",
      "worktree_digest": "sha256:4097646f053727197c05519a901462fe7e4e52041e8b4d4e63d1713bcace47b9",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/llm_reviewer.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:dec8dffd80c2dc365e6306a7a8b80170738e10d6448b3782760cd1232e7910c7",
      "index_digest": "sha256:dec8dffd80c2dc365e6306a7a8b80170738e10d6448b3782760cd1232e7910c7",
      "worktree_digest": "sha256:dec8dffd80c2dc365e6306a7a8b80170738e10d6448b3782760cd1232e7910c7",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/planner.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:0930b8c0d77a180a774eb7730b5509b606a81eebf65b6ab9cf5c8d2621bd46c7",
      "index_digest": "sha256:0930b8c0d77a180a774eb7730b5509b606a81eebf65b6ab9cf5c8d2621bd46c7",
      "worktree_digest": "sha256:ad10dbe86c01cf8cae9b8e16296f21c24efdaf9757e6f4afc1bc5619750d0d95",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/review.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:73327dd5a292e20334c98ff41e848eb94ff3126ee6b7e65912c09a883909d9a9",
      "index_digest": "sha256:73327dd5a292e20334c98ff41e848eb94ff3126ee6b7e65912c09a883909d9a9",
      "worktree_digest": "sha256:73327dd5a292e20334c98ff41e848eb94ff3126ee6b7e65912c09a883909d9a9",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/runtime.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:32fd8b7eb1a4f17ce58bb221d64d80d422ec68a942b9f55712bfde3ff3a715e1",
      "index_digest": "sha256:32fd8b7eb1a4f17ce58bb221d64d80d422ec68a942b9f55712bfde3ff3a715e1",
      "worktree_digest": "sha256:32fd8b7eb1a4f17ce58bb221d64d80d422ec68a942b9f55712bfde3ff3a715e1",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/schemas.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:18c00f0a9c17af143e0c8b0386164890a66aa080c42cc0bebe867c644a539f5e",
      "index_digest": "sha256:18c00f0a9c17af143e0c8b0386164890a66aa080c42cc0bebe867c644a539f5e",
      "worktree_digest": "sha256:80aa6f9d55e1a397d741908e412a4d020f2be2a153373e3fdb6436a2241592b8",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/state.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:4689685a1355550c882057ba4ca849626fb53ecdf622ed598ef49644f0504e71",
      "index_digest": "sha256:4689685a1355550c882057ba4ca849626fb53ecdf622ed598ef49644f0504e71",
      "worktree_digest": "sha256:4689685a1355550c882057ba4ca849626fb53ecdf622ed598ef49644f0504e71",
      "untracked_digest": "absent"
    },
    {
      "path": "src/mini_deerflow/structured_output.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:cbfe58807d55f10e098495d1a95a9185e04c79f2575de30ef5efcfca5b4a86d8",
      "index_digest": "sha256:cbfe58807d55f10e098495d1a95a9185e04c79f2575de30ef5efcfca5b4a86d8",
      "worktree_digest": "sha256:cbfe58807d55f10e098495d1a95a9185e04c79f2575de30ef5efcfca5b4a86d8",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_agent_workflow.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:c442e5e93597bb3f1f44cd5f4569eef969ee7ac1f36935231ff080d5ae8c78c0",
      "index_digest": "sha256:c442e5e93597bb3f1f44cd5f4569eef969ee7ac1f36935231ff080d5ae8c78c0",
      "worktree_digest": "sha256:e5f0446fdcff5f9694c02613a3ca983a5ab57687f139f09b49c6cb8689fd47f9",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_answer_synthesis.py",
      "object_kind": {
        "head": "absent",
        "index": "absent",
        "worktree": "absent",
        "untracked": "absent"
      },
      "state": "absent",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "absent",
      "index_digest": "absent",
      "worktree_digest": "absent",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_cli.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:2398fd031a1e61739b53e435fe7b12eb8e26c0522bab5f046e71345fa50be83c",
      "index_digest": "sha256:2398fd031a1e61739b53e435fe7b12eb8e26c0522bab5f046e71345fa50be83c",
      "worktree_digest": "sha256:0dfb97b3fb3c286b49001a5fa137fbec23901915adeda34105a6ec4783ad844f",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_context_budget.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:1cdc5d9769225bba703b012e77416e8c2842f0080ab94e67479d384a65801cad",
      "index_digest": "sha256:1cdc5d9769225bba703b012e77416e8c2842f0080ab94e67479d384a65801cad",
      "worktree_digest": "sha256:1cdc5d9769225bba703b012e77416e8c2842f0080ab94e67479d384a65801cad",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_decision.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:107e920f389afed77a080664b4cc24469f157690868f9ea19675b472cc62ef59",
      "index_digest": "sha256:107e920f389afed77a080664b4cc24469f157690868f9ea19675b472cc62ef59",
      "worktree_digest": "sha256:ca57bf7b04026e2e4537694ac4b0ba0fdfbe5a112c54796def64ad389e8cb984",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_demo_view_models.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:9f7d611dcb2cc6593191dcc02f48d44fbc435b4fbd0201aa675cf553ab6e5bc2",
      "index_digest": "sha256:9f7d611dcb2cc6593191dcc02f48d44fbc435b4fbd0201aa675cf553ab6e5bc2",
      "worktree_digest": "sha256:9f7d611dcb2cc6593191dcc02f48d44fbc435b4fbd0201aa675cf553ab6e5bc2",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_evidence.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:7ae48bf246a33b37dad186b7d5b5cad626ee740fb4879371a6ecb6a03d7452ac",
      "index_digest": "sha256:7ae48bf246a33b37dad186b7d5b5cad626ee740fb4879371a6ecb6a03d7452ac",
      "worktree_digest": "sha256:7ae48bf246a33b37dad186b7d5b5cad626ee740fb4879371a6ecb6a03d7452ac",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_final_acceptance.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:8e63bf7b5cacd015b3a6d3c72fc1b60e3bd98b7e4bb947e0aad3c2c4f5892a1c",
      "index_digest": "sha256:8e63bf7b5cacd015b3a6d3c72fc1b60e3bd98b7e4bb947e0aad3c2c4f5892a1c",
      "worktree_digest": "sha256:8e63bf7b5cacd015b3a6d3c72fc1b60e3bd98b7e4bb947e0aad3c2c4f5892a1c",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_llm_reviewer.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:407833345a02cb784d5f5a80549f8812273ca315924c1c693966f7b13759a62f",
      "index_digest": "sha256:407833345a02cb784d5f5a80549f8812273ca315924c1c693966f7b13759a62f",
      "worktree_digest": "sha256:407833345a02cb784d5f5a80549f8812273ca315924c1c693966f7b13759a62f",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_planner.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:e4a903c9561cadc93dbfc23d8ea953148801322b139025b341e05f73542c1c7c",
      "index_digest": "sha256:e4a903c9561cadc93dbfc23d8ea953148801322b139025b341e05f73542c1c7c",
      "worktree_digest": "sha256:0264c57b1bcceedb8b129e1d879d99adf26dcd46b87708ec8b2ebfb91fe31117",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_review_loop.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:5975308dc72df1ab86c21a63967ba44ca28e9c4b7a06620ee02445a4ec119ae3",
      "index_digest": "sha256:5975308dc72df1ab86c21a63967ba44ca28e9c4b7a06620ee02445a4ec119ae3",
      "worktree_digest": "sha256:5975308dc72df1ab86c21a63967ba44ca28e9c4b7a06620ee02445a4ec119ae3",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_runtime_composition.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:bae44e54c23cee8bd42bd5b61347d22c921e94616aec58256485ec5cc121d141",
      "index_digest": "sha256:bae44e54c23cee8bd42bd5b61347d22c921e94616aec58256485ec5cc121d141",
      "worktree_digest": "sha256:30547a63d54c6905bb3ab6dd0d01c0dc80b3f9b25d2e09c9c5e3627ac0ee423b",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_runtime_resume.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "clean",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:446630a66c768d7d51b11d515e77ac068a596cc5501fbd8cbda2b42d72effa46",
      "index_digest": "sha256:446630a66c768d7d51b11d515e77ac068a596cc5501fbd8cbda2b42d72effa46",
      "worktree_digest": "sha256:446630a66c768d7d51b11d515e77ac068a596cc5501fbd8cbda2b42d72effa46",
      "untracked_digest": "absent"
    },
    {
      "path": "tests/test_state.py",
      "object_kind": {
        "head": "regular",
        "index": "regular",
        "worktree": "regular",
        "untracked": "absent"
      },
      "state": "unstaged",
      "rename_from": null,
      "rename_to": null,
      "head_digest": "sha256:bc3b5a6984dc27ccc4de985af46112bb055c99f0d8ecb9adbc9f758694d373ba",
      "index_digest": "sha256:bc3b5a6984dc27ccc4de985af46112bb055c99f0d8ecb9adbc9f758694d373ba",
      "worktree_digest": "sha256:2fb69cae001ca7e75b4f4de879b99b3f7896f07cfe1e3d735ac2049ac59c6437",
      "untracked_digest": "absent"
    }
  ]
},
    "primary_symbols": [
      {"symbol":"build_agent_workflow","file":"src/mini_deerflow/agent_workflow.py","lines":"144-1169","role":"State-machine routing, review and synthesis"},
      {"symbol":"build_user_facing_answer","file":"src/mini_deerflow/evidence.py","lines":"628-710","role":"Current deterministic public-answer builder"},
      {"symbol":"render_user_answer","file":"src/mini_deerflow/evidence.py","lines":"722-779","role":"Public Markdown renderer"},
      {"symbol":"create_default_agent_runtime","file":"src/mini_deerflow/runtime.py","lines":"693-807","role":"Default dependency composition"},
      {"symbol":"create_research_plan","file":"src/mini_deerflow/planner.py","lines":"72-139","role":"Initial structured planner"}
    ],
    "related_symbols": [
      {"symbol":"build_agent_runtime","relationship":"CALLS build_agent_workflow","relevance":"Direct depth-1 dependent"},
      {"symbol":"synthesize_node","relationship":"CALLS render_user_answer","relevance":"Final state mutation and artifact ordering"},
      {"symbol":"_answer_markdown","relationship":"CALLS render_user_answer","relevance":"Persisted demo projection"},
      {"symbol":"open_default_agent_runtime","relationship":"CALLS create_default_agent_runtime","relevance":"Default runtime lifecycle"},
      {"symbol":"build_action_context","relationship":"supplies budget fields","relevance":"Selector/runtime quota consistency"},
      {"symbol":"LLMReviewer","relationship":"established structured-output pattern","relevance":"Template for answer synthesis"}
    ],
    "execution_path": [
      "Runtime composes planner, selector, reviewer, replanner and answer synthesizer.",
      "Planner receives goal, tools, conversation and execution-budget metadata.",
      "Action context receives a dynamically allocated step quota derived from remaining total budget and remaining steps.",
      "Tool calls execute only when both hard and allocated quotas permit.",
      "Step quota exhaustion records a partial step and reviews before continuing; total exhaustion records a terminal reason.",
      "Terminal review runs exactly once and is forced to finish.",
      "Synthesis consumes projected evidence and the latest unresolved review snapshot.",
      "Evidence indices are validated and converted to canonical citations before deterministic rendering.",
      "Structured public answer and rendered final_answer are checkpointed; report/artifact follow."
    ],
    "pdg_constraints": [
      {
        "description":"Use one budget classifier for both the selector context and route decision.",
        "affected_statements":["src/mini_deerflow/agent_workflow.py:305","src/mini_deerflow/agent_workflow.py:306","src/mini_deerflow/agent_workflow.py:308"],
        "implementation_consequence":"No duplicated quota arithmetic in separate nodes."
      },
      {
        "description":"Do not flatten all historical review findings into public limitations.",
        "affected_statements":["src/mini_deerflow/agent_workflow.py:990","src/mini_deerflow/agent_workflow.py:1005","src/mini_deerflow/evidence.py:693"],
        "implementation_consequence":"Latest review is current public snapshot; full history stays audit-only."
      },
      {
        "description":"Validate public answer before report/artifact side effects.",
        "affected_statements":["src/mini_deerflow/agent_workflow.py:964"],
        "implementation_consequence":"Synthesis failure uses safe fallback and cannot leave a half-persisted public answer."
      }
    ],
    "architectural_patterns": [
      {
        "pattern":"Structured output with bounded retries",
        "example_location":"src/mini_deerflow/llm_reviewer.py:LLMReviewer",
        "usage_guidance":"Mirror validation/error handling; do not expose chain-of-thought."
      },
      {
        "pattern":"Projection-only context compaction",
        "example_location":"src/mini_deerflow/context_budget.py",
        "usage_guidance":"Never mutate full checkpoint evidence to fit a prompt."
      },
      {
        "pattern":"Canonical citation allowlist",
        "example_location":"src/mini_deerflow/evidence.py and src/mini_deerflow/demo/view_models.py",
        "usage_guidance":"Model emits evidence indices; runtime owns URLs and Markdown links."
      },
      {
        "pattern":"Backward-compatible persisted projection",
        "example_location":"src/mini_deerflow/demo/view_models.py:_answer_markdown",
        "usage_guidance":"Prefer structured new state, preserve explicit legacy fallback."
      }
    ],
    "files_to_modify": [
      {"file":"src/mini_deerflow/answer_synthesis.py","symbols":["new structured adapter/models"],"intended_change":"Add bounded structured synthesis and evidence-index validation."},
      {"file":"src/mini_deerflow/evidence.py","symbols":["AnswerClaim","UserFacingAnswer","build_user_facing_answer","render_user_answer"],"intended_change":"Claim citations, contradictions, tables and localized deterministic rendering."},
      {"file":"src/mini_deerflow/agent_workflow.py","symbols":["build_agent_workflow","route_after_decision","budget_exhausted_node","review_node","synthesize_node"],"intended_change":"Budget classification, terminal review and structured synthesis state."},
      {"file":"src/mini_deerflow/decision.py","symbols":["ActionContext","build_action_context"],"intended_change":"Expose dynamic allocated quota."},
      {"file":"src/mini_deerflow/planner.py","symbols":["create_research_plan","PLANNER_SYSTEM_PROMPT"],"intended_change":"Budget-aware planning context."},
      {"file":"src/mini_deerflow/review.py","symbols":["ReviewContext","ReviewVerdict"],"intended_change":"Terminal metadata/current unresolved snapshot."},
      {"file":"src/mini_deerflow/llm_reviewer.py","symbols":["LLMReviewer"],"intended_change":"Final-review prompt contract."},
      {"file":"src/mini_deerflow/context_budget.py","symbols":["projection helpers"],"intended_change":"Bound synthesis payload."},
      {"file":"src/mini_deerflow/runtime.py","symbols":["build_agent_runtime","create_default_agent_runtime"],"intended_change":"Compose synthesizer and planner limits."},
      {"file":"src/mini_deerflow/state.py","symbols":["AgentState","create_initial_state"],"intended_change":"Persist public answer and completion metadata."},
      {"file":"src/mini_deerflow/demo/view_models.py","symbols":["_answer_markdown","project_demo_run"],"intended_change":"Prefer structured answer and represent partial status."},
      {"file":"src/mini_deerflow/cli.py","symbols":["output boundary"],"intended_change":"Only change if Unicode regression test locates a CLI fault."}
    ],
    "tests": [
      {"file":"tests/test_answer_synthesis.py","scenarios":["Vietnamese goal → Vietnamese structured answer","conflicting evidence → cited discrepancy","invented evidence index → rejected","comparison → cited escaped table","provider failure → safe fallback"]},
      {"file":"tests/test_decision.py","scenarios":["remaining budget/steps → fair quota","under-use → later quota expansion","zero total → zero quota"]},
      {"file":"tests/test_agent_workflow.py","scenarios":["step quota exhausted → partial advance","total exhausted → terminal partial","all paths → hard counters respected"]},
      {"file":"tests/test_review_loop.py","scenarios":["terminal exhaustion → exactly one review","terminal continue/replan → forced finish","review history → latest public/full audit"]},
      {"file":"tests/test_evidence.py","scenarios":["claim citations → no citation spam","comparison rows → per-row markers","legacy execution text → filtered"]},
      {"file":"tests/test_runtime_resume.py","scenarios":["new structured state → strict round-trip","finalized resume → no duplicate synthesis/tools","old checkpoint → legacy fallback"]},
      {"file":"tests/test_final_acceptance.py","scenarios":["Vietnamese comparison + contradiction + low budget → safe useful persisted answer"]}
    ],
    "verification_commands": [
      "uv run pytest -q tests/test_decision.py tests/test_planner.py tests/test_agent_workflow.py tests/test_review_loop.py",
      "uv run pytest -q tests/test_answer_synthesis.py tests/test_evidence.py tests/test_context_budget.py",
      "uv run pytest -q tests/test_runtime_composition.py tests/test_state.py tests/test_demo_view_models.py tests/test_cli.py",
      "uv run pytest -q tests/test_runtime_resume.py tests/test_final_acceptance.py",
      "uv run pytest -q",
      "uv run ruff check .",
      "uv run ruff format --check .",
      "node .gitnexus/run.cjs detect-changes --scope all --repo ."
    ],
    "risks": [
      "CRITICAL graph centrality for workflow, renderer and default runtime composition.",
      "Checkpoint compatibility and strict msgpack registration.",
      "One additional model call and bounded synthesis context.",
      "Unsafe Markdown/URL leakage from untrusted model/evidence text.",
      "Pre-existing unstaged edits in files the implementation will touch."
    ],
    "assumptions": [
      "Re-check the current diff before editing planner.py, schemas.py and dirty tests; integrate rather than overwrite.",
      "Verify the fair-share quota formula against RuntimeLimits where max_total_tool_calls is lower than the minimum three plan steps.",
      "Run a Unicode stdout test before changing cli.py; the current source already configures UTF-8.",
      "Re-run GitNexus impact because the index/worktree may drift after this plan."
    ],
    "open_questions": [
      "Should output-language support remain vi/en for this increment or accept a caller-supplied BCP-47 language?",
      "Should a comparison table be mandatory for every comparative goal or only when at least two evidence-backed criteria exist?",
      "If total budget is below plan-step count, should zero-quota trailing steps be auto-closed or should planner validation reject the plan?"
    ],
    "avoid": [
      "Do not repeat full repository discovery.",
      "Do not replace established structured-output/context-budget patterns without evidence.",
      "Do not allow the model to emit trusted URLs or raw Markdown links.",
      "Do not expose full historical review findings as current limitations.",
      "Do not route terminal budget exhaustion back to tool execution or replanning.",
      "Do not mutate or revert pre-existing unstaged user changes.",
      "Do not change PlanStep schema in this scope."
    ]
  }
}
~~~

## 12. Assumptions and Open Questions

### Assumptions

- [assumed] Phạm vi đầu tiên hỗ trợ language contract vi/en; executor phải xác nhận trước khi khoá schema.
- [assumed] Bảng chỉ sinh khi có ít nhất hai đối tượng và hai tiêu chí có evidence; nếu không, renderer dùng claim list và limitation.
- [assumed] Dynamic quota ceil(remaining_total/remaining_steps) là baseline; executor phải test trường hợp total nhỏ hơn ba bước trước khi chốt auto-close behavior.
- [verified] CLI source đã cấu hình UTF-8; chỉ sửa sau khi regression test tái hiện mojibake tại CLI.
- [verified] Worktree có thay đổi unstaged ở planner.py, schemas.py và một số test; chúng thuộc người dùng và phải được bảo toàn.

### Open questions

1. Có cần hỗ trợ ngôn ngữ ngoài tiếng Việt/Anh ngay trong increment này không?
2. Bảng so sánh có bắt buộc khi evidence chỉ đủ cho một tiêu chí hay nên trả danh sách + limitation?
3. Với max_total_tool_calls thấp hơn số bước tối thiểu, policy mong muốn là auto-close bước không quota hay reject plan sớm?

### Explicitly deferred

- Budget riêng cho model calls/token cost.
- Semantic entailment checker độc lập ngoài structured synthesizer.
- Thay đổi schema PlanStep để lưu expected_tool_calls.
- Tự động dịch toàn bộ research_report nội bộ; yêu cầu hiện tại chỉ bắt buộc public answer tiếng Việt.

## 13. Definition of Done

- Planner nhìn thấy execution budget và tạo plan ưu tiên trong giới hạn, không đổi PlanStep schema.
- Một bước không thể chiếm hết budget dự kiến của các bước sau; per-step và total exhaustion có route khác nhau.
- Total exhaustion thực hiện đúng một final review, không replan/execute thêm, và answer mang partial status/reason.
- Public synthesis dùng latest unresolved review snapshot; report vẫn giữ full history.
- Vietnamese comparative goal tạo kết luận trực tiếp, bảng so sánh khi đủ evidence, và không lộ progress/trace text.
- Mỗi factual claim, discrepancy và table row chỉ trỏ tới canonical successful evidence; invented citation bị loại.
- Demo/CLI hiển thị Unicode đúng và demo không gọi partial answer là completed.
- Checkpoint cũ/mới resume idempotently; không lặp tool hay synthesis.
- Targeted tests, full pytest, Ruff check và Ruff format check đều pass.
- GitNexus detect-changes chạy ở tip; mọi HIGH/CRITICAL impact được báo và có regression coverage.
