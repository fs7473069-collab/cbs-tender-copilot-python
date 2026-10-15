
from datetime import datetime

from app.response.response_builder import (
    build_completed_response,
    confidence_to_score,
)


def sample_request():
    return {
        "organizationId": "constell-dev",
        "workspaceId": "workspace-123",
        "jobId": "job-123",
        "analysisId": "analysis-123",
        "correlationId": "correlation-123",
        "documents": [{"documentId": "document-123"}],
        "tender": {
            "tenderName": "Hospital Equipment Tender",
            "customer": "City Hospital",
            "tenderReference": "HOSP-2026-001",
            "submissionDeadline": "2026-11-01T12:00:00Z",
        },
    }


def sample_requirements():
    return [
        {
            "text": "The supplier must provide a three-year warranty.",
            "mandatory": True,
            "confidence": "High",
            "page": 4,
            "source": {
                "page": 4,
                "section": "Warranty",
                "clause": "4.2",
                "excerpt": "A minimum three-year warranty is required.",
                "confidence": "High",
            },
        }
    ]


def test_confidence_conversion():
    assert confidence_to_score("High") == 0.90
    assert confidence_to_score("Medium") == 0.70
    assert confidence_to_score("Low") == 0.40
    assert confidence_to_score(0.95) == 0.95
    assert confidence_to_score("unknown") is None


def test_response_contains_expected_fields():
    response = build_completed_response(
        sample_request(), sample_requirements()
    )

    expected_fields = {
        "schemaVersion", "eventType", "eventId", "organizationId",
        "workspaceId", "jobId", "analysisId", "correlationId",
        "attempt", "status", "completedAt", "processedDocumentIds",
        "providerName", "modelName", "promptVersion", "result",
    }

    assert expected_fields.issubset(response.keys())
    assert response["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert response["status"] == "COMPLETED"


def test_request_ids_are_preserved():
    response = build_completed_response(
        sample_request(), sample_requirements()
    )

    assert response["workspaceId"] == "workspace-123"
    assert response["jobId"] == "job-123"
    assert response["analysisId"] == "analysis-123"
    assert response["correlationId"] == "correlation-123"
    assert response["processedDocumentIds"] == ["document-123"]


def test_tender_metadata_is_preserved():
    response = build_completed_response(
        sample_request(),
        sample_requirements(),
        tender_metadata={"deliveryLocation": "Karachi"},
    )

    result = response["result"]
    assert result["title"] == "Hospital Equipment Tender"
    assert result["customerName"] == "City Hospital"
    assert result["tenderReference"] == "HOSP-2026-001"
    assert result["submissionDeadline"] == "2026-11-01T12:00:00Z"
    assert result["extractedMetadata"]["deliveryLocation"] == "Karachi"


def test_requirement_and_source_fields():
    response = build_completed_response(
        sample_request(), sample_requirements()
    )

    requirement = response["result"]["requirements"][0]
    source = requirement["sources"][0]

    assert requirement["sourceItemKey"] == "REQ-001"
    assert requirement["content"] == (
        "The supplier must provide a three-year warranty."
    )
    assert requirement["mandatory"] is True
    assert requirement["confidence"] == 0.90

    assert source["documentId"] == "document-123"
    assert source["pageNo"] == 4
    assert source["section"] == "Warranty"
    assert source["clause"] == "4.2"
    assert source["excerpt"] == "A minimum three-year warranty is required."
    assert source["sourceRole"] == "PRIMARY"
    assert source["confidence"] == 0.90


def test_event_id_is_unique_and_timestamp_is_valid():
    first = build_completed_response(
        sample_request(), sample_requirements()
    )
    second = build_completed_response(
        sample_request(), sample_requirements()
    )

    assert first["eventId"] != second["eventId"]
    datetime.fromisoformat(
        first["completedAt"].replace("Z", "+00:00")
    )

def test_empty_requirements_returns_empty_list():
    response = build_completed_response(
        sample_request(),
        [],
    )

    assert response["status"] == "COMPLETED"
    assert response["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert response["result"]["requirements"] == []

def test_multiple_requirements_get_sequential_ids():
    requirements = [
        {"text": "Requirement A", "mandatory": True, "confidence": "High"},
        {"text": "Requirement B", "mandatory": False, "confidence": "Medium"},
        {"text": "Requirement C", "mandatory": True, "confidence": "Low"},
    ]

    response = build_completed_response(sample_request(), requirements)

    extracted = response["result"]["requirements"]

    assert [item["sourceItemKey"] for item in extracted] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
    ]
    assert len(extracted) == 3
   

def test_missing_confidence_is_handled():
    requirements = [
        {
            "text": "The supplier must provide training.",
            "mandatory": True,
        }
    ]

    response = build_completed_response(sample_request(), requirements)

    requirement = response["result"]["requirements"][0]
    source = requirement["sources"][0]

    assert requirement["confidence"] is None
    assert source["confidence"] is None


def test_request_without_documents():
    request = sample_request()
    request["documents"] = []

    response = build_completed_response(
        request,
        sample_requirements(),
    )

    assert response["processedDocumentIds"] == []
    assert response["result"]["requirements"][0]["sources"][0]["documentId"] is None


def test_multiple_documents_are_listed_in_response():
    request = sample_request()
    request["documents"].append(
        {"documentId": "document-456"}
    )

    response = build_completed_response(
        request,
        sample_requirements(),
    )

    assert response["processedDocumentIds"] == [
        "document-123",
        "document-456",
    ]

    # Current behavior: each requirement source uses the first document ID.
    assert (
        response["result"]["requirements"][0]["sources"][0]["documentId"]
        == "document-123"
    )
