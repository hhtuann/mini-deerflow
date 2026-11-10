# Document Q&A — D4 (Issue #50)

Local synthetic-fixture scope only. Trusted single-user loopback; opaque workspace
IDs are not authentication. D1 retention/real-data approvals remain pending.
D6 owns document UI; D4 adds an API and opt-in agent tool, not a second chat UI.

## API

`POST /api/{mode}/workspaces/{workspace_id}/questions` (`live` or `offline`):

```json
{"question":"What is the project budget?","document_ids":["doc-example"],"skill_id":"source-comparison"}
```

`document_ids` omitted means all currently active workspace documents. Empty,
duplicate, deleted or foreign IDs are rejected, never silently broadened. Sources
without completed current extraction remain in the version snapshot but supply no
evidence. `question` obeys the configured D3 query-character cap (default 400).
Optional `previous_answer` must be the prior response's compact `continuation`
object (`workspace_id`, previous `question`, SHA-256 `source_fingerprint`), not
the full answer. It remains bounded even when extracts exceed the 16 KiB request
boundary or many documents are selected. The fingerprint summarizes sorted
document/version pairs, is change-detection metadata only, and confers no source
authority. Explicit short followups such as `And now?` reuse the prior question
for retrieval; new questions take precedence. Clients retain this continuation;
there is no additional persistent question ledger. Claims never become evidence.

Response: `workspace_id`, `question`, `status` (`answered`/`abstained`), `claims`,
`limitations`, `source_snapshot`, `source_changes`, `synthesis_status`,
`context_projection`, `continuation`. Every claim has exact source text and citations containing
`document_id`, `version`, `chunk_id`, typed `locator`, and supporting `quote`.
Reuse the D3 chunk GET endpoint with explicit `?version=` to inspect citations.

Errors use closed prose: 404 missing workspace; 422 invalid question/scope/skill
or cross-workspace prior context; 409 operation busy or changed/unavailable
citation; 503 retrieval deadline; inherited 403 Origin and 413 body boundaries.
The existing single-operation lock serializes document HTTP mutations and answers.

## Grounding and honest limits

Both modes default to credential-free **extractive** answers. Lexical overlap is
not semantic support: extracts are labeled as source statements, not an inferred
answer or externally verified fact. No hits, unextracted latest revisions, or no
budget-fitting verified evidence produce abstention. Multiple sources remain
separate with an explicit inability to reconcile possible disagreements; the
system does not claim universal semantic contradiction detection or choose a
winner. Citation provenance is not factual truth.

Only selected current evidence can authorize citations. Runtime maps provider
1-based evidence indices to server-owned document/version/chunk/locator tuples,
requires exact quotes from persisted text and claim text within every quoted
passage, and re-resolves all citations after synthesis. Fabricated indices,
anchors, locators, quotes, unsupported text, stale versions and removed sources
cannot become returned claims. Whole chunks are dropped under pressure; no source
spans or locators are invented to fit a budget. Shared `ContextBudget`, exact JSON
rendering and `ProjectionMetadata` bound the full dynamic envelope, including
metadata, question, optional skill and prior-question context.

## Agent/provider compatibility

`DocumentQAService` accepts the existing retriever protocol, `ContextBudget`,
`SkillContext` and `TraceSink`. Existing skills are explicit subordinate guidance,
never capabilities. Trace events contain counts/status only, not question or
source content. `tools.documents.DocumentQuestionTool` implements the existing
`Tool` protocol and registers in `ToolRegistry`; its caller pins workspace and
selected IDs, while model input exposes only `question`. Document evidence is not
coerced into web evidence or fabricated URLs. Existing web/research/slide workflows
retain their contracts and do not gain document access implicitly.

Optional `LLMDocumentQASynthesizer` adapts an explicitly supplied model through the
repository's structured-output seam. No model or credentials are resolved by app
defaults. Trusted system guidance is separate from JSON document data; the adapter
binds no tools. Source instructions cannot change selected scope, authorize tools,
or mint citations. Provider drafts remain exact-extractive for enforceable claim
membership, not unrestricted paraphrase generation. Service timeout defaults to
30 seconds; cancellation propagates. Provider errors or invalid drafts produce
honestly labeled verified-extract fallback (`provider_failed` / `provider_rejected`),
not synthetic provider success. Actual provider behavior is not verified by fixtures.

## Verification

Synthetic regressions: `tests/test_document_qa.py`,
`tests/test_api_document_qa.py`, `tests/test_document_qa_tool.py`.
Real-local-FastAPI scenario: `frontend/e2e/document-qa.spec.ts` (upload/extract,
answer, inspect citation, revise, followup, stale citation, abstention). No paid
providers, real files or confidential content are required.
