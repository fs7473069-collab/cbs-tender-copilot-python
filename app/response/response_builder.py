
from datetime import datetime, timezone
from uuid import uuid4


def confidence_to_score(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if 0 <= value <= 1 else None

    if isinstance(value, str):
        return {
            "high": 0.90,
            "medium": 0.70,
            "low": 0.40,
        }.get(value.strip().lower())

    return None


def build_completed_response(
    request_message,
    requirements,
    tender_metadata=None,
):
    documents = request_message.get("documents", [])
    document_ids = [
        doc["documentId"]
        for doc in documents
        if doc.get("documentId")
    ]

    tender = request_message.get("tender") or {}
    metadata = tender_metadata or {}

    formatted_requirements = []

    for index, requirement in enumerate(requirements, start=1):
        source = requirement.get("source") or {}
        confidence = confidence_to_score(
            requirement.get("confidence")
        )

        source_confidence = confidence_to_score(
            source.get("confidence")
        )
        if source_confidence is None:
            source_confidence = confidence

        formatted_requirements.append({
            "sourceItemKey": f"REQ-{index:03d}",
            "content": requirement.get("text", ""),
            "mandatory": bool(requirement.get("mandatory", False)),
            "confidence": confidence,
            "sources": [
                {
                    "documentId": document_ids[0] if document_ids else None,
                    "pageNo": source.get("page", requirement.get("page")),
                    "section": source.get("section", requirement.get("section")),
                    "clause": source.get("clause", requirement.get("clause")),
                    "sheet": None,
                    "cellRange": None,
                    "excerpt": source.get(
                        "excerpt",
                        requirement.get("text", ""),
                    ),
                    "sourceRole": "PRIMARY",
                    "confidence": source_confidence,
                }
            ],
        })

    return {
        "schemaVersion": "1.0",
        "eventType": "RFP_ANALYSIS_COMPLETED",
        "eventId": str(uuid4()),
        "organizationId": request_message.get(
            "organizationId", "constell-dev"
        ),
        "workspaceId": request_message.get("workspaceId"),
        "jobId": request_message.get("jobId"),
        "analysisId": request_message.get("analysisId"),
        "correlationId": request_message.get("correlationId"),
        "attempt": request_message.get("attempt", 1),
        "status": "COMPLETED",
        "completedAt": (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "processedDocumentIds": document_ids,
        "providerName": "Ollama",
        "modelName": "qwen3:8b",
        "promptVersion": "rfp-extraction-v1",
        "result": {
            "title": tender.get("tenderName"),
            "customerName": tender.get("customer"),
            "tenderReference": tender.get("tenderReference"),
            "submissionDeadline": tender.get("submissionDeadline"),
            "summary": tender.get("summary"),
            "extractedMetadata": metadata,
            "requirements": formatted_requirements,
        },
    }
