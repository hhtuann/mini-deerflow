# Phase 1: Context Engineering and Skills

This is the canonical user guide to the current Phase 1 implementation. The
canonical graph is `src/mini_deerflow/agent_workflow.py`, composed by `runtime.py`.
Phase 1 adds explicit repository-local skills and bounded stage-specific context
projections to that graph; it does not replace the workflow or tool boundary.

**Acceptance status:** Phase 0 mentor acceptance remains pending. Phase 1 offline
demonstrations and deterministic tests exercise integration contracts, not live
LLM research quality. The local verification snapshot is recorded below; no live
browser or live model validation is claimed.

## Choose a skill for one turn

There are three bundled skills under the checkout's `skills/` directory:

| Exact ID | Intended guidance | Declared conflicts |
| --- | --- | --- |
| `research-brief` | Concise question, findings, supporting sources, uncertainties | `deep-research` |
| `deep-research` | Subquestions, independent sources, contradictions, detailed findings | `research-brief` |
| `source-comparison` | Compare author/date/scope, claims, agreements and disagreements | None |

The default skill for **every new turn is `None`**, including follow-ups on a
previously skilled thread. No goal-text matching, tool result, conversation
message, or model decision activates a skill automatically. Selecting a skill
provides optional research guidance; it grants no tools, permissions, execution
budget, or guaranteed answer format.

### Python API

These are in-process APIs, not HTTP endpoints. Given a composed `AgentRuntime`:

```python
first = await runtime.run(
    "Compare the available evidence for these research approaches.",
    thread_id="skills-example",
    turn_id="first",
    skill_id="source-comparison",
)
second = await runtime.continue_thread(
    "skills-example",
    "Summarize the remaining uncertainties.",
    turn_id="second",
)  # skill_id defaults to None; the first turn's skill is not inherited.
resumed = await runtime.resume(thread_id="skills-example")
```

`continue_thread` requires the conversation repository; durable resume requires
the checkpointer. `build_agent_runtime`, `create_default_agent_runtime`, and
`open_default_agent_runtime` accept `skills_registry` and `context_budget`.
Use `SkillsRegistry(root=trusted_root)` for an explicitly configured local root.
The runtime API selects at most one skill per turn. The registry also offers
`activate_many`, which sorts IDs, rejects duplicates, and rejects a declared
conflict from either side; it is not a multi-skill turn API. Reusing a turn ID
with a different skill selection is an idempotency conflict. The CLI currently
has no skill-selection flag; use the Python API or UI.

With the durable conversation repository, a matching completed-turn duplicate
`run` or `continue_thread` returns its stored checkpoint before skill
reactivation. Replay therefore does not depend on the selected file still being
available or unchanged. Goal and skill-selection conflicts are still rejected.

### Streamlit

Use **Skill for next turn** in the sidebar. Its initial value is **None**, it
shows the selected entry's description/applicability, and it is disabled while
a job is active. Submission forwards `skill_id` through `RunDemoCommand` or
`ContinueDemoCommand` and resets the selector for the next submission. Historical
turns and Agent details show their own active skill ID, including after reload.
A catalog discovery error produces **Skill catalog unavailable**.

The **Offline walkthrough** is the Phase 0 fixed scripted scenario: selecting a
skill does not make its fixture answers arbitrary skill-driven research. Use the
separate Phase 1 command below to inspect actual production request assembly.

## Skill files: discovery is not activation

Each direct child must be `<root>/<exact-id>/SKILL.md`. Files are UTF-8 with
**JSON frontmatter**, not YAML, between lines containing exactly `---`:

```text
---
{"id":"source-comparison","description":"Compare source claims, provenance, and disagreements.","applicability":"Use when comparing source accounts.","version":"1.0.0"}
---
Compare available evidence and preserve unresolved uncertainty.
```

Required metadata: `id`, `description`, `applicability`, `version`. Optional
`conflicts` is a JSON array of exact IDs. Unknown or duplicate keys are rejected.
IDs must match their directory, use lowercase hyphenated identifiers, and avoid
reserved names; version is a numeric `major.minor.patch` string.

`SkillsRegistry.discover()` reads bounded frontmatter only, never instruction
bodies. It returns sorted immutable `SkillMetadata` entries; their `fingerprint`
hashes canonical metadata, **not the full file**. Applicability is descriptive
catalog metadata, not an activation rule. A missing root gives an empty catalog;
malformed entries fail the entire discovery rather than being silently skipped.

`activate(exact_id)` reads the selected full file, checks its metadata against
discovery, and returns an immutable `SkillContext` with ID, optional description,
instruction body, and SHA-256 of the complete file snapshot. Its fingerprint is
different in meaning from the discovery fingerprint. `expected_fingerprint`, if
supplied, must match that full-file hash. Metadata changes require explicit
rediscovery; body-only changes can be picked up by a new activation, while an
existing turn retains its pinned snapshot.

Default registry limits are 128 entries, 4,096 frontmatter bytes, 65,536 file
bytes, and 32,768 instruction bytes. Empty/NUL-containing or invalid UTF-8 bodies
are rejected. Paths, traversal, symlinks, junctions and reparse points are not
valid activation IDs or accepted registry entries. The root is trusted caller
configuration; checks assume no hostile concurrent local filesystem mutation.

## What reaches each model stage

Stage-specific projections keep unrelated state out of the request. All five
parent stages can receive the selected skill's projected ID/fingerprint/body;
the body may be bounded or omitted by the shared policy.

| Stage | Dynamic inputs beyond the optional skill |
| --- | --- |
| Planning (`PlanningContext`) | Goal, turn ID/sequence when present, available tools, bounded prior conversation, execution limits |
| Action selection (`ActionContext`) | Goal/current step, turn ID/sequence when present, tools, completed summaries, conversation, projected observations/evidence, hard and allocated remaining tool budgets |
| Review (`ReviewContext`) | Goal, remaining steps, projected summaries/findings/evidence, derived limitations, remaining tool/replan budgets, finalization reason |
| Replan (`ReplanRequest`) | Goal, triggering review, completed summaries, replaced steps, tools, remaining budgets and replacement-step bounds |
| Answer synthesis (`AnswerSynthesisContext`) | Goal, language, completion/finalization status, projected findings/evidence and limitations |

Replanning receives review conclusions and summaries rather than a separate full
evidence catalog. Review and synthesis receive bounded evidence with retained
canonical URLs and provenance; projection never invents sources. Prior-turn
assistant prose/citations are untrusted conversation context, not current-turn
evidence or citation membership. Current claims still require current-turn tool
evidence. Synthesis validates evidence indices against its projected evidence
and maps accepted indices to runtime-owned source URLs.

The production planner accepts the context keywords. Injected legacy planners
receive only keywords supported by their signature, so a goal-only injected
planner does not demonstrate skill propagation. The parent's pinned context
budget is explicitly propagated through delegation to compatible researcher
seams and the default bounded researcher, so resumed child projections use that
budget rather than a newly configured default. Delegated researchers retain
their scoped context; the delegation contract does **not** propagate the
parent's `active_skill`. Do not assume child skill inheritance.

## Budgets, priorities, and accounting

`ContextBudget` is separate from tool-call, replan, recursion and session limits.
Its defaults are `max_total_chars=60_000`, `max_item_chars=4_000`,
`retained_recent_items=30`, and `max_excerpt_chars=1_500`. Positive integer limits
are validated; excerpts cannot exceed item size, and the total must allow an
item, an excerpt, and the 1,024-character fixed reserve.

`render_llm_payload` uses indented JSON with `ensure_ascii=False`.
`payload_chars` measures the exact serialized **dynamic JSON context**, including
escaping, structural fields and serialized projection metadata where present.
Static trusted system instructions (including `SKILLS_SYSTEM_GUIDANCE`), outer
message/tag wrappers, provider schemas/overhead, and model output are outside
this character budget. This is not all-in prompt-token or cost accounting.
`estimated_tokens = ceil(chars / 4)` is an approximate reporting heuristic, not
an exact tokenizer count or a guaranteed upper bound for every language/model.

Initial per-collection projections bound recent items/excerpts without mutating
durable state. A skill's LLM-only body is capped at 2,000 characters with an
explicit truncation marker, retaining its ID and full-file fingerprint. If the
assembled payload still exceeds the total, pressure is applied in this order,
only as far as necessary:

1. Omit optional skill instructions; retain skill identity/fingerprint.
2. Drop whole oldest conversation turns, preserving retained user/assistant pairs.
3. Remove successful raw observations already represented by retained evidence,
   matched by step/call/tool/delegation/branch provenance, even if newest.
4. Drop oldest evidence, potentially all projected evidence.
5. Drop oldest remaining observations, keeping the newest one.
6. Replace old summaries and findings with identity markers, then drop older
   entries if needed.
7. Drop older limitations, then bound the newest limitation.
8. Strip verbose tool input schemas while retaining names/descriptions.
9. Replace old replaced-plan objectives/criteria with step identity markers.

Mandatory goal, current-step identity and remaining-budget fields are not
shortened to force a fit. If the mandatory payload still cannot fit,
`ContextBudgetExceededError` is raised before an oversized request is sent.
The original state, evidence and pinned skill body remain available for
checkpointing, audit and report rendering; compaction affects projections only.

`ProjectionMetadata` reports omitted/truncated/retained item counts, payload and
budget characters, approximate tokens, skill ID/omission and policy version
(`phase1-v1`). Planning computes metrics but excludes `context_projection` from
its serialized wire shape. The planner's `on_context_projection` callback
receives fitted metadata before model setup/invocation; the production graph
wires it to a redacted `context_budget` event for `planner`. Action selection,
review, replan and synthesis also emit these events. Legacy injected planners
that do not accept/call the observer do not provide planning projection events.
UI Trace projects scalar metrics and the active ID, not instruction bodies or
raw context.

## Durable turn pins and resume

New `AgentState` carries `active_skill`, `context_budget`, and
`context_policy_version`. The SQLite checkpoint serializer allowlists the skill
and budget domain types. Every graph node checks the pinned policy version
before running, including resumed nodes; an incompatible explicit version
refuses execution rather than silently applying a new policy.

The conversation ledger stores the same snapshot in `TurnRecord.run_context_json`
when reserving a turn, before graph invocation. This **reservation-gap pin**
contains the full activated body/fingerprint, budget values and policy version.
If interruption happens before the first checkpoint, `resume` reconstructs
initial state from that durable JSON, checking the policy version first. If a
checkpoint exists, it owns the interrupted execution state. Neither path needs
to reactivate the current skill file to recover the pinned body. Editing the
file or configuring a different context budget affects new turns, not those pins.

Legacy records without the optional pin fields use compatibility defaults;
they cannot retroactively provide a historical skill/body pin. A completed
resume loads the completed turn; it does not start a new turn. Checkpoint
continuity does not provide exactly-once external effects across a crash between
an effect and its checkpoint. Reopening storage in offline scenarios is not a
claim of separately validated OS-process recovery.

## Reproduce offline checks

Run from the repository root after dependencies have been provisioned (`uv sync`
during setup). `--offline --no-sync` uses the existing environment without network
resolution or synchronization. These commands do not require live credentials:

```powershell
uv run --offline --no-sync python -m mini_deerflow.demo.phase1_offline
uv run --offline --no-sync pytest -q -rs -p no:cacheprovider tests/test_skills.py tests/test_phase1_context.py tests/test_phase1_runtime.py tests/test_phase1_delegation.py --basetemp "C:\Users\GIGABYTE\AppData\Local\Temp\opencode\p1t"
uv run --offline --no-sync pytest -q -rs -p no:cacheprovider --basetemp "C:\Users\GIGABYTE\AppData\Local\Temp\opencode\p1t"
uv run --offline --no-sync ruff check . --no-cache
uv run --offline --no-sync ruff format --check . --no-cache
uv lock --check --offline
```

Pytest recreates its basetemp directory; reserve that path for these checks and
run the commands sequentially. The Phase 1 demo uses fresh temporary storage,
the production runtime factory/graph and scripted external seams. It first runs
an unskilled baseline on a fresh thread with the **same goal** as the skilled
run, then interrupts/reopens/resumes the skilled run and continues with bounded
history and no implicit skill. Its dynamic context budget is **7,000 characters**;
it reuses Phase 0 fixtures but not the Phase 0 walkthrough's total context limit.
Inspect actual structured-model request measurements, continuity flags and
compaction diagnostics; output sizes and counts are observations, not API guarantees.

### Local verification snapshot

Verified locally on Windows Python **3.12.14** using the installed environment,
without installs. Full pytest reported **765 passed, 6 skipped,
223 subtests**; the deterministic evaluator passed **9 cases / 36 invariants**.
Lint, format, lock and diff checks passed. The Phase 1 demo captured **37
requests**, a maximum dynamic payload of **6,961 characters**, **8 compacted
requests**, and **15 passing boolean checks**. Snapshot counts are not guarantees
for future runs.

For the existing deterministic evaluator and quality commands, see
[README: Testing and Evaluation](../README.md#testing-and-evaluation).
`evals/run_evals.py` reads `evals/dataset.json` and runs its declared pytest cases
in subprocesses with per-case temporary directories; it is not a live LLM judge
or a substitute for the Phase 1 tests. Retain actual exit statuses and reports
when reproducing the verification snapshot.

## Limitations and trust

- The bundled registry is source-checkout-local:
  `default_skills_root()` resolves `Path(__file__).resolve().parents[2] / "skills"`.
  The root-level files are not an installed external skill registry; wheel
  distribution/discovery of them is not established by `pyproject.toml`.
- Static trust guidance makes skills subordinate to system/developer rules, user
  intent, existing permissions and execution limits. It does not solve prompt
  injection or semantically filter malicious instruction bodies. Hashes and
  declared conflicts provide integrity/selection checks, not content safety.
- There is no automatic activation, arbitrary skill execution, child inheritance,
  semantic memory/compression, exact token metering or all-in token budget.
- Offline request assembly and scripted behavior do not validate live model
  adherence, factual quality, cost or latency. No live browser or live model
  validation was performed in the reported checks. Phase 0 mentor acceptance
  remains pending.
