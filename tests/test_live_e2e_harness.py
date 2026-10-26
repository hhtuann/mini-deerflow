"""Credential-free contract tests for the opt-in-only live evaluation runner."""

import json
from pathlib import Path

from scripts import live_e2e


def test_requires_flag_consent_and_real_key():
    assert not live_e2e.consent_ready({})
    assert not live_e2e.consent_ready({live_e2e.CONSENT_ENV: "YES"})
    assert not live_e2e.consent_ready({live_e2e.KEY_ENV: "candidate-key"})
    assert not live_e2e.consent_ready(
        {live_e2e.CONSENT_ENV: "yes", live_e2e.KEY_ENV: "candidate-key"}
    )
    for bad_key in (
        "ci-fixture-only-not-a-real-key",
        "test-secret",
        "replace-with-your-z-ai-key",
    ):
        assert not live_e2e.consent_ready(
            {live_e2e.CONSENT_ENV: "YES", live_e2e.KEY_ENV: bad_key}
        )
    assert live_e2e.consent_ready(
        {live_e2e.CONSENT_ENV: "YES", live_e2e.KEY_ENV: "candidate-configured-key"}
    )


def test_default_cli_blocks_before_any_provider_import(monkeypatch, capsys):
    monkeypatch.delenv(live_e2e.CONSENT_ENV, raising=False)
    monkeypatch.delenv(live_e2e.KEY_ENV, raising=False)
    monkeypatch.setattr(
        live_e2e,
        "_child",
        lambda *args: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    assert live_e2e.main([]) == 2
    assert live_e2e.main(["--allow-paid-live"]) == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_worker_direct_invocation_cannot_bypass_consent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(live_e2e.KEY_ENV, "candidate-key")
    monkeypatch.delenv(live_e2e.CONSENT_ENV, raising=False)
    output = tmp_path / "no-result.json"
    assert (
        live_e2e.main(
            [
                "--worker",
                "--worker-result",
                str(output),
                "--case",
                "vi",
                "--allow-paid-live",
            ]
        )
        == 2
    )
    assert not output.exists()


def test_ci_dummy_key_cannot_trigger_paid_network(monkeypatch):
    monkeypatch.setenv(live_e2e.CONSENT_ENV, "YES")
    monkeypatch.setenv(live_e2e.KEY_ENV, "ci-fixture-only-not-a-real-key")
    assert live_e2e.main(["--allow-paid-live"]) == 2


def test_two_distinct_language_briefs_respect_http_boundaries():
    assert set(live_e2e.CASES) == {"vi", "en"}
    for details in live_e2e.CASES.values():
        assert 10 <= len(details["question"].strip()) <= 1000
        assert 3 <= len(details["audience"]) <= 120
        assert len(details["instructions"]) <= 500


def test_poll_reaches_terminal_job_using_only_real_response_shape():
    class Response:
        status_code = 200

        def __init__(self, status: str) -> None:
            self.status = status

        def json(self):
            return {"status": self.status}

    class Client:
        def __init__(self, status: str) -> None:
            self.status = status
            self.calls = []

        def get(self, uri):
            self.calls.append(uri)
            return Response(self.status)

    from time import monotonic

    for status in ("completed", "failed"):
        client = Client(status)
        assert (
            live_e2e._poll(client, {"job_id": "fake-job"}, deadline=monotonic() + 1)[
                "status"
            ]
            == status
        )
        assert client.calls == ["/api/live/jobs/fake-job"]


def test_result_has_no_prompts_credentials_raw_sources_or_exception_text(
    tmp_path: Path,
):
    payload = live_e2e.safe_result(
        "en", outcome="failed", stage="research", reason_code="research_job_failed"
    )
    payload["pptx_sha256"] = "a" * 64
    output = tmp_path / "sanitized.json"
    assert live_e2e._save(output, payload) == 1
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["manual_fact_check"] == "UNVERIFIED"
    assert saved["manual_office_visual"] == "UNVERIFIED"
    for private in (
        "api_key",
        "prompt",
        "exception",
        "traceback",
        "canonical_url",
        "path",
    ):
        assert private not in saved


def test_timeout_configuration_fails_before_provider_use(monkeypatch, capsys):
    monkeypatch.setenv(live_e2e.CONSENT_ENV, "YES")
    monkeypatch.setenv(live_e2e.KEY_ENV, "candidate-key")
    assert live_e2e.main(["--allow-paid-live", "--timeout-seconds", "29"]) == 2
    # The ceiling covers measured research (~682s) + presentation + margins;
    # values above it stay blocked.
    assert live_e2e.main(["--allow-paid-live", "--timeout-seconds", "1801"]) == 2
    assert "timeout must be" in capsys.readouterr().out


def test_default_timeout_matches_documented_1200_seconds_without_running_live(
    monkeypatch,
):
    from types import SimpleNamespace

    monkeypatch.setenv(live_e2e.CONSENT_ENV, "YES")
    monkeypatch.setenv(live_e2e.KEY_ENV, "candidate-configured-key")
    calls = []

    def worker_stub(command, **kwargs):
        calls.append((command, kwargs))
        # No result file: main must report failure, not a fabricated live pass.
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(live_e2e.subprocess, "run", worker_stub)
    assert live_e2e.main(["--allow-paid-live", "--case", "vi"]) == 1
    assert len(calls) == 1
    command, options = calls[0]
    assert command[command.index("--timeout-seconds") + 1] == "1200"
    assert options["timeout"] == 1215


def test_worker_command_is_complete_and_killable(tmp_path: Path):
    path = tmp_path / "child-report.json"
    command = live_e2e.worker_command("vi", 480, path)
    assert command[0]
    assert command[2:] == [
        "--allow-paid-live",
        "--worker",
        "--case",
        "vi",
        "--timeout-seconds",
        "480",
        "--worker-result",
        str(path),
    ]
    assert len(command) == 10


def test_live_failure_classification_keeps_exception_text_private():
    auth_error = type("AuthenticationError", (Exception,), {})("secret-token")
    assert live_e2e.classify_failure(auth_error) == "provider_authentication"
    inner = type("RateLimitError", (Exception,), {})("private provider response")
    outer = RuntimeError("opaque error")
    outer.__cause__ = inner
    assert live_e2e.classify_failure(outer) == "provider_rate_limit"
    assert live_e2e.classify_failure(RuntimeError("API key: private")) == "unclassified"


def test_presentation_report_rejects_arbitrary_error_text(tmp_path):
    secret = "secret-canary https://private.example/token C:\\private\\key"
    # Short input is deliberately below the previous length-only threshold.
    assert len(secret) < 240
    for message in (secret, "bad plan", secret * 20, None, {"detail": secret}):
        assert live_e2e.safe_presentation_error({"error_message": message}) is None
    fixed = "Presentation planning failed validation."
    assert live_e2e.safe_presentation_error({"error_message": fixed}) == fixed
    result = live_e2e.safe_result(
        "vi",
        outcome="failed",
        stage="presentation",
        reason_code="presentation_job_failed",
    )
    result["presentation_safe_error"] = live_e2e.safe_presentation_error(
        {"error_message": secret}
    )
    path = tmp_path / "safe-report.json"
    assert live_e2e._save(path, result) == 1
    assert secret not in path.read_text(encoding="utf-8")


def test_failed_trace_diagnostics_allowlist_only():
    reported = live_e2e.safe_failed_trace(
        {
            "traces": [
                {
                    "outcome": "failed",
                    "node": "planner",
                    "error_category": "provider",
                    "error_code": "runtime_failure",
                },
            ]
        }
    )
    assert reported == {
        "last_failed_node": "planner",
        "last_trace_error_category": "provider",
        "last_trace_error_code": "runtime_failure",
    }
    # The run-level failure (node-less, emitted last) must not hide the
    # node-level seam that actually failed.
    attributed = live_e2e.safe_failed_trace(
        {
            "traces": [
                {
                    "outcome": "failed",
                    "node": "decide_action",
                    "error_category": "internal",
                    "error_code": "unexpected_failure",
                },
                {
                    "outcome": "failed",
                    "node": None,
                    "error_category": "internal",
                    "error_code": "unexpected_failure",
                },
            ]
        }
    )
    assert attributed == {
        "last_failed_node": "decide_action",
        "last_trace_error_category": "internal",
        "last_trace_error_code": "unexpected_failure",
    }
    malicious = live_e2e.safe_failed_trace(
        {
            "traces": [
                {
                    "outcome": "failed",
                    "node": "bearer secret",
                    "error_category": "api_key=private",
                    "error_code": "private provider response",
                },
            ]
        }
    )
    assert malicious == {
        "last_failed_node": None,
        "last_trace_error_category": None,
        "last_trace_error_code": None,
    }
    assert live_e2e.safe_failed_trace({"traces": []}) == malicious


def test_worker_request_timeout_accommodates_measured_planner_latency():
    """Root-cause regression: 30s per-request bound timed out the planner.

    Measured on the configured provider: trivial call ~3s; the planner's
    structured response exceeded 30s and failed both live cases as
    provider_timeout. The worker bound must stay above that measured
    floor while remaining explicit and below the per-case wall clock.
    """
    timeout = live_e2e.WORKER_REQUEST_TIMEOUT_SECONDS
    assert timeout > 30.0
    assert timeout >= 120.0
    assert timeout <= 300.0


def test_poll_deadline_leaves_teardown_margin_before_hard_kill():
    """A deadline breach must still write a child result before the kill.

    The parent kills at timeout+15; teardown can wait for one in-flight
    bounded request (120s) plus overhead, so polling must stop early.
    """
    grace = live_e2e.WORKER_POLL_GRACE_SECONDS
    assert grace >= live_e2e.WORKER_REQUEST_TIMEOUT_SECONDS
    assert grace >= live_e2e.WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS
    started = 1000.0
    for seconds in (30, 480, 900, 1800):
        deadline = live_e2e.poll_deadline(started, seconds)
        assert deadline < started + seconds
        if seconds >= grace + 15:
            assert (
                deadline + live_e2e.WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS
                <= started + seconds + 15
            )


def test_slide_planner_request_bound_exceeds_research_bound():
    """The DeckPlan call is the heaviest schema; it gets the longest bound."""

    assert (
        live_e2e.WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS
        > live_e2e.WORKER_REQUEST_TIMEOUT_SECONDS
    )
    assert live_e2e.WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS <= 300.0


def test_presentation_failure_families_are_classified_by_type_only():
    """Slide-stage failures classify without exposing payloads."""

    from mini_deerflow.pptx.slide_agent import SlideAgentValidationError

    error = SlideAgentValidationError("plan slide count does not match")
    assert live_e2e.classify_failure(error) == "structured_validation"
    wrapped = RuntimeError("opaque")
    wrapped.__cause__ = error
    assert live_e2e.classify_failure(wrapped) == "structured_validation"


def test_provider_timeout_trace_is_classified_not_internal():
    """A provider SDK timeout must surface as provider/model_provider_failure."""

    from mini_deerflow.tracing import (
        TraceErrorCategory,
        TraceErrorCode,
        TraceOutcome,
        _classify_exception,
    )

    timeout_error = type("APITimeoutError", (Exception,), {})("private body")
    assert _classify_exception(timeout_error) == (
        TraceErrorCategory.PROVIDER,
        TraceErrorCode.MODEL_PROVIDER_FAILURE,
        TraceOutcome.PROVIDER_FAILED,
    )
    subclass = type("RateLimitError", (Exception,), {})("private body")
    assert _classify_exception(subclass)[0] is TraceErrorCategory.PROVIDER
    # Existing classifications stay unchanged.
    from mini_deerflow.context_budget import ContextBudgetExceededError

    budget = ContextBudgetExceededError("compaction cannot fit")
    assert _classify_exception(budget)[0] is TraceErrorCategory.CONTEXT_BUDGET
    assert _classify_exception(RuntimeError("x"))[0] is TraceErrorCategory.INTERNAL
    reported = live_e2e.safe_failed_trace(
        {
            "traces": [
                {
                    "outcome": "provider_failed",
                    "node": "planner",
                    "error_category": "provider",
                    "error_code": "model_provider_failure",
                },
            ]
        }
    )
    assert reported == {
        "last_failed_node": "planner",
        "last_trace_error_category": "provider",
        "last_trace_error_code": "model_provider_failure",
    }
