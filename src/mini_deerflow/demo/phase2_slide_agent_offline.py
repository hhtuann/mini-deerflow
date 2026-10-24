"""Credential-free end-to-end Slide Agent demonstration, entirely offline.

The research turn runs the real scripted offline runtime with the explicitly
selected ``slide-creation`` skill. The SAME production ``SlideAgentService``
then plans (injected scripted structured model), converts, renders, validates
and publishes through ``PptxArtifactStore``; a fresh child interpreter
re-downloads the exact bytes through a newly constructed FastAPI app.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand
from mini_deerflow.persistence import normalize_thread_id
from mini_deerflow.pptx.artifacts import PptxArtifactStore
from mini_deerflow.pptx.slide_agent import (
    DeckPlan,
    PresentationRequest,
    SlideAgentService,
)

THREAD = "phase2-slide-agent-demo"
TURN = "turn-1"
SKILL_ID = "slide-creation"

_VI = {
    "overview": "Tổng quan cho {audience}",
    "subtitle": "Tổng hợp bằng chứng đã xác thực dành cho {audience}",
    "bullets_title": "Điểm chính về {topic}",
    "bullet_topic": "Chủ đề: {topic}",
    "bullet_audience": "Đối tượng: {audience}",
    "bullet_evidence": "Dựa trên {count} nguồn bằng chứng của lượt nghiên cứu.",
    "strengths": "Điểm mạnh",
    "limits": "Hạn chế",
    "compare_title": "Bằng chứng và hạn chế",
    "metric_title": "Phạm vi bằng chứng",
    "metric_label": "Nguồn bằng chứng đã xác thực",
    "metric_insight": "Số nguồn liên quan được ghi nhận trong lượt nghiên cứu.",
    "steps_title": "Các bước đọc bản trình chiếu",
    "chart_title": "Xu hướng minh họa theo quý",
    "chart_caption": "Xu hướng theo quý (minh họa cấu trúc)",
    "table_title": "Tổng hợp thành phần",
    "table_caption": "Thành phần bộ trình chiếu",
    "conclusion_title": "Kết luận và nguồn",
    "conclusion_1": "Nội dung chính bám theo bằng chứng của lượt nghiên cứu.",
    "conclusion_2": "Số liệu trong biểu đồ và bảng chỉ mang tính minh họa.",
}

_EN = {
    "overview": "Overview for {audience}",
    "subtitle": "Validated evidence summary for {audience}",
    "bullets_title": "Key points on {topic}",
    "bullet_topic": "Topic: {topic}",
    "bullet_audience": "Audience: {audience}",
    "bullet_evidence": "Grounded in {count} evidence sources from the research turn.",
    "strengths": "Strengths",
    "limits": "Limits",
    "compare_title": "Evidence and limitations",
    "metric_title": "Evidence coverage",
    "metric_label": "Validated evidence sources",
    "metric_insight": "Sources relevant to the topic recorded this turn.",
    "steps_title": "How to read this deck",
    "chart_title": "Illustrative quarterly trend",
    "chart_caption": "Quarterly trend (chart structure demo)",
    "table_title": "Deck composition",
    "table_caption": "Presentation components",
    "conclusion_title": "Conclusion and sources",
    "conclusion_1": "Core content follows the turn's research evidence.",
    "conclusion_2": "Chart and table numbers are illustrative only.",
}


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build_scripted_plan(payload: dict) -> DeckPlan:
    """Derive a plan from the captured model-facing payload (no hardcoding)."""

    brief = payload.get("presentation") or {}
    evidence = payload.get("evidence") or []
    language = brief.get("language", "vi")
    strings = _EN if language == "en" else _VI
    topic = _clip(str(brief.get("topic", "")), 60)
    audience = _clip(str(brief.get("audience", "")), 40)
    count = int(brief.get("slide_count", 8))
    values = {
        "topic": topic,
        "audience": audience,
        "count": len(evidence),
    }

    def t(key: str, limit: int) -> str:
        return _clip(strings[key].format(**values), limit)

    middle = [
        ("comparison", "compare_title"),
        ("metric", "metric_title"),
        ("steps", "steps_title"),
        ("chart", "chart_title"),
        ("table", "table_title"),
    ]
    chosen = [("bullets", "bullets_title")] + middle[: max(0, count - 3)]
    chosen = chosen[: max(0, count - 1)]
    layouts = [("title", "overview")] + chosen + [("conclusion", "conclusion_title")]

    slides: list[dict] = []
    for layout, title_key in layouts:
        base = {
            "layout": layout,
            "title": t(title_key, 38),
            "purpose": f"Serve {audience} with layout {layout}.",
            "evidence_keys": [],
            "payload": {},
        }
        if layout == "title":
            base["payload"] = {"subtitle": t("subtitle", 130)}
        elif layout == "bullets":
            base["payload"] = {
                "items": [
                    t("bullet_topic", 110),
                    t("bullet_audience", 110),
                    t("bullet_evidence", 110),
                ]
            }
            base["evidence_keys"] = list(range(min(3, len(evidence))))
        elif layout == "comparison":
            base["payload"] = {
                "left": {
                    "heading": _clip(strings["strengths"], 20),
                    "items": [t("bullet_topic", 80), t("bullet_evidence", 80)],
                },
                "right": {
                    "heading": _clip(strings["limits"], 20),
                    "items": [t("conclusion_2", 80), t("bullet_audience", 80)],
                },
            }
        elif layout == "metric":
            base["payload"] = {
                "value": str(len(evidence))[:36],
                "label": t("metric_label", 80),
                "insight": t("metric_insight", 180),
            }
        elif layout == "steps":
            base["payload"] = {
                "items": [
                    _clip(item, 58)
                    for item in (
                        strings["conclusion_1"],
                        strings["conclusion_2"],
                        strings["metric_label"],
                        strings["steps_title"],
                    )
                ][: max(2, min(5, count))]
            }
        elif layout == "chart":
            base["payload"] = {
                "chart_type": "column",
                "categories": ["Quý 1", "Quý 2", "Quý 3"]
                if language == "vi"
                else ["Q1", "Q2", "Q3"],
                "series": [
                    {"name": _clip(strings["chart_title"], 30), "values": [2, 3, 4]},
                    {"name": _clip(strings["table_title"], 30), "values": [1, 2, 3]},
                ],
                "caption": t("chart_caption", 110),
            }
        elif layout == "table":
            base["payload"] = {
                "headers": [
                    _clip(strings["table_title"], 18),
                    "SL",
                    _clip(strings["limits"], 18),
                ],
                "rows": [
                    [
                        _clip(strings["conclusion_title"], 22),
                        1,
                        _clip(strings["conclusion_1"], 22),
                    ],
                    [
                        _clip(strings["chart_title"], 22),
                        1,
                        _clip(strings["conclusion_2"], 22),
                    ],
                ],
                "caption": t("table_caption", 110),
            }
        elif layout == "conclusion":
            base["payload"] = {
                "items": [t("conclusion_1", 105), t("conclusion_2", 105)],
                "source_keys": list(range(min(3, len(evidence)))),
            }
        slides.append(base)

    return DeckPlan.model_validate(
        {
            "deck_title": t("overview", 80),
            "language": language,
            "slides": slides,
        }
    )


class ScriptedSlidePlanner:
    """Fake StructuredChatModel capturing the model-facing payload."""

    def __init__(self, *, fail_first: int = 0) -> None:
        self.captures: list[dict] = []
        self._fail_first = fail_first
        self._calls = 0

    def _plan_from_messages(self, messages) -> DeckPlan:
        payload = json.loads(messages[1][1])
        skill = payload.get("active_skill") or {}
        self.captures.append(
            {
                "payload_chars": len(messages[1][1]),
                "topic": (payload.get("presentation") or {}).get("topic"),
                "audience": (payload.get("presentation") or {}).get("audience"),
                "language": (payload.get("presentation") or {}).get("language"),
                "slide_count": (payload.get("presentation") or {}).get("slide_count"),
                "theme": (payload.get("presentation") or {}).get("theme"),
                "evidence_urls": [
                    item.get("canonical_url") for item in payload.get("evidence") or []
                ],
                "skill_id": skill.get("id"),
                "skill_instruction_chars": len(skill.get("instructions") or ""),
            }
        )
        return build_scripted_plan(payload)

    def with_structured_output(self, schema, *, method):
        planner = self

        class _Runnable:
            def invoke(self, messages):
                planner._calls += 1
                if planner._calls <= planner._fail_first:
                    from pydantic import ValidationError

                    raise ValidationError.from_exception_data(
                        "DeckPlan",
                        [{"type": "missing", "loc": ("slides",), "input": {}}],
                    )
                return planner._plan_from_messages(messages)

            async def ainvoke(self, messages):
                return self.invoke(messages)

        assert schema is DeckPlan
        return _Runnable()

    def invoke(self, messages):
        raise AssertionError("scripted planner uses structured output only")

    async def ainvoke(self, messages):
        raise AssertionError("scripted planner uses structured output only")


REQUEST = PresentationRequest(
    topic="Tác động của trí tuệ nhân tạo đến giáo dục đại học Việt Nam",
    audience="sinh viên năm cuối và giảng viên",
    language="vi",
    slide_count=8,
    theme="academic",
    instructions="Nhấn mạnh bằng chứng và hạn chế của nghiên cứu.",
)


def _services(root: Path):
    backend = OfflineDemoBackend(root)
    planner = ScriptedSlidePlanner()
    service = DemoRuntimeService(backend)
    slide_service = SlideAgentService(
        service,
        PptxArtifactStore(root, mode="offline"),
        planner_model_factory=lambda: planner,
        structured_output_mode="native",
    )
    return service, slide_service, planner


async def _prepare(root: Path) -> dict:
    service, _, _ = _services(root)
    view = await service.run(
        RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
    )
    if view.active_skill_id != SKILL_ID:
        raise RuntimeError("Selected skill was not pinned to the research turn.")
    return {"skill_pinned": True, "turn_completed": True}


def _await_job(client: TestClient, job_id: str, *, timeout: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/offline/jobs/{job_id}").json()
        if snapshot["status"] in {"completed", "failed"}:
            return snapshot
        time.sleep(0.02)
    raise RuntimeError("Presentation job did not finish in time.")


def _drive(root: Path) -> dict:
    _service, slide_service, planner = _services(root)
    url = f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
    with TestClient(
        create_app(root, slide_services={"offline": slide_service})
    ) as client:
        for _ in range(2):  # identical request retried: immutable idempotency
            job = client.post(url, json=json.loads(REQUEST.model_dump_json()))
            if job.status_code != 202:
                raise RuntimeError(f"Generation submission failed: {job.status_code}")
            job_id = job.json()["job_id"]
            snapshot = _await_job(client, job_id)
            if snapshot["status"] != "completed":
                raise RuntimeError("Presentation job failed.")
        conflict = client.post(
            url,
            json=json.loads(
                REQUEST.model_copy(update={"slide_count": 6}).model_dump_json()
            ),
        )
        conflict_job = conflict.json()["job_id"]
        snapshot = _await_job(client, conflict_job)
        if snapshot["status"] != "failed" or "different presentation" not in (
            snapshot["error_message"] or ""
        ):
            raise RuntimeError("Conflicting generation did not fail safely.")
        download = client.get(url)
        if download.status_code != 200:
            raise RuntimeError("Download failed.")
        session = client.get(f"/api/offline/conversations/{THREAD}").json()
        active_skill = session["turns"][0]["run"]["active_skill_id"]
    record, stored = asyncio.run(
        PptxArtifactStore(root, mode="offline").retrieve(THREAD, TURN)
    )
    if download.content != stored:
        raise RuntimeError("Downloaded bytes differ from verified storage.")
    capture = planner.captures[0]
    if capture["skill_id"] != SKILL_ID or capture["skill_instruction_chars"] <= 0:
        raise RuntimeError("Skill guidance never reached the planning payload.")
    if not capture["evidence_urls"]:
        raise RuntimeError("Evidence allowlist never reached the planning payload.")
    if capture["language"] != "vi" or capture["slide_count"] != 8:
        raise RuntimeError("Request fields never reached the planning payload.")
    return {
        "sha256": hashlib.sha256(stored).hexdigest(),
        "byte_size": len(stored),
        "spec_json": record.spec_json,
        "turn_skill_id": active_skill,
        "planning_context": capture,
        "conflict_rejected": True,
    }


def _verify(root: Path) -> dict:
    """Fresh interpreter: fresh services, store and app re-download the bytes."""

    from mini_deerflow.pptx import validate_pptx
    from mini_deerflow.pptx.schemas import DeckSpec

    _, slide_service, _ = _services(root)
    url = f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
    with TestClient(
        create_app(root, slide_services={"offline": slide_service})
    ) as client:
        download = client.get(url)
        if download.status_code != 200:
            raise RuntimeError("Fresh-process download failed.")
        record, stored = asyncio.run(
            PptxArtifactStore(root, mode="offline").retrieve(THREAD, TURN)
        )
        if download.content != stored:
            raise RuntimeError("Fresh download differs from storage.")
        spec = DeckSpec.model_validate_json(record.spec_json)
        report = validate_pptx(stored, spec, min_layouts=4)
        session = client.get(f"/api/offline/conversations/{THREAD}").json()
        turn_status = session["turns"][0]["status"]
        other = client.get(
            f"/api/offline/conversations/unknown-{THREAD}/turns/{TURN}/presentation"
        )
        if other.status_code != 404:
            raise RuntimeError("Presentation leaked across conversations.")
    deck = {
        "slide_count": report.slide_count,
        "layouts": list(report.layouts),
        "native_objects": report.native_objects,
        "titles": [slide.title for slide in report.slides],
        "vietnamese_title": report.slides[0].title,
        "illustrative_markers": sum(
            1
            for slide in report.slides
            for text in slide.text
            if "minh họa" in text or "illustrative" in text.lower()
        ),
        "sources": report.slides[-1].text[-2:],
    }
    if report.slide_count != 8 or len(report.layouts) < 4:
        raise RuntimeError("Deck does not satisfy 8 slides / 4 layouts.")
    if turn_status != "completed":
        raise RuntimeError("Owning turn is not completed.")
    return {
        "valid": True,
        "fresh_process": True,
        "download_matches_storage": True,
        "conversation_isolated": True,
        "turn_status": turn_status,
        "sha256": hashlib.sha256(stored).hexdigest(),
        "byte_size": len(stored),
        "deck": deck,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path)
    parser.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_only and args.storage_root is None:
        parser.error("--verify-only requires --storage-root")
    root = (
        args.storage_root or Path(mkdtemp(prefix="mini-deerflow-slide-agent-"))
    ).resolve()
    normalize_thread_id(THREAD)
    if args.verify_only:
        print(json.dumps(_verify(root), ensure_ascii=True))
        return
    root.mkdir(parents=True, exist_ok=True)
    if (root / "checkpoints.sqlite").exists():
        raise RuntimeError("Choose a fresh demonstration storage directory.")
    asyncio.run(_prepare(root))
    evidence = _drive(root)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            __spec__.name,
            "--verify-only",
            "--storage-root",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    verified = json.loads(child.stdout)
    if verified["sha256"] != evidence["sha256"]:
        raise RuntimeError("Artifact changed across process restart.")
    report = {
        "storage_root": str(root),
        "skill_pinned": evidence["turn_skill_id"],
        "planning_context": evidence["planning_context"],
        "conflict_rejected": evidence["conflict_rejected"],
        **verified,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
