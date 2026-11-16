"""S3 Excel/Word exports attached to analysis completion. No real AWS or broker.

Presigned URLs in this file are fake (``download.example.invalid``). The
format matches ``mock_analysis_export_s3`` in ``tests/conftest.py``.
"""

import json
from io import BytesIO
from unittest.mock import Mock

import pytest
from docx import Document
from openpyxl import load_workbook

import app.export.local_export as local_export
import app.messaging.consumer as consumer
import app.messaging.export_consumer as export_consumer
import app.storage.s3_client as s3_client
from app.export.ai_recommendation import recommendation_from_evidence_match
from app.export.excel import (
    NO_DECISION,
    PENDING_REVIEW,
    _ai_recommendation,
    render_excel,
)
from app.export.suggested_actions import (
    SUGGESTED_STATUS,
    merge_suggested_actions,
)
from app.export.local_export import (
    EXCEL_FILENAME,
    EXPORT_FILE_ITEM_KEYS,
    PRESIGNED_URL_EXPIRES_DEFAULT_SECONDS,
    WORD_FILENAME,
    LocalExportError,
    attach_export_files,
    snapshot_from_completed_response,
    upload_analysis_exports,
)
from app.messaging.consumer import outbound_completion_payload
from app.export.snapshot import DRAFT_PENDING_REVIEW_LABEL
from app.export.word import NO_APPROVED_SECTIONS_TEXT, PENDING_DRAFTS_HEADING
from app.messaging.export_consumer import DOCX_CONTENT_TYPE, XLSX_CONTENT_TYPE
from app.response.response_builder import build_completed_response


def _download_url(key):
    return f"https://download.example.invalid/{key}"


def _export_item(file_name, key, bucket="analysis-export-bucket", expires_in=None):
    if expires_in is None:
        expires_in = PRESIGNED_URL_EXPIRES_DEFAULT_SECONDS
    return {
        "fileName": file_name,
        "downloadUrl": _download_url(key),
        "bucket": bucket,
        "key": key,
        "expiresIn": expires_in,
    }


def _request(**overrides):
    message = {
        "organizationId": "org 1",
        "workspaceId": "ws-1",
        "jobId": "job-1",
        "analysisId": "analysis-1",
        "correlationId": "corr-1",
        "documents": [{
            "documentId": "doc-1",
            "filename": "tender.docx",
            "bucket": "test-bucket",
            "key": "tender.docx",
        }],
        "tender": {
            "tenderName": "Synthetic Tender",
            "customer": "Synthetic Customer",
            "tenderReference": "REF-1",
            "submissionDeadline": "2099-01-01T00:00:00Z",
        },
    }
    message.update(overrides)
    return message


def _extracted():
    return [{
        "text": "Synthetic requirement for export.",
        "mandatory": True,
        "confidence": "High",
        "source": {
            "page": 3,
            "section": "2",
            "clause": "2.1",
            "excerpt": "Synthetic excerpt.",
        },
        "evidence_match": {
            "status": "REVIEW_REQUIRED",
            "note": "Synthetic note.",
            "evidence": [{
                "text": "Synthetic evidence.",
                "source": "kb-synthetic.pdf",
                "page": 2,
                "similarity": 0.5,
            }],
        },
    }]


def _completed(**overrides):
    return build_completed_response(
        _request(**overrides),
        _extracted(),
    )


def _keys():
    prefix = "exports/org_1/ws-1/job-1/analysis-1"
    return f"{prefix}/{EXCEL_FILENAME}", f"{prefix}/{WORD_FILENAME}"


@pytest.fixture(autouse=True)
def isolated_consumer(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "DEBUG_RESPONSE_PATH", tmp_path / "response.json")
    monkeypatch.setattr(consumer, "COMPLETION_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(consumer, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(consumer, "summarize_tender_document", lambda *args, **kwargs: {})


def _channel():
    channel = Mock()
    channel.is_open = True
    return channel


def _method(tag, redelivered=False):
    method = Mock()
    method.delivery_tag = tag
    method.redelivered = redelivered
    return method


def _forbid(monkeypatch, name, target):
    probe = Mock(side_effect=AssertionError(name))
    monkeypatch.setattr(target, probe)
    return probe


def _rows(sheet):
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def _use_env_bucket(monkeypatch):
    monkeypatch.setattr(local_export, "default_bucket", export_consumer.default_bucket)
    monkeypatch.delenv("S3_EXPORT_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)


# Mapping and uploads ---------------------------------------------------------

def test_snapshot_uses_analysis_fields_and_leaves_decisions_empty():
    response = _completed()
    snapshot = snapshot_from_completed_response(response)
    requirement = snapshot["requirements"][0]

    assert snapshot["tender"] == {
        "title": "Synthetic Tender",
        "customerName": "Synthetic Customer",
        "tenderReference": "REF-1",
        "submissionDeadline": "2099-01-01T00:00:00Z",
    }
    assert requirement["requirementId"] == "REQ-001"
    assert requirement["sourceItemKey"] == "REQ-001"
    assert requirement["content"] == "Synthetic requirement for export."
    assert requirement["mandatory"] is True
    assert requirement["confidence"] == 0.9
    assert requirement["evidenceMatch"]["status"] == "REVIEW_REQUIRED"
    assert requirement["sources"][0]["excerpt"] == "Synthetic excerpt."
    assert requirement["aiRecommendation"] == {
        "recommendation": "NEEDS_HUMAN_REVIEW",
        "rationale": (
            "Retrieved evidence requires reviewer verification. "
            "It does not prove compliance."
        ),
    }
    assert "decision" not in requirement
    assert snapshot["actions"] == [{
        "actionId": "AI-SUGGESTED-REQ-001",
        "requirementId": "REQ-001",
        "title": "AI suggested: Verify retrieved CBS evidence",
        "description": (
            "Verify the retrieved CBS evidence against this requirement. "
            "Cited source: kb-synthetic.pdf, p.2. AI-suggested only; "
            "not a completed task or human decision."
        ),
        "status": SUGGESTED_STATUS,
    }]
    assert "owner" not in snapshot["actions"][0]
    assert "dueDate" not in snapshot["actions"][0]
    assert snapshot["draftSections"] == []
    assert "decision" not in response["result"]["requirements"][0]
    assert "aiRecommendation" not in response["result"]["requirements"][0]
    assert "actions" not in response
    assert "actions" not in response["result"]


def test_both_reports_are_uploaded_with_presigned_urls(
    tmp_path, monkeypatch, capsys, mock_analysis_export_s3,
):
    monkeypatch.chdir(tmp_path)
    response = _completed()
    files = upload_analysis_exports(response)

    excel_key, word_key = _keys()
    uploads = mock_analysis_export_s3["uploads"]
    presigns = mock_analysis_export_s3["presigns"]
    assert [item["key"] for item in uploads] == [excel_key, word_key]
    assert [item["content_type"] for item in uploads] == [
        XLSX_CONTENT_TYPE,
        DOCX_CONTENT_TYPE,
    ]
    assert {item["bucket"] for item in uploads} == {"analysis-export-bucket"}
    assert PRESIGNED_URL_EXPIRES_DEFAULT_SECONDS == 345600
    assert [(item["bucket"], item["key"], item["expires_in"]) for item in presigns] == [
        ("analysis-export-bucket", excel_key, 345600),
        ("analysis-export-bucket", word_key, 345600),
    ]
    assert files == [
        _export_item(EXCEL_FILENAME, excel_key),
        _export_item(WORD_FILENAME, word_key),
    ]
    for item in files:
        assert tuple(item) == EXPORT_FILE_ITEM_KEYS
        assert set(item) == set(EXPORT_FILE_ITEM_KEYS)
    assert list(tmp_path.rglob("*.xlsx")) == []
    assert list(tmp_path.rglob("*.docx")) == []

    logged = capsys.readouterr().out
    assert "Analysis export bucket source: S3_EXPORT_BUCKET" in logged
    assert "analysis-export-bucket" not in logged
    for item in files:
        assert item["downloadUrl"] not in logged

    workbook = load_workbook(BytesIO(uploads[0]["data"]))
    assert workbook.sheetnames == [
        "Tender Overview",
        "Requirements",
        "Compliance Matrix",
        "Actions",
        "AI Draft Responses",
    ]
    requirements = _rows(workbook["Requirements"])
    assert requirements[1][0] == "REQ-001"
    assert "Synthetic requirement for export." in requirements[1]
    assert "Synthetic excerpt." in requirements[1]
    compliance = dict(zip(
        _rows(workbook["Compliance Matrix"])[0],
        _rows(workbook["Compliance Matrix"])[1],
    ))
    assert compliance["Final Decision"] == PENDING_REVIEW == NO_DECISION
    assert compliance["AI Recommendation"] == (
        "NEEDS_HUMAN_REVIEW: Retrieved evidence requires reviewer "
        "verification. It does not prove compliance."
    )
    assert compliance["AI Evidence Status"] == "REVIEW_REQUIRED"
    assert _rows(workbook["Actions"])[0][2] == "Title"
    actions_row = dict(zip(
        _rows(workbook["Actions"])[0],
        _rows(workbook["Actions"])[1],
    ))
    assert actions_row["Action ID"] == "AI-SUGGESTED-REQ-001"
    assert actions_row["Requirement ID"] == "REQ-001"
    assert actions_row["Title"].startswith("AI suggested:")
    assert "kb-synthetic.pdf" in actions_row["Description"]
    assert "p.2" in actions_row["Description"]
    assert actions_row["Status"] == SUGGESTED_STATUS
    assert actions_row["Owner"] in (None, "")
    assert actions_row["Due Date"] in (None, "")
    assert len(_rows(workbook["Actions"])) == 2

    document = Document(BytesIO(uploads[1]["data"]))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    table_text = "\n".join(
        cell.text
        for table in document.tables
        for row in table.rows
        for cell in row.cells
    )
    assert "Synthetic Tender" in text
    assert "Synthetic Customer" in table_text
    assert NO_APPROVED_SECTIONS_TEXT in text
    assert "Invented draft" not in text
    assert "exportFiles" not in response
    draft_sheet = _rows(workbook["AI Draft Responses"])
    assert draft_sheet[0][3] == "Review Status"
    assert DRAFT_PENDING_REVIEW_LABEL in draft_sheet[1]


def test_analysis_export_uses_response_draft_sections(
    tmp_path, monkeypatch, mock_analysis_export_s3,
):
    monkeypatch.chdir(tmp_path)
    response = _completed()
    response["draftSections"] = [{
        "sectionId": "DRAFT-TECHNICAL_APPROACH",
        "sectionType": "TECHNICAL_APPROACH",
        "title": "Proposed Technical Approach",
        "content": "Pending technical draft for any tender.",
        "status": "DRAFT",
        "approvalStatus": "DRAFT",
        "sourceRequirementIds": ["REQ-001"],
        "sources": [{"filename": "tender.docx", "page": 2}],
        "missingInformation": ["Support coverage is not evidenced."],
        "order": 3,
        "approvedBy": None,
    }]
    snapshot = snapshot_from_completed_response(response)
    assert snapshot["draftSections"][0]["sectionId"] == "DRAFT-TECHNICAL_APPROACH"
    assert snapshot["draftSections"][0]["approvalStatus"] == "DRAFT"

    files = upload_analysis_exports(response)
    uploads = mock_analysis_export_s3["uploads"]
    assert [item["key"] for item in uploads] == list(_keys())
    workbook = load_workbook(BytesIO(uploads[0]["data"]))
    headers = _rows(workbook["AI Draft Responses"])[0]
    row = dict(zip(headers, _rows(workbook["AI Draft Responses"])[1]))
    assert row["Section Type"] == "TECHNICAL_APPROACH"
    assert row["Generated Content"] == "Pending technical draft for any tender."
    assert row["Review Status"] == DRAFT_PENDING_REVIEW_LABEL
    assert row["Related Requirement IDs"] == "REQ-001"
    assert "tender.docx" in row["Source References"]
    assert "Support coverage is not evidenced." in row["Missing Information"]
    document = Document(BytesIO(uploads[1]["data"]))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    pending_at = text.index(PENDING_DRAFTS_HEADING)
    assert "Pending technical draft for any tender." not in text[:pending_at]
    assert "Pending technical draft for any tender." in text[pending_at:]
    assert files[0]["fileName"] == EXCEL_FILENAME
    assert files[1]["fileName"] == WORD_FILENAME


def _snapshot_with_evidence(status, **match_extra):
    response = _completed()
    match = {"status": status, "note": "n", "evidence": []}
    match.update(match_extra)
    response["result"]["requirements"][0]["evidenceMatch"] = match
    return snapshot_from_completed_response(response)


def test_review_required_maps_to_needs_human_review():
    snapshot = _snapshot_with_evidence("REVIEW_REQUIRED")
    rec = snapshot["requirements"][0]["aiRecommendation"]
    assert rec["recommendation"] == "NEEDS_HUMAN_REVIEW"
    assert "reviewer verification" in rec["rationale"]
    assert "decision" not in snapshot["requirements"][0]


def test_not_found_maps_to_evidence_required():
    rec = _snapshot_with_evidence("NOT_FOUND")["requirements"][0]["aiRecommendation"]
    assert rec == recommendation_from_evidence_match({"status": "NOT_FOUND"})
    assert rec["recommendation"] == "EVIDENCE_REQUIRED"
    assert "No acceptable CBS evidence" in rec["rationale"]


def test_not_checked_maps_to_not_checked():
    rec = _snapshot_with_evidence("NOT_CHECKED")["requirements"][0]["aiRecommendation"]
    assert rec["recommendation"] == "NOT_CHECKED"
    assert "could not be completed" in rec["rationale"]


def test_missing_and_unknown_evidence_status_are_not_invented():
    response = _completed()
    response["result"]["requirements"][0].pop("evidenceMatch")
    snapshot = snapshot_from_completed_response(response)
    assert "aiRecommendation" not in snapshot["requirements"][0]

    unknown = _snapshot_with_evidence("COMPLIANT")
    assert "aiRecommendation" not in unknown["requirements"][0]
    assert snapshot["actions"] == []
    assert unknown["actions"] == []
    assert recommendation_from_evidence_match({"status": "COMPLIANT"}) is None
    assert recommendation_from_evidence_match(None) is None
    assert recommendation_from_evidence_match({"status": ""}) is None
    assert recommendation_from_evidence_match({"status": "  "}) is None


def test_excel_recommendation_column_and_final_decision_stay_separate():
    for status, expected_code in (
        ("REVIEW_REQUIRED", "NEEDS_HUMAN_REVIEW"),
        ("NOT_FOUND", "EVIDENCE_REQUIRED"),
        ("NOT_CHECKED", "NOT_CHECKED"),
    ):
        snapshot = _snapshot_with_evidence(status)
        requirement = snapshot["requirements"][0]
        displayed = _ai_recommendation(requirement["aiRecommendation"])
        assert displayed.startswith(expected_code + ":")
        workbook = load_workbook(BytesIO(render_excel(snapshot)))
        row = dict(zip(
            [cell.value for cell in workbook["Compliance Matrix"][1]],
            [cell.value for cell in workbook["Compliance Matrix"][2]],
        ))
        assert row["AI Recommendation"] == displayed
        assert row["Final Decision"] == PENDING_REVIEW == NO_DECISION
        assert row["AI Evidence Status"] == status
        assert row["Decided By"] in (None, "")
        assert "COMPLIANT" not in displayed
        assert "NON_COMPLIANT" not in displayed


def test_review_required_action_includes_source_and_page():
    snapshot = _snapshot_with_evidence(
        "REVIEW_REQUIRED",
        evidence=[{
            "text": "passage",
            "source": "Karandaaz.pdf",
            "page": 4,
            "similarity": 0.7,
        }],
    )
    action = snapshot["actions"][0]
    assert action["actionId"] == "AI-SUGGESTED-REQ-001"
    assert action["requirementId"] == "REQ-001"
    assert action["title"].startswith("AI suggested:")
    assert "Karandaaz.pdf" in action["description"]
    assert "p.4" in action["description"]
    assert action["status"] == SUGGESTED_STATUS
    assert "owner" not in action
    assert "dueDate" not in action


def test_mandatory_not_found_creates_collect_action():
    snapshot = _snapshot_with_evidence("NOT_FOUND")
    assert snapshot["requirements"][0]["mandatory"] is True
    action = snapshot["actions"][0]
    assert action["title"] == "AI suggested: Identify CBS evidence"
    assert "mandatory" in action["description"].lower()
    assert "missing evidence" not in action["title"].lower()


def test_optional_not_found_does_not_create_action():
    response = _completed()
    response["result"]["requirements"][0]["mandatory"] = False
    response["result"]["requirements"][0]["evidenceMatch"] = {
        "status": "NOT_FOUND", "evidence": [],
    }
    snapshot = snapshot_from_completed_response(response)
    assert snapshot["actions"] == []


def test_not_checked_action_is_retry_not_missing_evidence():
    action = _snapshot_with_evidence("NOT_CHECKED")["actions"][0]
    assert action["title"] == "AI suggested: Investigate evidence matching"
    lowered = action["description"].lower()
    assert "does not mean evidence is missing" in lowered
    assert "collect" not in lowered


def test_duplicate_requirement_gets_one_suggested_action():
    requirement = {
        "requirementId": "REQ-001",
        "mandatory": True,
        "evidenceMatch": {
            "status": "REVIEW_REQUIRED",
            "evidence": [{"source": "a.pdf", "page": 1}],
        },
    }
    actions = merge_suggested_actions([requirement, dict(requirement)])
    assert len(actions) == 1
    assert actions[0]["actionId"] == "AI-SUGGESTED-REQ-001"


def test_existing_java_actions_are_preserved_and_not_duplicated():
    response = _completed()
    existing = {
        "actionId": "act-java-1",
        "requirementId": "REQ-001",
        "title": "Human follow-up",
        "description": "Already assigned",
        "owner": "reviewer-1",
        "dueDate": "2099-02-01",
        "status": "OPEN",
    }
    response["actions"] = [existing]
    snapshot = snapshot_from_completed_response(response)
    assert snapshot["actions"] == [existing]
    assert snapshot["actions"][0]["owner"] == "reviewer-1"


def test_excel_actions_sheet_shows_suggested_status_clearly():
    snapshot = _snapshot_with_evidence("NOT_CHECKED")
    workbook = load_workbook(BytesIO(render_excel(snapshot)))
    row = dict(zip(
        [cell.value for cell in workbook["Actions"][1]],
        [cell.value for cell in workbook["Actions"][2]],
    ))
    assert row["Status"] == SUGGESTED_STATUS
    assert row["Title"].startswith("AI suggested:")
    assert row["Requirement ID"] == "REQ-001"
    overview = "\n".join(
        str(cell.value or "") for row_cells in workbook["Tender Overview"].iter_rows()
        for cell in row_cells
    )
    assert "No actions are available" not in overview


def test_export_bucket_wins_over_fallback(monkeypatch, capsys, mock_analysis_export_s3):
    _use_env_bucket(monkeypatch)
    monkeypatch.setenv("S3_EXPORT_BUCKET", "export-bucket-test")
    monkeypatch.setenv("S3_BUCKET", "fallback-bucket-test")

    upload_analysis_exports(_completed())

    assert {item["bucket"] for item in mock_analysis_export_s3["uploads"]} == {
        "export-bucket-test",
    }
    assert {item["bucket"] for item in mock_analysis_export_s3["presigns"]} == {
        "export-bucket-test",
    }
    logged = capsys.readouterr().out
    assert "Analysis export bucket source: S3_EXPORT_BUCKET" in logged
    assert "export-bucket-test" not in logged
    assert "fallback-bucket-test" not in logged


def test_fallback_bucket_when_export_bucket_blank(
    monkeypatch, capsys, mock_analysis_export_s3,
):
    _use_env_bucket(monkeypatch)
    monkeypatch.setenv("S3_EXPORT_BUCKET", "   ")
    monkeypatch.setenv("S3_BUCKET", "fallback-bucket-test")

    upload_analysis_exports(_completed())

    assert {item["bucket"] for item in mock_analysis_export_s3["uploads"]} == {
        "fallback-bucket-test",
    }
    logged = capsys.readouterr().out
    assert "Analysis export bucket source: S3_BUCKET" in logged
    assert "fallback-bucket-test" not in logged


def test_missing_bucket_fails_before_upload(monkeypatch, mock_analysis_export_s3):
    _use_env_bucket(monkeypatch)

    with pytest.raises(LocalExportError, match="S3_EXPORT_BUCKET"):
        upload_analysis_exports(_completed())

    assert mock_analysis_export_s3["uploads"] == []
    assert mock_analysis_export_s3["presigns"] == []


def test_custom_presign_expiry(monkeypatch, mock_analysis_export_s3):
    monkeypatch.setenv("S3_PRESIGNED_URL_EXPIRES_SECONDS", " 120 ")

    files = upload_analysis_exports(_completed())

    assert [item["expires_in"] for item in mock_analysis_export_s3["presigns"]] == [
        120, 120,
    ]
    assert [item["expiresIn"] for item in files] == [120, 120]


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "3600.5", "1e3", "+60", "True"])
def test_invalid_presign_expiry_fails_before_upload(
    monkeypatch, mock_analysis_export_s3, raw,
):
    monkeypatch.setenv("S3_PRESIGNED_URL_EXPIRES_SECONDS", raw)

    with pytest.raises(LocalExportError, match="S3_PRESIGNED_URL_EXPIRES_SECONDS") as caught:
        upload_analysis_exports(_completed())

    assert mock_analysis_export_s3["uploads"] == []
    assert raw not in str(caught.value)


def test_export_does_not_call_ollama_rag_or_export_worker(
    monkeypatch, mock_analysis_export_s3,
):
    probes = [
        _forbid(monkeypatch, "ollama", "app.llm.ollama_client.generate_response"),
        _forbid(monkeypatch, "extract", "app.llm.ollama_client.extract_requirements"),
        _forbid(monkeypatch, "summary", "app.llm.ollama_client.extract_tender_summary"),
        _forbid(monkeypatch, "relevance", "app.llm.ollama_client.assess_evidence_relevance"),
        _forbid(monkeypatch, "retriever", "app.rag.retriever.TenderRetriever"),
        _forbid(monkeypatch, "embeddings", "app.rag.retriever.create_embeddings"),
        _forbid(monkeypatch, "s3-client", "app.storage.s3_client.create_s3_client"),
        _forbid(monkeypatch, "s3-bytes", "app.storage.s3_client.upload_bytes"),
        _forbid(monkeypatch, "s3-file", "app.storage.s3_client.upload_file"),
        _forbid(monkeypatch, "worker-s3", "app.messaging.export_consumer.upload_bytes"),
        _forbid(
            monkeypatch,
            "worker",
            "app.messaging.export_consumer.process_export_delivery",
        ),
        _forbid(
            monkeypatch,
            "pipeline",
            "app.orchestration.service.process_tender_document",
        ),
    ]
    excel_calls = {"n": 0}
    word_calls = {"n": 0}
    real_excel = local_export.render_excel
    real_word = local_export.render_word

    def count_excel(snapshot):
        excel_calls["n"] += 1
        assert snapshot["actions"][0]["status"] == SUGGESTED_STATUS
        assert snapshot["draftSections"] == []
        assert "decision" not in snapshot["requirements"][0]
        assert snapshot["requirements"][0]["aiRecommendation"]["recommendation"] == (
            "NEEDS_HUMAN_REVIEW"
        )
        return real_excel(snapshot)

    def count_word(snapshot):
        word_calls["n"] += 1
        return real_word(snapshot)

    monkeypatch.setattr(local_export, "render_excel", count_excel)
    monkeypatch.setattr(local_export, "render_word", count_word)

    upload_analysis_exports(_completed())

    assert excel_calls["n"] == 1
    assert word_calls["n"] == 1
    assert len(mock_analysis_export_s3["uploads"]) == 2
    assert len(mock_analysis_export_s3["presigns"]) == 2
    for probe in probes:
        probe.assert_not_called()


@pytest.mark.parametrize("organization_id", [
    "..",
    "../outside",
    "foo/../../etc",
    "..\\..\\secret",
    "",
    "   ",
    ".",
    "/",
    "a/../b",
    None,
])
def test_key_sanitisation_rejects_traversal(
    tmp_path, organization_id, mock_analysis_export_s3,
):
    response = _completed()
    response["organizationId"] = organization_id

    with pytest.raises(LocalExportError):
        upload_analysis_exports(response)

    assert mock_analysis_export_s3["uploads"] == []
    assert list(tmp_path.rglob("*.xlsx")) == []
    assert list(tmp_path.rglob("*.docx")) == []


@pytest.mark.parametrize("sanitized", ["", ".", ".."])
def test_empty_sanitized_segment_fails_without_uploading(
    monkeypatch, mock_analysis_export_s3, sanitized,
):
    monkeypatch.setattr(local_export, "sanitize_segment", lambda value: sanitized)

    with pytest.raises(LocalExportError):
        upload_analysis_exports(_completed())

    assert mock_analysis_export_s3["uploads"] == []


def test_slash_in_id_is_one_key_segment(mock_analysis_export_s3):
    response = _completed()
    response["organizationId"] = "acme/team"
    upload_analysis_exports(response)

    key = mock_analysis_export_s3["uploads"][0]["key"]
    assert key.startswith("exports/acme_team/")
    assert ".." not in key.split("/")
    assert len(key.split("/")) == 6


def test_empty_render_does_not_upload(monkeypatch, mock_analysis_export_s3):
    monkeypatch.setattr(local_export, "render_excel", lambda snapshot: b"")

    with pytest.raises(LocalExportError):
        upload_analysis_exports(_completed())

    assert mock_analysis_export_s3["uploads"] == []


def test_empty_presign_url_does_not_return_partial(monkeypatch, mock_analysis_export_s3):
    monkeypatch.setattr(
        local_export,
        "create_presigned_download_url",
        lambda bucket, key, expires_in: "  ",
    )

    with pytest.raises(LocalExportError, match="empty"):
        upload_analysis_exports(_completed())

    assert len(mock_analysis_export_s3["uploads"]) == 1
    assert mock_analysis_export_s3["uploads"][0]["key"].endswith(EXCEL_FILENAME)


def test_word_upload_failure_does_not_return_partial(monkeypatch, mock_analysis_export_s3):
    real_upload = local_export.upload_bytes

    def upload(data, bucket, key, content_type):
        if str(key).endswith(".docx"):
            raise RuntimeError("denied")
        return real_upload(data, bucket, key, content_type)

    monkeypatch.setattr(local_export, "upload_bytes", upload)

    with pytest.raises(RuntimeError):
        upload_analysis_exports(_completed())

    assert [item["key"].endswith(EXCEL_FILENAME) for item in mock_analysis_export_s3["uploads"]] == [
        True,
    ]


def test_presigned_get_uses_private_object_url(monkeypatch):
    client = Mock()
    client.generate_presigned_url.return_value = "https://download.example.invalid/obj"
    monkeypatch.setattr(s3_client, "create_s3_client", lambda: client)

    url = s3_client.create_presigned_download_url("bucket-a", "exports/a.xlsx", 3600)

    client.generate_presigned_url.assert_called_once_with(
        ClientMethod="get_object",
        Params={"Bucket": "bucket-a", "Key": "exports/a.xlsx"},
        ExpiresIn=3600,
    )
    client.put_object.assert_not_called()
    client.put_object_acl.assert_not_called()
    assert url == "https://download.example.invalid/obj"


@pytest.mark.parametrize("expires_in", [0, -5, True, False, "3600", 1.5, None])
def test_presign_rejects_expiry_before_creating_a_client(monkeypatch, expires_in):
    monkeypatch.setattr(
        s3_client,
        "create_s3_client",
        Mock(side_effect=AssertionError("no client")),
    )

    with pytest.raises(ValueError):
        s3_client.create_presigned_download_url("bucket-a", "exports/a.xlsx", expires_in)


def test_outbound_payload_drops_only_requirements():
    response = attach_export_files(
        _completed(),
        [
            _export_item(EXCEL_FILENAME, "exports/a/tender-export.xlsx"),
            _export_item(WORD_FILENAME, "exports/a/tender-export.docx"),
        ],
    )
    outbound = outbound_completion_payload(response)

    assert "requirements" in response["result"]
    assert len(response["result"]["requirements"]) == 1
    assert "requirements" not in outbound["result"]
    assert outbound["exportFiles"] == response["exportFiles"]
    assert set(outbound) == set(response)
    for key in outbound:
        if key == "result":
            continue
        assert outbound[key] == response[key]
    for field in (
        "title",
        "customerName",
        "tenderReference",
        "submissionDeadline",
        "summary",
        "extractedMetadata",
    ):
        assert field in outbound["result"]
        assert outbound["result"][field] == response["result"][field]


# Consumer integration --------------------------------------------------------

def _run(monkeypatch, message, redelivered=False, publish=None, process=None):
    if process is None:
        process = Mock(return_value=_extracted())
    if publish is None:
        publish = Mock()
    monkeypatch.setattr(consumer, "download_file", Mock())
    monkeypatch.setattr(consumer, "process_tender_document", process)
    monkeypatch.setattr(consumer, "publish_message", publish)
    channel = _channel()
    consumer.callback(
        channel,
        _method(21, redelivered=redelivered),
        Mock(),
        json.dumps(message).encode("utf-8"),
    )
    return channel, publish, process


def test_completion_publishes_download_urls_and_keeps_analysis_fields(
    monkeypatch, mock_analysis_export_s3,
):
    seen = []

    def publish(message):
        assert message["eventType"] == "RFP_ANALYSIS_COMPLETED"
        seen.append(message)

    forbidden = [
        _forbid(monkeypatch, "ollama", "app.llm.ollama_client.generate_response"),
        _forbid(monkeypatch, "retriever", "app.rag.retriever.TenderRetriever"),
        _forbid(monkeypatch, "s3-client", "app.storage.s3_client.create_s3_client"),
        _forbid(monkeypatch, "s3-bytes", "app.storage.s3_client.upload_bytes"),
        _forbid(monkeypatch, "s3-file", "app.storage.s3_client.upload_file"),
        _forbid(monkeypatch, "worker-s3", "app.messaging.export_consumer.upload_bytes"),
        _forbid(
            monkeypatch,
            "worker",
            "app.messaging.export_consumer.process_export_delivery",
        ),
        _forbid(
            monkeypatch,
            "render-upload",
            "app.messaging.export_consumer._render_and_upload",
        ),
    ]
    channel, publish, process = _run(monkeypatch, _request(), publish=publish)

    channel.basic_ack.assert_called_once()
    channel.basic_nack.assert_not_called()
    assert process.call_count == 1
    assert len(seen) == 1
    message = seen[0]
    excel_key, word_key = _keys()
    assert message["exportFiles"] == [
        _export_item(EXCEL_FILENAME, excel_key),
        _export_item(WORD_FILENAME, word_key),
    ]
    assert [item["key"] for item in mock_analysis_export_s3["uploads"]] == [
        excel_key, word_key,
    ]
    built = _completed()
    assert set(message) == set(built) | {"exportFiles"}
    assert "requirements" not in message["result"]
    for key, value in built.items():
        if key in ("eventId", "completedAt"):
            assert message[key]
            continue
        if key == "result":
            expected_result = dict(value)
            expected_result.pop("requirements", None)
            assert message[key] == expected_result
            continue
        assert message[key] == value
    saved = json.loads(consumer.DEBUG_RESPONSE_PATH.read_text(encoding="utf-8"))
    assert saved["exportFiles"] == message["exportFiles"]
    assert saved["result"]["requirements"][0]["content"] == (
        "Synthetic requirement for export."
    )
    assert len(saved["result"]["requirements"]) == 1
    for probe in forbidden:
        probe.assert_not_called()


def test_published_json_omits_requirements_and_keeps_internal_copy(
    monkeypatch, mock_analysis_export_s3,
):
    extracted = [
        {
            "text": f"Requirement {index}",
            "mandatory": True,
            "confidence": "High",
        }
        for index in range(328)
    ]
    seen = []

    def publish(message):
        seen.append(message)

    channel, _publish, process = _run(
        monkeypatch,
        _request(),
        publish=publish,
        process=Mock(return_value=extracted),
    )

    channel.basic_ack.assert_called_once()
    assert process.call_count == 1
    assert len(seen) == 1
    published = seen[0]
    assert published["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert "requirements" not in published["result"]
    for field in (
        "schemaVersion",
        "eventType",
        "eventId",
        "organizationId",
        "workspaceId",
        "jobId",
        "analysisId",
        "correlationId",
        "attempt",
        "status",
        "completedAt",
        "processedDocumentIds",
        "providerName",
        "modelName",
        "promptVersion",
    ):
        assert field in published
    for field in (
        "title",
        "customerName",
        "tenderReference",
        "submissionDeadline",
        "summary",
        "extractedMetadata",
    ):
        assert field in published["result"]
    assert [item["fileName"] for item in published["exportFiles"]] == [
        EXCEL_FILENAME,
        WORD_FILENAME,
    ]
    for item in published["exportFiles"]:
        assert set(item) == set(EXPORT_FILE_ITEM_KEYS)
        assert item["expiresIn"] == 345600
        assert item["bucket"] == "analysis-export-bucket"
    assert len(mock_analysis_export_s3["uploads"]) == 2
    saved = json.loads(consumer.DEBUG_RESPONSE_PATH.read_text(encoding="utf-8"))
    assert len(saved["result"]["requirements"]) == 328
    assert saved["result"]["requirements"][0]["content"] == "Requirement 0"
    assert saved["result"]["requirements"][327]["content"] == "Requirement 327"
    assert saved["exportFiles"] == published["exportFiles"]


def test_render_failure_does_not_publish_or_ack(
    monkeypatch, tmp_path, capsys, mock_analysis_export_s3,
):
    def explode(snapshot):
        raise RuntimeError(
            "SECRET-TENDER-PHRASE " + snapshot["requirements"][0]["content"]
        )

    monkeypatch.setattr(local_export, "render_word", explode)
    channel, publish, process = _run(monkeypatch, _request())

    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=True)
    assert process.call_count == 1
    assert not consumer.DEBUG_RESPONSE_PATH.exists()
    assert not consumer.COMPLETION_LEDGER_PATH.exists()
    assert [item["key"].endswith(EXCEL_FILENAME) for item in mock_analysis_export_s3["uploads"]] == [
        True,
    ]
    assert list(tmp_path.rglob("*.xlsx")) == []
    logged = capsys.readouterr().out
    assert "RuntimeError" in logged
    assert "Analysis export failed" in logged
    assert "SECRET-TENDER-PHRASE" not in logged
    assert "Synthetic requirement for export." not in logged
    assert "analysis-export-bucket" not in logged


def test_upload_failure_does_not_publish_or_ack(monkeypatch, capsys):
    secret = "https://download.example.invalid/secret-upload-token"

    def upload(data, bucket, key, content_type):
        if str(key).endswith(".docx"):
            raise RuntimeError(secret)
        return {}

    monkeypatch.setattr(local_export, "upload_bytes", upload)
    channel, publish, process = _run(monkeypatch, _request())

    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=True)
    assert process.call_count == 1
    assert not consumer.DEBUG_RESPONSE_PATH.exists()
    assert not consumer.COMPLETION_LEDGER_PATH.exists()
    logged = capsys.readouterr().out
    assert "RuntimeError" in logged
    assert "Analysis export failed" in logged
    assert secret not in logged
    assert "analysis-export-bucket" not in logged


def test_presign_failure_does_not_publish_or_ack(monkeypatch, capsys):
    secret = "https://download.example.invalid/secret-presign-token"
    calls = {"n": 0}

    def presign(bucket, key, expires_in):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError(secret)
        return secret

    monkeypatch.setattr(local_export, "create_presigned_download_url", presign)
    channel, publish, process = _run(monkeypatch, _request())

    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=True)
    assert process.call_count == 1
    assert calls["n"] == 2
    assert not consumer.DEBUG_RESPONSE_PATH.exists()
    assert not consumer.COMPLETION_LEDGER_PATH.exists()
    logged = capsys.readouterr().out
    assert "RuntimeError" in logged
    assert "Analysis export failed" in logged
    assert secret not in logged


def test_redelivered_export_failure_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(
        local_export,
        "render_excel",
        Mock(side_effect=OSError("SECRET-TENDER-PHRASE disk")),
    )
    channel, publish, _process = _run(monkeypatch, _request(), redelivered=True)

    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=False)
    logged = capsys.readouterr().out
    assert "OSError" in logged
    assert "SECRET-TENDER-PHRASE" not in logged


def test_traversal_id_does_not_publish(monkeypatch, tmp_path):
    message = _request()
    message["jobId"] = "foo/../../etc"
    channel, publish, _process = _run(monkeypatch, message)

    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=True)
    assert list(tmp_path.rglob("*.xlsx")) == []
    assert list(tmp_path.rglob("*.docx")) == []


def test_duplicate_delivery_does_not_upload_again(monkeypatch, mock_analysis_export_s3):
    calls = {"n": 0}
    real = local_export.render_excel

    def count(snapshot):
        calls["n"] += 1
        return real(snapshot)

    monkeypatch.setattr(local_export, "render_excel", count)
    process = Mock(return_value=_extracted())
    publish = Mock()
    _run(monkeypatch, _request(), publish=publish, process=process)
    channel, publish, process = _run(
        monkeypatch,
        _request(),
        redelivered=True,
        publish=publish,
        process=process,
    )

    assert calls["n"] == 1
    assert len(mock_analysis_export_s3["uploads"]) == 2
    assert process.call_count == 1
    assert publish.call_count == 1
    channel.basic_ack.assert_called_once()


def test_republish_after_failed_publish_reuploads_the_same_keys(
    monkeypatch, mock_analysis_export_s3,
):
    first = Mock(side_effect=RuntimeError("broker down"))
    channel, _publish, _process = _run(monkeypatch, _request(), publish=first)
    channel.basic_nack.assert_called_once_with(delivery_tag=21, requeue=True)
    assert consumer.DEBUG_RESPONSE_PATH.exists()
    first_keys = [item["key"] for item in mock_analysis_export_s3["uploads"]]

    seen = []

    def second(message):
        seen.append(message["exportFiles"])

    _run(monkeypatch, _request(), redelivered=True, publish=second)
    second_keys = [item["key"] for item in mock_analysis_export_s3["uploads"]]
    assert second_keys == first_keys + first_keys
    assert seen == [[
        _export_item(EXCEL_FILENAME, first_keys[0]),
        _export_item(WORD_FILENAME, first_keys[1]),
    ]]
    saved = json.loads(consumer.DEBUG_RESPONSE_PATH.read_text(encoding="utf-8"))
    assert saved["exportFiles"] == seen[0]
    for item in saved["exportFiles"]:
        assert set(item) == set(EXPORT_FILE_ITEM_KEYS)


def test_export_worker_entrypoint_remains_importable():
    assert export_consumer.start_export_consumer
    assert export_consumer._render_and_upload
