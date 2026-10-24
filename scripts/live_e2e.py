"""Manual, explicitly opted-in live research -> native PPTX acceptance.

Never invoked by pytest or CI. A parent process supervises one disposable,
killable worker per case. Only allowlisted, redacted metrics are published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

CONSENT_ENV = "MINI_DEERFLOW_LIVE_E2E_CONSENT"
KEY_ENV = "MINI_DEERFLOW_API_KEY"
# Measured on the configured provider (glm-5.3 via api.z.ai coding endpoint):
# a trivial chat answers in ~3s, but the research planner's single structured
# response (reasoning + strict JSON plan) exceeded the previous 30s per-request
# bound and failed BOTH live cases as provider_timeout (see diag evidence in
# docs/phase3-live-e2e.md). 120s keeps every request explicitly bounded while
# accommodating that measured latency; the per-case wall clock (timeout-seconds
# plus the parent's hard kill) still governs the total budget.
WORKER_REQUEST_TIMEOUT_SECONDS = 120.0
# The DeckPlan slide-planner response is far larger than any research-call
# schema (8 layout-specific payloads in one strict JSON object). Measured
# in-flow: the deck job failed ~120s after submission under the shared 120s
# bound while isolated runs at 300s succeeded - the planner call itself needs
# a longer explicit per-request bound.
WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS = 240.0
# The child must stop polling early enough to let TestClient/ApiJobs teardown
# finish (teardown waits for at most one in-flight bounded provider request,
# now the 240s slide-planner call) and still write its result file before the
# parent's hard kill at timeout-seconds + 15. Without this margin a deadline
# breach produced no child result at all (observed "hard_timeout" with stage
# "worker").
WORKER_POLL_GRACE_SECONDS = 250.0
CASES = {
    "vi": {
        "question": "Dựa trên nguồn đáng tin cậy, đánh giá lợi ích và hạn chế của năng lượng mặt trời đối với hệ thống điện.",
        "audience": "sinh viên đại học và giảng viên",
        "instructions": "Tách bằng chứng, hạn chế và kết luận; không suy đoán số liệu.",
    },
    "en": {
        "question": "Using verifiable sources, compare the benefits and limitations of urban tree planting for summer heat mitigation.",
        "audience": "university students and researchers",
        "instructions": "Distinguish supported findings from uncertainties; do not invent metrics.",
    },
}


def consent_ready(env: dict[str, str]) -> bool:
    key = env.get(KEY_ENV, "").strip()
    return (
        env.get(CONSENT_ENV) == "YES"
        and bool(key)
        and not key.startswith("ci-fixture-")
        and key.lower() not in {"test-secret", "replace-with-your-z-ai-key"}
    )


def safe_result(
    case: str, *, outcome: str, stage: str, reason_code: str
) -> dict[str, object]:
    """No credentials, prompts, source URLs, payloads or exception messages."""
    return {
        "case": case,
        "outcome": outcome,
        "stage": stage,
        "reason_code": reason_code,
        "research_failure_family": None,
        "last_failed_node": None,
        "last_trace_error_category": None,
        "last_trace_error_code": None,
        "research_seconds": None,
        "presentation_seconds": None,
        "evidence_count": None,
        "accepted_citations": None,
        "citation_membership_valid": None,
        "tool_calls_used": None,
        "slide_count": None,
        "distinct_layouts": None,
        "pptx_sha256": None,
        "pptx_bytes": None,
        "pptx_structure_valid": None,
        "manual_fact_check": "UNVERIFIED",
        "manual_office_visual": "UNVERIFIED",
    }


_FAILURE_FAMILIES = {
    "AuthenticationError": "provider_authentication",
    "PermissionDeniedError": "provider_permission",
    "RateLimitError": "provider_rate_limit",
    "APIConnectionError": "provider_connection",
    "APITimeoutError": "provider_timeout",
    "BadRequestError": "provider_bad_request",
    "NotFoundError": "provider_not_found",
    "UnprocessableEntityError": "provider_invalid_request",
    "InternalServerError": "provider_server_error",
    "APIStatusError": "provider_http_error",
    "OutputParserException": "structured_output",
    "OutputParserError": "structured_output",
    "ValidationError": "structured_validation",
    "SlideAgentValidationError": "structured_validation",
    "SlideAgentConflictError": "artifact_conflict",
    "SlideAgentError": "slide_agent",
    "PptxValidationError": "pptx_validation",
    "LayoutOverflowError": "pptx_layout_overflow",
    "ArtifactConflictError": "artifact_conflict",
    "ArtifactIntegrityError": "storage_integrity",
    "ArtifactNotFoundError": "artifact_missing",
    "ArtifactError": "artifact_storage",
    "ContextBudgetExceededError": "context_budget",
    "GraphRecursionError": "graph_recursion",
    "TimeoutError": "timeout",
    "ConnectionError": "connection",
    "OperationalError": "storage",
}


def classify_failure(error: BaseException) -> str:
    """Classify exception types only; never serialize error messages."""
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        family = _FAILURE_FAMILIES.get(type(current).__name__)
        if family is not None:
            return family
        current = current.__cause__ or current.__context__
    return "unclassified"


def safe_failed_trace(job: dict) -> dict[str, str | None]:
    """Only known typed safe DTO values; never echo arbitrary trace data."""
    fields: dict[str, str | None] = {
        "last_failed_node": None,
        "last_trace_error_category": None,
        "last_trace_error_code": None,
    }
    traces = job.get("traces")
    if not isinstance(traces, list):
        return fields
    allowed_nodes = {
        "planner",
        "decide_action",
        "execute_tool",
        "complete_step",
        "review",
        "replan",
        "synthesize",
        "budget_exhausted",
    }
    allowed_categories = {
        "safety",
        "provider",
        "transport",
        "context_budget",
        "tool",
        "persistence",
        "runtime",
        "internal",
    }
    allowed_codes = {
        "url_denied",
        "web_provider_failure",
        "web_transport_failure",
        "model_provider_failure",
        "tool_failure",
        "context_budget_exceeded",
        "checkpoint_failure",
        "runtime_failure",
        "unexpected_failure",
    }
    # Prefer the most recent FAILED trace that names a workflow node: the
    # run-level failure event (node-less) is emitted last and would otherwise
    # hide which seam actually failed.
    fallback: dict[str, str | None] | None = None
    for item in reversed(traces):
        if not isinstance(item, dict):
            continue
        if item.get("outcome") not in {
            "failed",
            "provider_failed",
            "transport_failed",
        }:
            continue
        node = item.get("node")
        category = item.get("error_category")
        code = item.get("error_code")
        candidate = {
            "last_failed_node": (
                node if isinstance(node, str) and node in allowed_nodes else None
            ),
            "last_trace_error_category": (
                category
                if isinstance(category, str) and category in allowed_categories
                else None
            ),
            "last_trace_error_code": (
                code if isinstance(code, str) and code in allowed_codes else None
            ),
        }
        if candidate["last_failed_node"] is not None:
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback if fallback is not None else fields


def poll_deadline(started: float, timeout_seconds: int) -> float:
    """Polling deadline that leaves teardown headroom inside the hard kill."""

    return started + max(1.0, timeout_seconds - WORKER_POLL_GRACE_SECONDS)


def _poll(client: object, job: dict, *, deadline: float) -> dict:
    while time.monotonic() < deadline:
        response = client.get(f"/api/live/jobs/{job['job_id']}")
        if response.status_code != 200:
            raise RuntimeError("job_poll_http_error")
        snapshot = response.json()
        if snapshot["status"] in ("completed", "failed"):
            return snapshot
        time.sleep(1)
    raise TimeoutError("job_deadline")


def _save(path: Path, result: dict[str, object]) -> int:
    path.write_text(
        json.dumps(result, ensure_ascii=True, sort_keys=True), encoding="utf-8"
    )
    return 0 if result["outcome"] == "passed" else 1


def worker_command(case: str, timeout_seconds: int, result: Path) -> list[str]:
    """Build exact child arguments; independently unit-tested without network."""
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--allow-paid-live",
        "--worker",
        "--case",
        case,
        "--timeout-seconds",
        str(timeout_seconds),
        "--worker-result",
        str(result),
    ]


def _child(case: str, result_path: Path, deadline_seconds: int) -> int:
    """Run the genuine model, API, PPTX and persistence inside a killable child."""
    result = safe_result(case, outcome="failed", stage="startup", reason_code="unknown")
    started = time.monotonic()
    try:
        # Delay all provider-capable imports until AFTER the double consent gate.
        import asyncio

        from fastapi.testclient import TestClient

        from mini_deerflow.api import create_app
        from mini_deerflow.config import Settings
        from mini_deerflow.demo.live_backend import LiveDemoBackend
        from mini_deerflow.demo.service import DemoRuntimeService
        from mini_deerflow.model import create_chat_model
        from mini_deerflow.pptx.artifacts import PPTX_MIME, PptxArtifactStore
        from mini_deerflow.pptx.schemas import DeckSpec
        from mini_deerflow.pptx.slide_agent import SlideAgentService
        from mini_deerflow.pptx.validator import validate_pptx
        from mini_deerflow.runtime import RuntimeLimits

        class DiagnosedLiveBackend(LiveDemoBackend):
            failure_family: str | None = None

            async def run(self, command, *, trace_sink=None):
                try:
                    return await super().run(command, trace_sink=trace_sink)
                except Exception as error:
                    self.failure_family = classify_failure(error)
                    raise

        class DiagnosedSlideService(SlideAgentService):
            """Record the typed failure family; never the payload."""

            failure_family: str | None = None

            async def generate(self, *args, **kwargs):
                try:
                    return await super().generate(*args, **kwargs)
                except Exception as error:
                    self.failure_family = classify_failure(error)
                    raise

        settings = Settings(
            _env_file=None,
            request_timeout=WORKER_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
            wiki_request_timeout=10.0,
        )
        # A tool-call cap is NOT an exact ceiling on model calls or paid tokens.
        limits = RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=8,
            max_replan_cycles=1,
            recursion_limit=60,
            max_delegation_concurrency=1,
            max_session_tool_calls=8,
            max_conversation_context_chars=8_000,
        )
        with tempfile.TemporaryDirectory(prefix="mini-deerflow-live-e2e-") as storage:
            root = Path(storage)
            backend = DiagnosedLiveBackend(
                root / "live", settings=settings, limits=limits
            )
            service = DemoRuntimeService(backend)
            store = PptxArtifactStore(root / "live", mode="live")
            slide_planner_settings = Settings(
                _env_file=None,
                request_timeout=WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS,
                max_retries=0,
                wiki_request_timeout=10.0,
            )
            slide_agent = DiagnosedSlideService(
                service,
                store,
                planner_model_factory=lambda: create_chat_model(slide_planner_settings),
                structured_output_mode=settings.structured_output_mode,
            )
            app = create_app(
                storage_root=root,
                services={"live": service},
                slide_services={"live": slide_agent},
            )
            deadline = poll_deadline(started, deadline_seconds)
            with TestClient(app) as client:
                result["stage"] = "research"
                response = client.post(
                    "/api/live/conversations",
                    json={
                        "message": CASES[case]["question"],
                        "skill_id": "slide-creation",
                    },
                )
                if response.status_code != 202:
                    result["reason_code"] = "research_submit_http_error"
                    return _save(result_path, result)
                submitted = response.json()
                research = _poll(client, submitted, deadline=deadline)
                if research["status"] != "completed":
                    result["reason_code"] = "research_job_failed"
                    result["research_failure_family"] = backend.failure_family
                    result.update(safe_failed_trace(research))
                    return _save(result_path, result)
                result["research_seconds"] = round(time.monotonic() - started, 2)
                thread = submitted["thread_id"]
                turn = submitted["turn_id"]
                snapshot = client.get(f"/api/live/conversations/{thread}")
                if snapshot.status_code != 200:
                    result["reason_code"] = "research_snapshot_unavailable"
                    return _save(result_path, result)
                source = next(
                    (
                        item
                        for item in snapshot.json()["turns"]
                        if item["turn_id"] == turn
                    ),
                    None,
                )
                if not source or source["status"] != "completed" or not source["run"]:
                    result["reason_code"] = "research_snapshot_invalid"
                    return _save(result_path, result)
                run = source["run"]
                evidence = run["evidence"]
                citations = run["accepted_citations"]
                evidence_urls = {item["canonical_url"] for item in evidence}
                result["evidence_count"] = len(evidence)
                result["accepted_citations"] = len(citations)
                result["citation_membership_valid"] = set(citations).issubset(
                    evidence_urls
                )
                result["tool_calls_used"] = run["budget"]["total_tool_calls"]
                if result["citation_membership_valid"] is not True:
                    result["reason_code"] = "citation_membership_mismatch"
                    return _save(result_path, result)

                result["stage"] = "presentation"
                deck_started = time.monotonic()
                endpoint = f"/api/live/conversations/{thread}/turns/{turn}/presentation"
                request_body = {
                    "topic": CASES[case]["question"],
                    "audience": CASES[case]["audience"],
                    "language": case,
                    "slide_count": 8,
                    "theme": "academic",
                    "instructions": CASES[case]["instructions"],
                }
                response = client.post(endpoint, json=request_body)
                if response.status_code != 202:
                    result["reason_code"] = "presentation_submit_http_error"
                    return _save(result_path, result)
                deck_job = _poll(client, response.json(), deadline=deadline)
                if deck_job["status"] != "completed":
                    result["reason_code"] = "presentation_job_failed"
                    result["research_failure_family"] = slide_agent.failure_family
                    message = deck_job.get("error_message")
                    result["presentation_safe_error"] = (
                        message
                        if isinstance(message, str) and len(message) <= 240
                        else None
                    )
                    return _save(result_path, result)
                result["presentation_seconds"] = round(
                    time.monotonic() - deck_started, 2
                )

                result["stage"] = "verification"
                metadata_response = client.get(endpoint + "/metadata")
                download = client.get(endpoint)
                if metadata_response.status_code != 200 or download.status_code != 200:
                    result["reason_code"] = "verified_artifact_unavailable"
                    return _save(result_path, result)
                metadata = metadata_response.json()
                data = download.content
                record, stored = asyncio.run(store.retrieve(thread, turn))
                spec = DeckSpec.model_validate_json(record.spec_json)
                valid = validate_pptx(data, spec, min_layouts=4).valid
                result["slide_count"] = metadata["slide_count"]
                result["distinct_layouts"] = len(metadata["layouts"])
                result["pptx_bytes"] = len(data)
                result["pptx_sha256"] = hashlib.sha256(data).hexdigest()
                result["pptx_structure_valid"] = bool(valid)
                if not all(
                    (
                        valid,
                        metadata["slide_count"] == 8,
                        len(metadata["outline"]) == 8,
                        len(metadata["layouts"]) >= 4,
                        data == stored,
                        metadata["sha256"] == record.sha256,
                        result["pptx_sha256"] == record.sha256,
                        download.headers.get("content-type") == PPTX_MIME,
                        download.headers.get("content-disposition")
                        == 'attachment; filename="presentation.pptx"',
                    )
                ):
                    result["reason_code"] = "artifact_contract_mismatch"
                    return _save(result_path, result)
                result["stage"] = "completed"
                result["outcome"] = "passed"
                result["reason_code"] = "none"
                return _save(result_path, result)
    except TimeoutError:
        result["reason_code"] = "job_deadline_exceeded"
    except Exception:  # noqa: BLE001 - exception messages may leak provider data
        # A teardown error after a recorded pass must not relabel the case.
        if result.get("outcome") != "passed":
            result["reason_code"] = (
                "configuration_failed"
                if result["stage"] == "startup"
                else "unexpected_failure"
            )
        else:
            result["reason_code"] = "none"
    return _save(result_path, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid-live", action="store_true")
    parser.add_argument("--case", choices=(*CASES, "both"), default="both")
    parser.add_argument("--timeout-seconds", type=int, default=480)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--worker-result", type=Path, default=None, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if not args.allow_paid_live or not consent_ready(os.environ):
        print(
            "BLOCKED: explicit flag, YES consent and a non-fixture API key are required."
        )
        return 2
    # Measured 2026-10-24 on glm-5.3/api.z.ai: research alone took ~682s (7
    # tool calls, sequential reasoning model calls), leaving no room for the
    # presentation stage inside the old 900s ceiling. 1800s keeps an explicit
    # bound while covering research + presentation + teardown margin. The
    # poll grace still enforces the child result before the hard kill.
    if not 30 <= args.timeout_seconds <= 1800:
        print("BLOCKED: timeout must be between 30 and 1800 seconds.")
        return 2
    if args.worker:
        if args.case not in CASES or args.worker_result is None:
            print("BLOCKED: invalid worker invocation.")
            return 2
        return _child(args.case, args.worker_result, args.timeout_seconds)

    try:
        from mini_deerflow.config import Settings

        settings = Settings(_env_file=None)
        provider_host = urlsplit(str(settings.base_url)).hostname or "unknown"
        model_name = settings.model_name
    except ValueError:
        print("BLOCKED: provider configuration invalid (details redacted).")
        return 2

    selected = list(CASES) if args.case == "both" else [args.case]
    report: dict[str, object] = {
        "schema_version": 1,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "provider_host": provider_host,
        "model": model_name,
        "wall_clock_limit_seconds_per_case": args.timeout_seconds,
        "research_tool_limit_per_case": 8,
        "model_call_count": None,
        "token_count": None,
        "monetary_cost": None,
        "live_execution_requested": True,
        "cases": [],
        "automated_factual_accuracy": "NOT_ASSESSED",
        "office_visual_acceptance": "NOT_ASSESSED",
    }
    # TestClient's normal shutdown waits for its background worker; each case
    # therefore runs in a child that can be terminated by the parent.
    with tempfile.TemporaryDirectory(prefix="mini-deerflow-live-controller-") as work:
        for case in selected:
            path = Path(work) / f"{case}.json"
            command = worker_command(case, args.timeout_seconds, path)
            try:
                child = subprocess.run(
                    command,
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=args.timeout_seconds + 15,
                )
                if path.is_file():
                    case_result = json.loads(path.read_text(encoding="utf-8"))
                else:
                    case_result = safe_result(
                        case,
                        outcome="failed",
                        stage="worker",
                        reason_code="no_child_result",
                    )
                if child.returncode != 0 and case_result["outcome"] == "passed":
                    case_result = safe_result(
                        case,
                        outcome="failed",
                        stage="worker",
                        reason_code="child_exit_failure",
                    )
            except subprocess.TimeoutExpired:
                case_result = safe_result(
                    case, outcome="failed", stage="worker", reason_code="hard_timeout"
                )
            report["cases"].append(case_result)
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    report["status"] = (
        "PASS"
        if all(item["outcome"] == "passed" for item in report["cases"])
        else "FAIL"
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
