# Mini DeerFlow research workspace

React + TypeScript + Vite frontend for the local FastAPI transport. All conversation, execution, evidence, skill history and artifact data comes from the safe backend DTOs. Offline mode is a fixed deterministic walkthrough, not arbitrary external research.

## Run locally

From the repository root (Python dependencies installed with `uv sync`):

```sh
uv run python -m uvicorn mini_deerflow.api:create_app --factory --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. Vite strictly binds port 5173 and proxies `/api` to localhost:8000. The backend is local-only; run a single worker. Set up the backend's live credentials separately to use Live mode.

## Checks

```sh
npm run typecheck
npm run lint
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

Playwright starts an isolated Python API using a new, unseeded temporary storage directory and the Vite server. Both ports must be free. The first test exercises a cold initial conversation, desktop follow-up/reload and phone focus/type/send. The second test then seeds one genuinely interrupted conversation through the real runtime to verify resume and preserved execution history. No API responses are mocked. The config passes its unique storage path to the launcher/test worker through `MINI_DEERFLOW_E2E_STORAGE`; the launcher refuses to serve an existing directory. Screenshots/download evidence are written under `test-results/`. No production conversation storage is used.

## Interaction and data boundaries

- URL `mode` and `thread` preserve navigation across reload; all actual history is fetched from the server. Browser Back/Forward works.
- Mode changes remount ephemeral state and scope all job/session query identities. Server job discovery runs every 1.5s; saved session refresh runs every 2.5s while idle. Durable reads wait for job discovery and pause during active jobs to avoid racing checkpoint initialization; current job snapshots supply progress. Terminal status changes refresh saved conversations and turns. Refresh retries read requests; uncertain POST requests are never automatically retried.
- A skill applies to one accepted submission and resets. Historical skill identity belongs to the selected turn. The selector contains metadata only.
- Running, queued and unresolved persisted turns disable duplicate submissions. An interrupted/running saved turn can be resumed after job discovery confirms no active operation.
- Graph uses backend node/edge identities with deterministic Dagre layout, React Flow pan/zoom/fit controls and an equivalent native node selector. It is read-only. Failed child status is independent of parent success.
- Graph metric cards expose all backend counts: nodes, running, completed, failed, tool calls, sub-agents and evidence. Textual legends name every supported node status and kind; legend entries describe possible categories, not additional executed entities.
- Active job turns appear in the inspector picker before they are persisted. With no explicit historical selection, the active turn is the default, including after navigating away and reopening a cached conversation. Historical turns remain selectable, and the picker can return to the active job.
- Graph and trace snapshots are used while a job is queued/running. After a terminal transition, persisted turn data becomes authoritative only after a successful session request that **started after** that terminal job identity/status was observed. Until then, the UI labels the latest snapshot as a job segment, marks cached conversation data as historical, and pauses submission/recovery. Refresh failures offer `Retry saved history`; stale cached Plan/budget/artifact data is not substituted. Artifact download waits for reconciled history. A successful reload restores full pre-resume children, delegation, fan-in and traces.
- Trace Run, Kind and Outcome filters each accept multiple selections. Selections are ORed within a list and ANDed across lists. An empty selection means **All**, matching the original UI. `All runs`, `All kinds` and `All outcomes` clear individual selections; `Reset trace filters` clears all three. Native multiple selects support Ctrl/Command-click and Shift+arrow keyboard selection. Selecting a single item still filters to that item.
- Plan explicitly displays the selected turn's goal alongside its steps, status and budget, independently of artifact availability.
- Markdown supports GFM comparison tables in focusable horizontal-scroll regions and denies HTML and images; only exact accepted citation URLs with HTTP(S) schemes are active. Artifact preview is inert exact source and downloads use the selected mode/thread/turn endpoint.
- Mobile navigation and inspector use native modal dialogs (focus trap, Escape, return focus); tabs support arrows/Home/End. CSS honors reduced motion, 44px targets, and safe-area insets.

## Manual browser acceptance

At 1440×900 create an offline conversation with a selected skill, follow up with None, reload, reopen each historical turn and verify its skill identity. Inspect Current run and Workflow; select failed and successful branches, parent, fan-in and continuation, and compare status/details. Check Evidence, Review, trace run/kind/outcome filters, Context approximate metrics, and exact report download. At 375×812 use keyboard navigation, open/close drawers with Escape, verify focus returns, and confirm no document-level horizontal overflow. Test a disconnected API and use Refresh to recover. A seeded interrupted conversation is needed to manually exercise Resume after a server restart. Live execution requires configured backend credentials and separate acceptance.

The automated phone test uses a desktop Chromium viewport and keyboard events; it does not emulate a mobile software keyboard. Physical-device keyboard/visual-viewport validation remains pending. See `VERIFICATION.md` for exact results and remaining verification gaps.
