# Phase 2 - Grounded Google Web Provider

## Overview

- Priority: P1
- Status: pending
- Effort: 5h
- Goal: replace the Jina adapter with Gemini 2.5 Flash Google Search and URL Context while preserving domain/tool contracts.

## Architecture

`web.py` remains the vendor-neutral boundary: errors, categories, `SearchResult`, `FetchedPage`, and provider protocols. A new `google_web.py` owns Google SDK construction, request configuration, raw-response parsing, and safe error mapping.

```text
WebSearchTool -> WebSearchProvider.search
              -> GoogleWebProvider
              -> Google Search tool
              -> grounding_chunks/supports
              -> validated SearchResult[]

WebFetchTool -> PublicWebTargetValidator -> SafeWebTarget
             -> GoogleWebProvider
             -> URL Context tool
             -> url_context_metadata + response text
             -> validated FetchedPage
             -> returned/final URL validation by WebFetchTool
```

## Exact file changes

| File | Action | Change |
| --- | --- | --- |
| `src/mini_deerflow/web.py` | modify | Retain vendor-neutral contracts/errors; remove Jina HTTP client, Jina provider, fixed endpoints, and HTTP-body parser/helpers. |
| `src/mini_deerflow/google_web.py` | create | Add injectable Google content-client protocol, production `google-genai` adapter, `GoogleWebProvider`, metadata normalizers, and exception/category mapping. |
| `tests/test_web_provider.py` | replace vendor cases | Test Google request configs, normalization, malformed metadata, quota/auth/upstream/transport errors, cancellation, and secret/body redaction. |
| `tests/test_web_tools.py` | update only if contract expectations change | Keep generic tool contract and SSRF/redirect tests; do not make tools Google-aware. |
| `tests/test_web_contracts.py` | update only if imports move | Preserve existing error/category/model contract coverage. |

## Search normalization contract

1. Invoke `gemini-2.5-flash` with Google Search enabled and a bounded prompt requesting factual research for the supplied query.
2. Accept source identity only from first-candidate grounding metadata:
   - `grounding_chunks[index].web.title` -> `SearchResult.title`;
   - `grounding_chunks[index].web.uri` -> `SearchResult.url`;
   - linked `grounding_supports[].segment.text` (or bounded segment offsets) -> snippet.
3. Never parse URLs from `response.text`, model JSON, search queries, or rendered Search Entry Point HTML.
4. Drop invalid/non-HTTP(S) chunks, deduplicate by normalized URL in provider order, cap at requested `max_results`, and bound title/snippet via existing Pydantic models.
5. Metadata present with no chunks means an empty result; missing/invalid candidate metadata means `MALFORMED_RESPONSE`.

## URL Context normalization contract

1. Require a `SafeWebTarget`; passing a raw string raises before SDK invocation.
2. Invoke URL Context with only `str(target.url)` and a bounded extraction prompt. Never submit resolved IPs, credentials, or local paths.
3. Require successful URL retrieval metadata. Use metadata `retrieved_url` as the final URL; never take a URL from generated text.
4. Use response text as model-mediated page content, set a truthful content type such as `text/plain`, and set status `200` only for successful retrieval metadata.
5. If final URL differs, place it in `redirect_chain`; `WebFetchTool` then revalidates it before accepting content.
6. A failed/unsafe/unsupported retrieval status becomes a static `WebFetchError` with `REJECTION` or `UPSTREAM`; no raw URL/status payload is exposed.

## Error and quota handling

| Google condition | Project category | Behavior |
| --- | --- | --- |
| Missing/blank key or model | `CONFIGURATION` | Fail before SDK call. |
| 401/403 | `AUTHENTICATION` | Static error, no response body/cause. |
| 429/resource exhausted | `RATE_LIMIT` | No immediate provider retry; admitted tool budget remains consumed. |
| 404/model endpoint | `ENDPOINT` | Static endpoint/model unavailable error. |
| Other 4xx | `REJECTION` | Controlled provider rejection. |
| 5xx | `UPSTREAM` | Controlled upstream failure. |
| timeout/network/SDK transport | `TRANSPORT` | Preserve `CancelledError`; normalize other expected failures. |
| missing candidates/metadata/invalid URL | `MALFORMED_RESPONSE` | Reject response; do not salvage generated URLs. |

Do not encode “500 free searches/day” as an application guarantee. It is a current published free-tier allowance, not a stable runtime contract. Existing agent budgets cap calls; provider quota remains external.

## Security/SSRF constraints

- Keep pre-provider public HTTP(S), userinfo, hostname, DNS, and IP checks unchanged.
- Google performs the fetch remotely, but validation still prevents submitting localhost/private targets and leaking internal URL intent to a third party.
- Revalidate every final/redirect URL exposed by metadata; reject content before evidence creation when validation fails.
- Do not claim visibility into redirects Google does not expose, DNS pinning, or byte-exact source capture.
- Do not log SDK request/response objects; tests must scan rendered exceptions/tool results for API key and raw payload markers.

## Test scenarios

- Grounded chunks -> ordered/deduplicated/capped `SearchResult` values.
- Grounding support indices -> per-source snippets; invalid indices ignored safely.
- Model body contains an invented URL -> URL is absent from results.
- Missing grounding metadata -> malformed-response error.
- URL Context success -> `FetchedPage`; changed final URL -> redirect chain.
- URL Context failure status -> safe typed error.
- Raw/private URL cannot reach provider; unsafe returned URL remains denied by existing tool test.
- 401/403/404/429/5xx/timeout -> exact safe categories.
- Cancellation propagates and secrets/raw bodies never appear.

## Success criteria

- Jina production symbols and dependencies are absent.
- Both provider methods satisfy existing protocols; `tools/web.py` stays vendor-neutral.
- Evidence receives only schema-valid metadata-owned URLs/content.
- Default unit suite makes zero network calls.

## Todo

- [ ] Run impacts for `JinaWebProvider`, `WebProvider`, and both web tools.
- [ ] Extract vendor-neutral contracts from Jina implementation.
- [ ] Implement injected Google client/provider.
- [ ] Implement deterministic metadata normalization and error mapping.
- [ ] Replace provider tests and run web/safety/tracing suites.

