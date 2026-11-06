
from datetime import datetime, timezone
from uuid import uuid4

from app.llm import ollama_client

PROVIDER_NAME = "Ollama"
# Inbound request ``tender`` keys for each published result field.
REQUEST_TENDER_FIELDS = {
    "title": "tenderName",
    "customerName": "customer",
    "tenderReference": "tenderReference",
    "submissionDeadline": "submissionDeadline",
    "summary": "summary",
}


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



def format_evidence_match(evidence_match):
    """Format RAG evidence separately from the tender source."""
    if not isinstance(evidence_match, dict):
        return {
            "status": "NOT_CHECKED",
            "evidence": [],
            "note": "No evidence-matching result was provided.",
        }

    evidence = []
    for item in evidence_match.get("evidence", []):
        if not isinstance(item, dict):
            continue

        evidence.append({
            "text": item.get("text"),
            "source": item.get("source"),
            "page": item.get("page"),
            "similarity": item.get("similarity", item.get("score")),
        })

    return {
        "status": evidence_match.get("status", "REVIEW_REQUIRED"),
        "evidence": evidence,
        "note": evidence_match.get(
            "note",
            "Retrieved evidence requires human verification.",
        ),
    }

def _tender_result_fields(tender, tender_summary):
    """Request-supplied values win; document-derived values fill the gaps."""
    summary = tender_summary if isinstance(tender_summary, dict) else {}
    fields = {}
    for field, request_key in REQUEST_TENDER_FIELDS.items():
        value = tender.get(request_key)
        if value is None or (isinstance(value, str) and not value.strip()):
            derived = summary.get(field)
            value = derived if isinstance(derived, str) and derived.strip() else None
        fields[field] = value
    return fields


def build_completed_response(
    request_message,
    requirements,
    tender_metadata=None,
    tender_summary=None,
):
    documents = request_message.get("documents", [])
    document_ids = [
        doc["documentId"]
        for doc in documents
        if doc.get("documentId")
    ]

    tender = request_message.get("tender") or {}
    metadata = tender_metadata or {}
    tender_fields = _tender_result_fields(tender, tender_summary)

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
            "evidenceMatch": format_evidence_match(
                requirement.get("evidence_match")
            ),
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
        "providerName": PROVIDER_NAME,
        "modelName": ollama_client.MODEL_NAME,
        "promptVersion": ollama_client.EXTRACTION_PROMPT_VERSION,
        "result": {
            "title": tender_fields["title"],
            "customerName": tender_fields["customerName"],
            "tenderReference": tender_fields["tenderReference"],
            "submissionDeadline": tender_fields["submissionDeadline"],
            "summary": tender_fields["summary"],
            "extractedMetadata": metadata,
            "requirements": formatted_requirements,
        },
    }
