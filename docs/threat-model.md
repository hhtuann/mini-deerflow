# Mini DeerFlow Day 13 Threat Model

## Scope and security claim

Mini DeerFlow is a single-user learning MVP. Day 13 adds a typed public-web
target boundary, redacted structured traces, and deterministic contract
evaluation. It does not make the application production-ready and does not add
authentication, tenant isolation, deployment controls, a sandbox, or arbitrary
code execution.

The protected assets are API credentials, authorization headers, local
workspace files, checkpoint data, final artifacts, evidence provenance, and
the integrity of execution budgets. Goals, model output, search results,
fetched pages, branch summaries, filenames, URLs, and provider payloads are
untrusted.

## Trust boundaries

| Boundary | Untrusted input | Enforced control |
| --- | --- | --- |
| Model to action | tool name and arguments | typed action schema, registry allowlist, budgets |
| Web fetch | URL, DNS answers, redirect target | public HTTP(S) validator before provider call; final/declared redirects revalidated before content acceptance |
| Provider/transport | status, body, exception | size limits and static error normalization; bodies and exception chains are not diagnostics |
| Evidence to citation | model-authored URL | canonical membership in successful evidence only |
| Workspace | model-authored path/content | relative-path, traversal, link/junction, size, and explicit write-capability checks |
| Delegation | tasks and branch results | depth one, web-only registry, aggregate admission, deterministic fan-in, parent revalidation |
| Context projection | accumulated untrusted content | deterministic compaction or refusal before an LLM seam |
| Trace sink | execution events | closed typed schema containing counts and controlled codes, never payload content |
| Checkpoint/resume | persisted workflow state | typed serializer allowlist and stable thread identity |

## Web URL policy

`PublicWebTargetValidator` accepts only syntactically valid `http` or `https`
URLs with no userinfo. It rejects `localhost`, `.localhost`, `.local`, IPv6
zone identifiers, and IP literals that are loopback, private, link-local,
multicast, unspecified, reserved, or otherwise non-global.

For a hostname, the injected resolver must return at least one parseable IP
address and every answer must pass the same public-address checks. Mixed
public/private results are denied. Resolution exceptions and empty or malformed
answer sets become controlled safety denials without including the hostname or
resolver exception. Tests and evaluations inject fixed resolvers; they never
perform DNS or network calls.

`WebFetchTool` validates the requested target before invoking its provider. A
provider result is not accepted until the final URL and every redirect target
the provider reports have been validated. Direct HTTP provider implementations
must disable automatic redirects or validate every `Location` before following
it. The current Jina adapter talks only to a fixed Jina endpoint and can observe
the requested and final URL, but the remote Reader controls intermediate
redirect following. Binding the validated DNS addresses to a direct transport,
protecting against DNS rebinding, and independently observing every remote
redirect hop remain production hardening work.

## Diagnostics and trace policy

Errors exposed by web tools contain a static message plus controlled category
and code. Safety denial, provider failure, and transport failure are distinct.
The tool runner logs only tool identity and exception class; it does not attach
tracebacks or exception messages. The CLI renders controlled messages rather
than arbitrary validation or runtime exception text.

Execution traces may contain run/thread IDs, node and phase, tool identity,
route, budgets, durations, context projection counters, branch/accounting
counts, evidence/citation/artifact counts, and controlled error category/code.
The schema has no fields for goals, prompts, queries, URLs, headers, response
bodies, excerpts, findings, exception text, or raw payloads. Tracing is opt-in
on the CLI and writes JSON Lines to stderr; it never creates a trace file by
default.

## Principal threats and current controls

| Threat | Current MVP control | Residual risk |
| --- | --- | --- |
| SSRF through a model URL | typed pre-provider public-target validation and all-address DNS policy | DNS rebinding, proxy behavior, and incomplete visibility into remote redirect hops |
| Prompt injection in pages/files | content remains untrusted data; capability and citation checks are deterministic | a model may still produce a poor or misleading summary |
| Secret leakage through failures | static provider/tool/CLI errors, no response bodies or exception chains, closed trace schema | third-party SDK logging and process-level telemetry need deployment review |
| Invented citations | citation URLs must belong to successful canonical evidence; renderer repeats the check | membership does not prove truth or claim-level entailment |
| Workspace escape | explicit write capability plus path/link/size checks | no multi-user isolation or OS sandbox |
| Delegation amplification | depth one, 2–3 branches, aggregate reservation, bounded concurrency and timeout | external effects before a checkpoint are not exactly once |
| Oversized context | deterministic projection/compaction, then refusal | character estimates are not exact tokenizer accounting |
| Checkpoint replay | completed checkpointed nodes resume without duplicate completed work | crash-before-checkpoint external effects can repeat |

## Explicitly deferred

- No shell tool, code-execution tool, browser automation, or sandbox container.
- No alternate transport or URL encoding intended to bypass the web policy.
- No production egress proxy, DNS pinning, rebinding defense, redirect-hop
  enforcement, TLS policy, content malware scanning, or domain reputation list.
- No authentication, authorization, multi-tenant isolation, encrypted database,
  retention policy, distributed tracing backend, or production deployment.
- No factual-answer quality, groundedness-entailment, calibration, live latency,
  or live cost benchmark.
