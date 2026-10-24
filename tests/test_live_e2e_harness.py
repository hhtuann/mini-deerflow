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
    assert live_e2e.main(["--allow-paid-live", "--timeout-seconds", "901"]) == 2
    assert "timeout must be" in capsys.readouterr().out

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
