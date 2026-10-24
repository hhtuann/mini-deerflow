"""HTTP contract and persistence/security integration over the real offline runtime."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.api.jobs import MAX_JOB_HISTORY
from mini_deerflow.api.security import MAX_REQUEST_BYTES
from mini_deerflow.demo.offline_scenario import (
    FALSE_FACT_CANARY,
    INVENTED_SOURCE,
    MACHINE_PATH_CANARY,
    RAW_PAYLOAD_CANARY,
    SECRET_CANARY,
    UNSAFE_URL,
    OfflineDemoBackend,
)
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand

MESSAGE = "Compare the bounded research evidence carefully."
PREFIX = "/api/offline"


def wait_job(client: TestClient, job: dict) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/api/{job['mode']}/jobs/{job['job_id']}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"completed", "failed"}:
            return result
        time.sleep(0.01)
    pytest.fail("Job did not finish within 20 seconds")


def submit(client: TestClient, url: str = f"{PREFIX}/conversations", **body) -> dict:
    response = client.post(url, json={"message": MESSAGE, **body})
    assert response.status_code == 202, response.text
    job = response.json()
    UUID(job["job_id"])
    assert set(job) == {
        "job_id",
        "mode",
        "thread_id",
        "turn_id",
        "operation",
        "status",
        "error_message",
        "traces",
        "execution_graph",
        "view",
    }
    return job


def assert_safe(text: str, root: Path) -> None:
    text = text.replace("\\\\", "\\")
    for forbidden in (
        SECRET_CANARY,
        RAW_PAYLOAD_CANARY,
        MACHINE_PATH_CANARY,
        FALSE_FACT_CANARY,
        UNSAFE_URL,
        INVENTED_SOURCE,
        str(root),
        "tool_observations",
        "pending_action",
        "checkpoint_path",
        "workspace_root",
        "tool_arguments",
        '"messages"',
        "Traceback",
    ):
        assert forbidden not in text


def test_offline_conversation_followup_skills_artifacts_and_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/health").json() == {
            "status": "ok",
            "modes": ["live", "offline"],
        }
        skills = client.get("/api/skills").json()
        assert skills["available"] is True
        assert {skill["id"] for skill in skills["skills"]} >= {"research-brief"}
        assert all(
            set(skill) == {"id", "description", "applicability", "version"}
            for skill in skills["skills"]
        )
        workflow = client.get("/api/workflow").json()
        assert workflow["nodes"] and workflow["edges"]
        assert client.get(f"{PREFIX}/conversations").json() == []
        first = wait_job(client, submit(client, skill_id="research-brief"))
        assert first["status"] == "completed"
        assert first["view"]["active_skill_id"] == "research-brief"
        assert first["traces"] and first["execution_graph"]["nodes"]
        assert first["error_message"] is None
        thread = first["thread_id"]
        url = f"{PREFIX}/conversations/{thread}"
        history = client.get(f"{PREFIX}/conversations").json()
        assert len(history) == 1 and history[0]["thread_id"] == thread
        session = client.get(url).json()
        assert len(session["turns"]) == 1
        assert session["turns"][0]["turn_id"] == first["turn_id"]
        assert session["turns"][0]["status"] == "completed"
        assert session["turns"][0]["assistant_message"]
        artifact_url = f"{url}/turns/{first['turn_id']}/artifact"
        artifact = client.get(artifact_url)
        assert artifact.status_code == 200
        assert (
            artifact.text == session["turns"][0]["run"]["artifact"]["markdown_source"]
        )
        assert (
            artifact.headers["content-disposition"]
            == 'attachment; filename="report.md"'
        )
        assert artifact.headers["content-type"].startswith("text/markdown")
        assert artifact.headers["x-content-type-options"] == "nosniff"
        second = wait_job(
            client,
            submit(
                client,
                f"{url}/turns",
                message="  Follow up on the previous evidence.  ",
            ),
        )
        assert second["status"] == "completed"
        assert second["operation"] == "continue"
        assert second["view"]["active_skill_id"] is None
        assert second["turn_id"] != first["turn_id"]
        session = client.get(url).json()
        assert len(session["turns"]) == 2
        assert (
            session["turns"][1]["user_message"] == "Follow up on the previous evidence."
        )
        assert session["turns"][0]["run"]["active_skill_id"] == "research-brief"
        assert client.get(artifact_url).text == artifact.text
        jobs = client.get(f"{PREFIX}/jobs").json()
        assert [item["job_id"] for item in jobs] == [first["job_id"], second["job_id"]]
        assert jobs[0] == first  # prior snapshots never inherit a later job's view
        assert client.post(f"{url}/resume").status_code == 409
        assert client.get(f"/api/live/jobs/{first['job_id']}").status_code == 404
        assert client.get("/api/live/jobs").json() == []
        assert client.get(f"/api/live/conversations/{thread}").status_code == 404
        other = wait_job(client, submit(client))
        assert other["status"] == "completed"
        assert (
            client.get(
                f"{PREFIX}/conversations/{other['thread_id']}/turns/{first['turn_id']}/artifact"
            ).status_code
            == 404
        )
        assert client.get(f"{url}/turns/not-a-turn/artifact").status_code == 404
        assert_safe(
            json.dumps([first, second, session, history, jobs, workflow, skills]),
            tmp_path,
        )
        assert_safe(artifact.text, tmp_path)
    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get(f"{PREFIX}/jobs").json() == []
        assert restarted.get(f"{PREFIX}/jobs/{first['job_id']}").status_code == 404
        assert restarted.get(url).json() == session
        assert restarted.get(artifact_url).text == artifact.text
        assert len(restarted.get(f"{PREFIX}/conversations").json()) == 2


@pytest.mark.parametrize("running", [False, True])
def test_persisted_interrupted_or_running_turn_requires_explicit_resume(
    tmp_path, running
):
    backend = OfflineDemoBackend(tmp_path, interrupt_after_delegation=True)
    service = DemoRuntimeService(backend)
    with pytest.raises(RuntimeError, match="deterministic interruption"):
        asyncio.run(service.run(RunDemoCommand(thread_id="resume-thread")))
    if running:
        # Model a process termination that leaves the durable active ledger running.
        import sqlite3

        with sqlite3.connect(tmp_path / "checkpoints.sqlite") as database:
            database.execute("UPDATE turns SET status = 'running'")
            database.execute("UPDATE conversations SET status = 'running'")
    with TestClient(create_app(tmp_path)) as client:
        url = f"{PREFIX}/conversations/resume-thread"
        session = client.get(url).json()
        assert session["turns"][0]["status"] in {"running", "interrupted"}
        assert client.get(f"{PREFIX}/jobs").json() == []
        assert client.post(f"{url}/turns", json={"message": MESSAGE}).status_code == 409
        response = client.post(f"{url}/resume")
        assert response.status_code == 202, response.text
        resumed = wait_job(client, response.json())
        assert resumed["status"] == "completed"
        assert resumed["operation"] == "resume"
        assert resumed["turn_id"] == session["turns"][0]["turn_id"]
        session = client.get(url).json()
        assert len(session["turns"]) == 1
        assert session["turns"][0]["status"] == "completed"
        assert not any(
            trace["kind"] in {"tool", "delegation"} for trace in resumed["traces"]
        )
        assert_safe(json.dumps(resumed), tmp_path)


def test_atomic_concurrency_active_discovery_and_safe_progress(tmp_path):
    entered, release = Event(), Event()

    class PausedBackend(OfflineDemoBackend):
        async def run(self, command, *, trace_sink=None):
            class PausedSink:
                def emit(self, event):
                    trace_sink.emit(event)
                    if not entered.is_set():
                        entered.set()
                        assert release.wait(10)

            return await super().run(command, trace_sink=PausedSink())

    service = DemoRuntimeService(PausedBackend(tmp_path))
    with TestClient(create_app(tmp_path, services={"offline": service})) as client:
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        client.post,
                        f"{PREFIX}/conversations",
                        json={"message": MESSAGE},
                    )
                    for _ in range(2)
                ]
                responses = [future.result(timeout=10) for future in futures]
            assert sorted(response.status_code for response in responses) == [202, 409]
            first = next(
                response.json() for response in responses if response.status_code == 202
            )
            assert entered.wait(5)
            active = client.get(f"{PREFIX}/jobs").json()
            assert len(active) == 1 and active[0]["status"] == "running"
            assert active[0]["traces"]
            assert active[0]["view"] is None
            assert active == client.get(f"{PREFIX}/jobs").json()
            for mode in ("offline", "live"):
                for suffix in (
                    "/conversations",
                    f"/conversations/{first['thread_id']}",
                    f"/conversations/{first['thread_id']}/turns/{first['turn_id']}/artifact",
                ):
                    response = client.get(f"/api/{mode}{suffix}")
                    assert response.status_code == 409
                    assert response.json() == {
                        "detail": "An operation is already active."
                    }
                    assert_safe(response.text, tmp_path)
            assert (
                client.post(
                    "/api/live/conversations", json={"message": MESSAGE}
                ).status_code
                == 409
            )
            assert_safe(json.dumps(active), tmp_path)
        finally:
            release.set()
        assert wait_job(client, first)["status"] == "completed"
        assert len(client.get(f"{PREFIX}/conversations").json()) == 1


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_cold_storage_reads_and_first_submission(tmp_path, attempt):
    root = tmp_path / f"cold-{attempt}"
    paths = (
        (f"{PREFIX}/conversations", 200),
        (f"{PREFIX}/conversations/missing", 404),
        (f"{PREFIX}/conversations/missing/turns/missing/artifact", 404),
    ) * 3
    start = Barrier(len(paths) + 1)

    with TestClient(create_app(root), raise_server_exceptions=False) as client:

        def read(path):
            start.wait(timeout=10)
            return client.get(path)

        def create():
            start.wait(timeout=10)
            return client.post(f"{PREFIX}/conversations", json={"message": MESSAGE})

        with ThreadPoolExecutor(max_workers=len(paths) + 1) as pool:
            reads = [pool.submit(read, path) for path, _ in paths]
            submission = pool.submit(create)
            response = submission.result(timeout=20)
            assert response.status_code == 202, response.text
            for future, (_, idle_status) in zip(reads, paths, strict=True):
                result = future.result(timeout=20)
                assert result.status_code in {idle_status, 409}, result.text
                assert "database is locked" not in result.text
                assert_safe(result.text, root)
        completed = wait_job(client, response.json())
        assert completed["status"] == "completed", completed["error_message"]
        assert completed["error_message"] is None
        assert_safe(json.dumps(completed), root)
        assert len(client.get(f"{PREFIX}/conversations").json()) == 1


@pytest.mark.parametrize("resource", ["list", "detail", "artifact"])
def test_inflight_storage_read_blocks_submission_and_other_reads(tmp_path, resource):
    async def scenario():
        entered, release, post_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        read_active, worker_started, overlap = Event(), Event(), Event()

        class PausedReadBackend(OfflineDemoBackend):
            async def pause_read(self):
                if read_active.is_set():
                    overlap.set()
                if not entered.is_set():
                    read_active.set()
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        read_active.clear()

            async def list_conversations(self):
                await self.pause_read()
                return await super().list_conversations()

            async def load_conversation(self, thread_id):
                await self.pause_read()
                return await super().load_conversation(thread_id)

            async def run(self, command, *, trace_sink=None):
                worker_started.set()
                if read_active.is_set():
                    overlap.set()
                return await super().run(command, trace_sink=trace_sink)

        async def observe_request(request):
            if request.method == "POST":
                post_seen.set()

        paths = {
            "list": (f"{PREFIX}/conversations", 200),
            "detail": (f"{PREFIX}/conversations/missing", 404),
            "artifact": (f"{PREFIX}/conversations/missing/turns/missing/artifact", 404),
        }
        service = DemoRuntimeService(PausedReadBackend(tmp_path))
        app = create_app(tmp_path, services={"offline": service})
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                event_hooks={"request": [observe_request]},
            ) as client,
        ):
            path, expected = paths[resource]
            first = asyncio.create_task(client.get(path))
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = asyncio.create_task(client.get(f"{PREFIX}/conversations"))
            post = asyncio.create_task(
                client.post(f"{PREFIX}/conversations", json={"message": MESSAGE})
            )
            try:
                await asyncio.wait_for(post_seen.wait(), timeout=5)
                assert not post.done()
                assert not second.done()
                assert not worker_started.is_set()
            finally:
                release.set()
                responses = await asyncio.gather(first, second, post)
            assert responses[0].status_code == expected
            assert responses[1].status_code == 200
            assert responses[2].status_code == 202
            job = responses[2].json()
            for _ in range(1000):
                result = await client.get(f"{PREFIX}/jobs/{job['job_id']}")
                job = result.json()
                if job["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "completed", job["error_message"]
            assert not overlap.is_set()
            assert_safe(json.dumps(job), tmp_path)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": "short"},
        {"message": " " * 12},
        {"message": "x" * 1001},
        {"message": 1234567890},
        {"message": MESSAGE, SECRET_CANARY: "secret"},
        {"message": MESSAGE, "skill_id": "../../secret"},
        {"message": MESSAGE, "skill_id": ["research-brief"]},
        {"message": MESSAGE, "skill_id": "missing-skill"},
        {"message": MESSAGE, "thread_id": SECRET_CANARY},
    ],
)
def test_invalid_requests_are_sanitized_and_create_no_jobs(tmp_path, body):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(f"{PREFIX}/conversations", json=body)
        assert response.status_code == 422
        assert SECRET_CANARY not in response.text
        assert "input" not in response.text and "loc" not in response.text
        assert client.get(f"{PREFIX}/jobs").json() == []
        assert client.get(f"{PREFIX}/conversations").json() == []


@pytest.mark.parametrize(
    "url",
    [
        "/api/wrong/conversations",
        "/api/offline/conversations/bad%20id",
        "/api/offline/conversations/.hidden",
        "/api/offline/conversations/" + "x" * 129,
        "/api/offline/jobs/not-a-uuid",
    ],
)
def test_invalid_modes_and_ids(tmp_path, url):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(url).status_code == 422


def test_missing_resources_and_resume_body_validation(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"{PREFIX}/jobs/{uuid4()}").status_code == 404
        url = f"{PREFIX}/conversations/missing"
        assert client.get(url).status_code == 404
        assert client.post(f"{url}/turns", json={"message": MESSAGE}).status_code == 404
        assert client.post(f"{url}/resume").status_code == 404
        response = client.post(f"{url}/resume", json={SECRET_CANARY: "secret"})
        assert response.status_code == 422 and SECRET_CANARY not in response.text
        response = client.post(f"{PREFIX}/conversations", content='{ "' + SECRET_CANARY)
        assert response.status_code == 422 and SECRET_CANARY not in response.text


def test_browser_origin_cors_and_trusted_hosts(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        for origin in (
            "https://evil.example",
            "null",
            "http://localhost:5173.evil.example",
        ):
            response = client.post(
                f"{PREFIX}/conversations",
                json={"message": MESSAGE},
                headers={"Origin": origin},
            )
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        assert (
            client.post(
                f"{PREFIX}/conversations",
                json={"message": MESSAGE},
                headers=[
                    ("Origin", "http://localhost:5173"),
                    ("Origin", "https://evil.example"),
                ],
            ).status_code
            == 403
        )
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = client.options(
                f"{PREFIX}/conversations",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
        assert (
            client.get("/api/health", headers={"Host": "evil.example"}).status_code
            == 400
        )
        assert client.get(f"{PREFIX}/jobs").json() == []
        response = client.post(
            f"{PREFIX}/conversations",
            json={"message": MESSAGE},
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.status_code == 202
        assert (
            response.headers["access-control-allow-origin"] == "http://localhost:5173"
        )
        assert wait_job(client, response.json())["status"] == "completed"


def test_request_size_is_bounded_even_without_or_with_false_content_length(tmp_path):
    app = create_app(tmp_path)

    async def scenario():
        async def chunks():
            for _ in range(5):
                yield b"x" * (MAX_REQUEST_BYTES // 4)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for headers in ({}, {"Content-Length": "1"}):
                response = await client.post(
                    f"{PREFIX}/conversations", content=chunks(), headers=headers
                )
                assert response.status_code == 413
            response = await client.post(
                f"{PREFIX}/conversations",
                content=b"{}",
                headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)},
            )
            assert response.status_code == 413
            assert (await client.get(f"{PREFIX}/jobs")).json() == []

    with TestClient(app):
        asyncio.run(scenario())


def test_bounded_job_history_preserves_durable_conversations(tmp_path, monkeypatch):
    assert MAX_JOB_HISTORY == 32
    monkeypatch.setattr("mini_deerflow.api.jobs.MAX_JOB_HISTORY", 2)
    with TestClient(create_app(tmp_path)) as client:
        completed = [wait_job(client, submit(client)) for _ in range(3)]
        assert all(job["status"] == "completed" for job in completed)
        jobs = client.get(f"{PREFIX}/jobs").json()
        assert [job["job_id"] for job in jobs] == [
            job["job_id"] for job in completed[1:]
        ]
        assert client.get(f"{PREFIX}/jobs/{completed[0]['job_id']}").status_code == 404
        assert len(client.get(f"{PREFIX}/conversations").json()) == 3
        assert (
            client.get(
                f"{PREFIX}/conversations/{completed[0]['thread_id']}"
            ).status_code
            == 200
        )


def test_backend_failures_never_echo_errors(tmp_path):
    class BrokenBackend(OfflineDemoBackend):
        async def run(self, command, *, trace_sink=None):
            raise RuntimeError(f"{SECRET_CANARY} {tmp_path}")

        async def list_conversations(self):
            raise RuntimeError(f"{RAW_PAYLOAD_CANARY} {tmp_path}")

    service = DemoRuntimeService(BrokenBackend(tmp_path))
    with TestClient(
        create_app(tmp_path, services={"offline": service}),
        raise_server_exceptions=False,
    ) as client:
        failed = wait_job(client, submit(client))
        assert failed["status"] == "failed" and failed["view"] is None
        assert failed["error_message"]
        response = client.get(f"{PREFIX}/conversations")
        assert response.status_code == 500
        assert response.json() == {"detail": "Operation failed."}
        assert_safe(json.dumps(failed) + response.text, tmp_path)


def test_unavailable_skill_catalog_is_safe_and_fail_closed(tmp_path, monkeypatch):
    from mini_deerflow.skills import SkillsError

    def fail_discovery(self):
        raise SkillsError(SECRET_CANARY)

    monkeypatch.setattr("mini_deerflow.api.SkillsRegistry.discover", fail_discovery)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/skills").json() == {"skills": [], "available": False}
        response = client.post(
            f"{PREFIX}/conversations",
            json={
                "message": MESSAGE,
                "skill_id": "research-brief",
            },
        )
        assert response.status_code == 503
        assert SECRET_CANARY not in response.text
        assert client.get(f"{PREFIX}/jobs").json() == []


def test_injected_interruption_is_a_safe_failed_job_and_resumes_after_restart(tmp_path):
    interrupted = DemoRuntimeService(
        OfflineDemoBackend(tmp_path, interrupt_after_delegation=True)
    )
    with TestClient(create_app(tmp_path, services={"offline": interrupted})) as client:
        failed = wait_job(client, submit(client, skill_id="research-brief"))
        assert failed["status"] == "failed"
        assert failed["view"] is None
        assert failed["traces"] and failed["execution_graph"]["nodes"]
        assert "deterministic interruption" not in failed["error_message"]
        assert_safe(json.dumps(failed), tmp_path)
        url = f"{PREFIX}/conversations/{failed['thread_id']}"
        before = client.get(url).json()
        assert before["turns"][0]["status"] == "interrupted"
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"{PREFIX}/jobs").json() == []
        assert client.get(url).json() == before
        response = client.post(f"{url}/resume", json={})
        assert response.status_code == 202
        completed = wait_job(client, response.json())
        assert completed["status"] == "completed"
        assert completed["turn_id"] == failed["turn_id"]
        assert completed["view"]["active_skill_id"] == "research-brief"
        assert client.get(f"{PREFIX}/jobs/{failed['job_id']}").status_code == 404


def test_live_transport_with_injected_offline_fixture_is_mode_isolated(tmp_path):
    # Exercise live routing without making provider calls or requiring credentials.
    live_fixture = DemoRuntimeService(OfflineDemoBackend(tmp_path / "live"))
    with TestClient(create_app(tmp_path, services={"live": live_fixture})) as client:
        live = wait_job(client, submit(client, "/api/live/conversations"))
        assert live["status"] == "completed" and live["mode"] == "live"
        thread_id, turn_id = live["thread_id"], live["turn_id"]
        assert client.get("/api/live/conversations").json()[0]["thread_id"] == thread_id
        assert client.get(f"/api/live/conversations/{thread_id}").status_code == 200
        assert client.get(f"{PREFIX}/conversations/{thread_id}").status_code == 404
        assert client.get(f"{PREFIX}/jobs/{live['job_id']}").status_code == 404
        assert client.get(f"{PREFIX}/jobs").json() == []
        artifact = client.get(
            f"/api/live/conversations/{thread_id}/turns/{turn_id}/artifact"
        )
        assert artifact.status_code == 200
        assert artifact.text == live["view"]["artifact"]["markdown_source"]
        assert (
            client.get(
                f"{PREFIX}/conversations/{thread_id}/turns/{turn_id}/artifact"
            ).status_code
            == 404
        )


@pytest.mark.parametrize("length", [10, 1000])
def test_message_length_boundaries_apply_after_stripping(tmp_path, length):
    with TestClient(create_app(tmp_path)) as client:
        completed = wait_job(client, submit(client, message="  " + "x" * length + "  "))
        assert completed["status"] == "completed"
        assert completed["view"]["goal"] == "x" * length


def test_openapi_exposes_only_public_dtos_and_entrypoint_is_loopback(
    tmp_path, monkeypatch
):
    from mini_deerflow.api.__main__ import main

    with TestClient(create_app(tmp_path)) as client:
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        for private_type in (
            "AgentState",
            "SkillContext",
            "ExecutionTrace",
            "Settings",
        ):
            assert private_type not in schema.text
        assert "JobDTO" in schema.json()["components"]["schemas"]
    calls = []
    monkeypatch.setattr(
        "mini_deerflow.api.__main__.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    main()
    assert calls == [
        (
            ("mini_deerflow.api:create_app",),
            {"factory": True, "host": "127.0.0.1", "port": 8000},
        )
    ]
