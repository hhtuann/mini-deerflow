"""D5 immutable reports using synthetic persisted D4 evidence only."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from pydantic import ValidationError

from mini_deerflow.document_qa import DocumentQAService
from mini_deerflow.document_reports import (
    ReportConflictError,
    ReportIntegrityError,
    ReportLimits,
    ReportNotFoundError,
    ReportRequest,
    ReportRevisionRequest,
    ReportSectionRequest,
    ReportStore,
    ReportValidationError,
    ReportWriterAgent,
)
from tests.test_document_qa import seed


def run(value):
    return asyncio.run(value)


def setup(tmp_path):
    documents, workspace, docs = seed(tmp_path)
    reports = ReportStore(tmp_path)
    writer = ReportWriterAgent(documents, reports, DocumentQAService())
    request = ReportRequest(
        title="Revenue report",
        kind="report",
        document_ids=(docs[0][0],),
        sections=(
            ReportSectionRequest(
                section_id="overview", title="Overview", question="Revenue"
            ),
            ReportSectionRequest(
                section_id="detail", title="Detail", question="Revenue"
            ),
        ),
    )
    return documents, workspace, docs, reports, writer, request


def test_create_restart_history_and_immutable_provenance(tmp_path):
    _, workspace, docs, reports, writer, request = setup(tmp_path)
    report = run(writer.create(workspace, request))
    assert report.version == 1 and report.parent_version is None
    assert report.sections[0].claims[0].text == "Revenue is 10 million."
    assert report.sections[0].claims[0].citations[0].document_id == docs[0][0]
    assert report.sections[0].origin_version == 1
    assert not report.sections[0].approved
    restarted = ReportStore(tmp_path)
    run(restarted.recover())
    assert run(restarted.get(workspace, report.report_id)) == report
    assert run(reports.history(workspace, report.report_id)) == (report,)
    with pytest.raises(ValidationError):
        report.title = "changed"
    with pytest.raises(ReportNotFoundError):
        run(reports.get("other", report.report_id))


def test_revision_preserves_approved_untouched_section(tmp_path):
    _, workspace, _, reports, writer, request = setup(tmp_path)
    first = run(writer.create(workspace, request))
    approved = run(
        writer.revise(
            workspace,
            first.report_id,
            ReportRevisionRequest(
                base_version=1, sections=(), approved_section_ids=("overview", "detail")
            ),
        )
    )
    revised = run(
        writer.revise(
            workspace,
            first.report_id,
            ReportRevisionRequest(
                base_version=2,
                sections=(
                    ReportSectionRequest(
                        section_id="detail", title="New detail", question="Revenue"
                    ),
                ),
            ),
        )
    )
    assert revised.sections[0] == approved.sections[0]
    assert revised.sections[0].approved
    assert not revised.sections[1].approved
    assert revised.sections[1].origin_version == 3
    assert revised.parent_version == 2
    assert run(reports.get(workspace, first.report_id, 1)) == first
    assert len(run(reports.history(workspace, first.report_id))) == 3
    with pytest.raises(ReportConflictError):
        run(
            writer.revise(
                workspace,
                first.report_id,
                ReportRevisionRequest(
                    base_version=1, approved_section_ids=("overview",)
                ),
            )
        )


def test_source_deleted_keeps_history_but_blocks_new_generation(tmp_path):
    documents, workspace, docs, reports, writer, request = setup(tmp_path)
    first = run(writer.create(workspace, request))
    run(documents.delete_document(workspace, docs[0][0]))
    assert run(reports.get(workspace, first.report_id)) == first
    with pytest.raises(ReportValidationError):
        run(writer.create(workspace, request))
    with pytest.raises(ReportValidationError):
        run(
            writer.revise(
                workspace,
                first.report_id,
                ReportRevisionRequest(base_version=1, sections=(request.sections[0],)),
            )
        )
    assert run(reports.history(workspace, first.report_id)) == (first,)


def test_source_race_after_qa_fails_closed(tmp_path):
    documents, workspace, docs, reports, _, request = setup(tmp_path)

    class RacingQA(DocumentQAService):
        async def answer(self, *args, **kwargs):
            answer = await super().answer(*args, **kwargs)
            await documents.delete_document(workspace, docs[0][0])
            return answer

    writer = ReportWriterAgent(documents, reports, RacingQA())
    with pytest.raises(ReportValidationError):
        run(
            writer.create(
                workspace,
                request.model_copy(update={"sections": (request.sections[0],)}),
            )
        )


def test_model_rejects_client_claims_duplicates_and_unbounded_input(tmp_path):
    _, _, _, _, _, request = setup(tmp_path)
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({**request.model_dump(), "claims": []})
    with pytest.raises(ValidationError):
        ReportRequest.model_validate(
            {
                **request.model_dump(),
                "sections": (request.sections[0], request.sections[0]),
            }
        )
    with pytest.raises(ValidationError):
        ReportRevisionRequest(base_version=True, approved_section_ids=("overview",))
    with pytest.raises(ValidationError):
        ReportSectionRequest(section_id="../x", title="x", question="Revenue")
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({**request.model_dump(), "document_ids": ()})


def test_quota_and_corruption_are_controlled(tmp_path):
    documents, workspace, _, _, _, request = setup(tmp_path)
    reports = ReportStore(
        tmp_path, limits=replace(ReportLimits(), max_reports_per_workspace=1)
    )
    writer = ReportWriterAgent(documents, reports, DocumentQAService())
    report = run(writer.create(workspace, request))
    with pytest.raises(ReportValidationError):
        run(writer.create(workspace, request))
    with sqlite3.connect(reports.database_path) as connection:
        connection.execute("UPDATE report_revisions SET checksum = ?", ("0" * 64,))
    with pytest.raises(ReportIntegrityError):
        run(reports.get(workspace, report.report_id))


def test_concurrent_same_base_has_one_winner(tmp_path):
    _, workspace, _, reports, writer, request = setup(tmp_path)
    report = run(writer.create(workspace, request))
    revision = ReportRevisionRequest(base_version=1, sections=(request.sections[0],))

    async def race():
        return await asyncio.gather(
            writer.revise(workspace, report.report_id, revision),
            writer.revise(workspace, report.report_id, revision),
            return_exceptions=True,
        )

    results = run(race())
    assert sum(isinstance(item, ReportConflictError) for item in results) == 1
    assert len(run(reports.history(workspace, report.report_id))) == 2


def test_interrupted_transaction_is_not_a_visible_revision(tmp_path):
    _, workspace, _, reports, writer, request = setup(tmp_path)
    report = run(writer.create(workspace, request))
    connection = sqlite3.connect(reports.database_path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO report_revisions SELECT workspace_id, report_id, 2, payload, checksum FROM report_revisions WHERE version=1"
    )
    connection.close()  # Simulated interruption before commit.
    restarted = ReportStore(tmp_path)
    run(restarted.recover())
    assert run(restarted.history(workspace, report.report_id)) == (report,)


def test_source_version_race_between_sections_blocks_publication(tmp_path):
    documents, workspace, docs, reports, _, request = setup(tmp_path)

    class RacingQA(DocumentQAService):
        calls = 0

        async def answer(self, *args, **kwargs):
            answer = await super().answer(*args, **kwargs)
            self.calls += 1
            if self.calls == 1:
                await documents.upload_version(
                    workspace,
                    docs[0][0],
                    content_type="text/plain",
                    data=b"Revenue is 20 million.",
                )
            return answer

    with pytest.raises(ReportValidationError):
        run(
            ReportWriterAgent(documents, reports, RacingQA()).create(workspace, request)
        )


def test_history_and_byte_quotas(tmp_path):
    documents, workspace, _, _, _, request = setup(tmp_path)
    reports = ReportStore(
        tmp_path, limits=replace(ReportLimits(), max_versions_per_report=1)
    )
    writer = ReportWriterAgent(documents, reports, DocumentQAService())
    report = run(writer.create(workspace, request))
    with pytest.raises(ReportValidationError):
        run(
            writer.revise(
                workspace,
                report.report_id,
                ReportRevisionRequest(
                    base_version=1, approved_section_ids=("overview",)
                ),
            )
        )
    assert run(reports.get(workspace, report.report_id)) == report
    tiny = ReportStore(
        tmp_path / "tiny", limits=replace(ReportLimits(), max_revision_bytes=10)
    )
    with pytest.raises(ReportValidationError):
        run(
            ReportWriterAgent(documents, tiny, DocumentQAService()).create(
                workspace, request
            )
        )


def test_unknown_ledger_schema_fails_closed(tmp_path):
    _, workspace, _, reports, writer, request = setup(tmp_path)
    report = run(writer.create(workspace, request))
    with sqlite3.connect(reports.database_path) as connection:
        connection.execute("UPDATE report_store_meta SET schema_version=999")
    with pytest.raises(ReportIntegrityError):
        run(ReportStore(tmp_path).get(workspace, report.report_id))


def test_database_hardlink_alias_is_rejected(tmp_path):
    import os

    _, workspace, _, reports, writer, request = setup(tmp_path)
    report = run(writer.create(workspace, request))
    try:
        os.link(reports.database_path, tmp_path / "alias.sqlite")
    except OSError:
        pytest.skip("hardlinks unavailable on this filesystem")
    with pytest.raises(ReportIntegrityError):
        run(reports.get(workspace, report.report_id))


def test_json_array_containers_keep_strict_elements():
    request = ReportRequest.model_validate(
        {
            "title": "Title",
            "kind": "summary",
            "document_ids": ["doc-1"],
            "sections": [
                {"section_id": "s", "title": "Section", "question": "Revenue"}
            ],
        }
    )
    assert request.document_ids == ("doc-1",)
    with pytest.raises(ValidationError):
        ReportRevisionRequest.model_validate(
            {"base_version": "1", "approved_section_ids": ["s"]}
        )
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({**request.model_dump(), "document_ids": [42]})
