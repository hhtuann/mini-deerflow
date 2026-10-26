# Phase 3 · Task 3.4 — Deployment/Security Readiness Gate

Issue [#21](https://github.com/hhtuann/mini-deerflow/issues/21) · [Phase 3 roadmap #16](https://github.com/hhtuann/mini-deerflow/issues/16)

## Security decision / approved scope

**Deployment decision: KEEP LOCAL-ONLY.** This repository implements a **single-user, local loopback** React UI + FastAPI backend, backed by SQLite and local files, with real paid LLM/provider access when configured. **There is no user authentication, authorization, tenant isolation, durable job queue, secure public ingress or operator-managed key service**. Do **not** deploy it to a public interface, network-shared container, hosted preview, tunnel, exposed development proxy, or shared machine merely by changing the Uvicorn/Vite host bind. A public service needs a separately approved architecture and security scope (Task 3.4 does *not* add auth or claim production readiness).

Automated security checks and real existing HTTP tests are useful safety nets but **not a security audit**, vulnerability scan, penetration test, privacy-impact assessment, actual backup restore, or proof of safe operation on a hostile multi-user workstation. Keep production release **BLOCKED**.

## Release-readiness matrix (explicit gates)

| Target | Decision | Evidence/remaining blockers |
| --- | --- | --- |
| **Single-user trusted loopback development** | **READY** if static gate and normal CI PASS | Uvicorn `127.0.0.1:8000`; Vite `127.0.0.1:5173`; explicit origin and host restrictions; bounded request body; local-only secrets; single worker; no provider key in frontend |
| **Local pilot with real user data** | **BLOCKED** until operator checks | Need backup *and restore* rehearsal for SQLite + workspace together, tested provider budget/rotation and incident procedure, reviewed filesystem permissions/log retention; ensure host genuinely remains loopback |
| **Unauthenticated LAN/public deployment** | **NOT IN SCOPE / PROHIBITED** | Changing `0.0.0.0` is not an acceptable fix. No auth, access control, tenant ownership boundary or TLS gateway |
| **Authenticated public production** | **BLOCKED** | Design separate scope for authN/authZ, per-user object ownership, TLS and secure proxy, rate limiting, abuse prevention, provider quotas, per-tenant isolation, secret management, monitoring and backups. Independent professional security review required |
| **Multi-worker / distributed queue** | **NOT IN SCOPE** | In-memory HTTP job IDs and single-worker slots cannot support cross-instance routing or exactly-once remote model execution |

`scripts/deployment_readiness.py` reports only **static local code-boundary** readiness: `PASS_LOCAL_STATIC_BOUNDARY` does not override the other BLOCKED decisions. Missing or changed config fails closed. It does not read any API key, contact model providers or make external requests.

## Threat model and tested controls

| Threat / asset | Existing boundary | Testing / limitations |
| --- | --- | --- |
| Attacker reaches API from the network | `python -m mini_deerflow.api` binds Uvicorn to **127.0.0.1**; Vite host also 127.0.0.1 | Source-AST and Vite config gate. Must verify deployment network namespace/host and container port mappings manually. Do not run `uvicorn ... --host 0.0.0.0` |
| Browser-origin misuse / DNS rebinding | `TrustedHostMiddleware` restricts Host; `LocalRequestBoundary` checks unsafe-method Origin; CORS has two exact development origins | ASGI tests reject hostile hosts, attacker origins and preflights. Non-browser requests without Origin remain allowed by design; CORS is **not authentication**. A locally running malicious process may still call the service |
| Excessive request body and JSON injection | 16,384-byte streamed/buffered request ceiling and schema validation | Existing API/DTO tests + new config gate; not a reverse proxy rate limiter |
| Unauthorized viewing/downloading conversation and PPTX | Server-side ledger binds PPTX to exact mode/thread/turn; SHA/byte verification and strict workspace paths | No per-user ACL/authentication; unguessable IDs are **not** access control. Any process/browser with legitimate local access can potentially read session data |
| Malicious research URLs / tool abuse | Existing allowlisted tool registry, URL scheme/host policies, typed schemas and bounded budgets | Trace and tool tests cover the intended boundary, but provider outputs remain untrusted, citations only prove membership, not truth |
| API key exposure | `.env` and `.mini-deerflow/` ignored; `MINI_DEERFLOW_API_KEY` loaded server-side via `SecretStr`; frontend uses relative `/api` | Read-only git-index check rejects accidentally tracked `.env*` (templates allowed). Frontend source scan rejects common provider env exposures. **Does not prove** secrets absent from historical Git commits, vendor bundles, comments, unrelated docs or external log aggregators. Run independent secret scanning prior to any release |
| Provider cost / unbounded jobs | Single active worker, model/tool limits and local E2E opt-in for paid tests | No hard provider billing cap or guaranteed remote cancellation. A client disconnect/timeout does not prove remote work stopped |
| PPTX/data integrity and crash seams | SQLite immutable artifact metadata + SHA-256 + exclusive staged hardlink; verified download | Phase 3.3 maintenance is dry-run-only by default. SQLite metadata and filesystem hardlink are not globally atomic; power-loss durability and backup restore need a drill |
| Confidential logs/traces | Typed safe projections, no raw provider error in public DTO; controlled error messages | Reviewer must inspect any extra logging, third-party monitoring, Vite dev tooling and OS crash dumps for tokens/PII; no comprehensive DLP |
| Malicious dependency / compromised CI | Python `uv.lock`, frontend `package-lock.json`, credential-free CI, Actions read-only contents permission | Reproducible lock consistency and tests, **not an independent SCA/CVE scan**, license scan, SBOM or signature verification. Audit and pin GitHub Actions SHAs before regulated/public release |
| Hostile filesystem peer | Workspace rejects traversal, symlinks and unaccounted hardlinks; operator cleanup only offline | OS/process isolation against a malicious same-user local process is not guaranteed; do not share writable directories with untrusted tenants |

### HTTP artifact response handling

The API returns owner-verified PPTX bytes only for a **completed** turn; content is served with correct MIME and attachment filename, and HTTP responses use `nosniff`. Treat saved PPTX, Markdown and user-provided sources as untrusted content when previewing or sharing. Do not introduce inline HTML rendering or outbound links that bypass the existing URL allowlist.

## Mandatory security controls before local pilot (manual)

1. **Host exposure:** Verify at runtime that API/Vite listen only on local loopback (including Docker/WSL/VM forwarded ports and developer tunnels). A green AST check proves the checked entrypoints, **not all ways of launching Uvicorn**.
2. **Secrets:** Keep API keys out of Git and web builds. Use a local credential manager, limit provider account permissions/budget, rotate key after suspected exposure, and verify sanitized exception/error/trace output. Never post `.env` or credentials to GitHub, PR, artifact uploads or chat.
3. **Storage privacy:** Restrict filesystem permissions on `.mini-deerflow/` to the trusted user; ensure no synchronized/shared folder exposes SQLite history, source evidence or rendered PPTX.
4. **Backup/restore:** Stop all workers; atomically stage a consistent backup of both `checkpoints.sqlite` (including WAL considerations) and the workspaces/presentation files; restore into a separate environment; reopen conversations and validate each committed artifact against hashes/spec; capture timestamp, OS and failure details. Do **not** delete the original until restored data is proven.
5. **Recovery/retention:** Phase 3.3 tool is read-only by default. Only apply safe POSIX detached-stage cleanup after worker shutdown, verified backup and a reviewed dry run. On Windows apply is unsupported; never delete orphan final PPTX as a cleanup shortcut.
6. **Health and monitoring:** `GET /api/health` is a basic process response, **not deep dependency, provider, SQLite disk health or queue readiness**. Record local process failures, per-request outcomes, storage errors, duration and budget exhaustion with sanitized identifiers; set disk space alerts for storage and retention.
7. **Dependencies:** Record Python/frontend lockfiles, scan for known CVEs and licenses with maintained vulnerability databases, review updates and pin action dependencies. Document scan date/tools, not just `uv sync --locked` success.
8. **Smoke and rollback:** Run offline CI (Ubuntu + Windows, React, browser) and real LibreOffice sample render. Rehearse rolling back the entire local directory and restarting without replaying an ambiguous provider POST. Review live-provider E2E only with explicit user approval/budget.
9. **Incident response:** Stop the loopback service, rotate API credentials, disconnect external provider integrations, preserve a local encrypted copy of the data/trace needed for forensic review, and restore from a verified backup. Treat unknown provider spend as a billing incident, not proof of safe cancellation.

## Credential-free automated audit

Run from repo root:

```sh
uv sync --locked --dev
uv run --no-sync python scripts/deployment_readiness.py
uv run --no-sync python -m pytest -q tests/test_deployment_readiness.py
```

Example status structure (no secrets or HTTP payloads):

```json
{
  "status": "PASS_LOCAL_STATIC_BOUNDARY",
  "release_matrix": {
    "trusted_loopback_development": "READY",
    "local_pilot_without_backup_restore_drill": "BLOCKED",
    "unauthenticated_network_deployment": "NOT_IN_SCOPE",
    "public_production": "BLOCKED"
  }
}
```

The automated audit checks these repository source facts and fails closed on missing, linked, mutated or unrecognized configuration: Uvicorn local bind, Vite local host/proxy, explicit trusted hosts, exact allowed browser origins, request body cap, both dependency locks, ignored local data/secrets, absence of tracked real `.env` files, and no frontend source reference to provider credentials. Since Vite is TypeScript, its gate is a conservative literal source check; a changed Vite configuration syntax requires **manual review and gate update**, never silent approval.

The unit tests mutate *temporary copies* of the critical configuration, proving that wildcard Host, public bind, wildcard origin, huge body and frontend secrets fail the gate. They also hit the actual FastAPI ASGI stack to reject attacker Host/Origin/preflight while the offline service stays credential-free.

**Limitations:** This is static inspection and deterministic API tests, not dynamic network scanning, IP/port inventory, fuzzing, penetration testing, comprehensive secrets history scan, signature validation or SCA. GitNexus impact/detect_changes should be reported separately if available; source additions only do not imply zero architectural risk.

## Gate acceptance

- **Code/CI:** Local static gate + all offline Python/frontend/browser CI on the exact Task 3.4 PR HEAD.
- **Release policy:** No edits allowing unauthenticated public ingress. READY is explicitly scoped to *trusted, loopback-only development*.
- **Manual:** Operator restores a representative backup, checks storage permissions and secret manager/rotation, reviews current CVEs, confirms loopback exposure. If missing, local real-user pilot remains BLOCKED.
- **Task closure:** Update Issue #21 with a signed-off decision about local-only vs separately scoped authenticated deployment and all unresolved gates. Merge only upon explicit owner instruction. Automated checks cannot approve public deployment.
