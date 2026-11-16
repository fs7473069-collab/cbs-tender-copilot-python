"""Focused end-to-end validation of AI bid-response drafting.

Uses a synthetic tender and mocked Qwen3:8B. Does not start Ollama, RabbitMQ,
or a full analysis run.
"""

import json
from io import BytesIO

from docx import Document

from app.drafting.generator import SECTION_SPECS, generate_draft_sections
from app.export.local_export import snapshot_from_completed_response
from app.export.word import NO_APPROVED_SECTIONS_TEXT, PENDING_DRAFTS_HEADING, render_word
from app.llm.ollama_client import MODEL_NAME
from app.messaging.consumer import outbound_completion_payload
from app.response.response_builder import build_completed_response


DRAFT_FIELDS = {
    "sectionId",
    "sectionType",
    "title",
    "content",
    "status",
    "approvalStatus",
    "sourceRequirementIds",
    "sources",
    "missingInformation",
    "generatedAt",
    "model",
    "order",
}

SECTION_BODIES = {
    "CAPABILITY_RESPONSE": (
        "CBS can support NTC's customer portal using verified SSO delivery "
        "cited for REQ-001. Information Required: 24/7 operations coverage "
        "has no CBS evidence (REQ-003)."
    ),
    "UNDERSTANDING_OF_REQUIREMENTS": (
        "NTC requires SSO for the citizen portal (REQ-001), an optional "
        "audit trail (REQ-002), and 24/7 support (REQ-003)."
    ),
    "TECHNICAL_APPROACH": (
        "The proposed approach reuses the SSO pattern evidenced in the "
        "Karandaaz portal work for REQ-001 and identifies 24/7 support as a gap."
    ),
    "IMPLEMENTATION_METHODOLOGY": (
        "Implementation would sequence SSO enablement first, then optional "
        "audit logging. 24/7 support remains Information Required."
    ),
    "RELEVANT_EXPERIENCE": (
        "CBS holds ISO 27001, delivered a national tax system for Acme Bank "
        "in 2019, and staffs 40 certified engineers. This is not in the "
        "knowledge-base evidence."
    ),
    "COMPLIANCE_RESPONSE": (
        "REQ-001 has candidate CBS evidence requiring reviewer verification. "
        "REQ-003 has no acceptable CBS evidence."
    ),
}


def _extracted_requirements():
    return [
        {
            "text": "The supplier shall provide single sign-on (SSO) for the citizen portal.",
            "mandatory": True,
            "confidence": "High",
            "source": {
                "page": 12,
                "section": "3 Functional Requirements",
                "clause": "3.1.1",
                "excerpt": "The supplier shall provide single sign-on (SSO) for the citizen portal.",
                "filename": "NTC-Citizen-Portal-RFP.pdf",
            },
            "evidence_match": {
                "status": "REVIEW_REQUIRED",
                "note": "Retrieved evidence requires human verification.",
                "evidence": [{
                    "text": "CBS implemented SSO and role-based access for the Karandaaz portal.",
                    "source": "RFP Response - Karandaaz final version copy.pdf",
                    "page": 8,
                    "similarity": 0.71,
                }],
            },
        },
        {
            "text": "The solution should retain an audit trail of administrator actions.",
            "mandatory": False,
            "confidence": "Medium",
            "source": {
                "page": 14,
                "section": "3 Functional Requirements",
                "clause": "3.2.4",
                "excerpt": "The solution should retain an audit trail of administrator actions.",
            },
            "evidence_match": {
                "status": "NOT_FOUND",
                "note": "No acceptable CBS evidence was found.",
                "evidence": [],
            },
        },
        {
            "text": "The supplier shall provide 24/7 operational support for the portal.",
            "mandatory": True,
            "confidence": "High",
            "source": {
                "page": 19,
                "section": "5 Service Levels",
                "clause": "5.1",
                "excerpt": "The supplier shall provide 24/7 operational support for the portal.",
            },
            "evidence_match": {
                "status": "NOT_FOUND",
                "note": "No acceptable CBS evidence was found.",
                "evidence": [],
            },
        },
    ]


def _request():
    return {
        "organizationId": "org-ntc",
        "workspaceId": "ws-bids",
        "jobId": "job-portal-1",
        "analysisId": "analysis-portal-1",
        "correlationId": "corr-portal-1",
        "documents": [{
            "documentId": "doc-ntc-rfp",
            "filename": "NTC-Citizen-Portal-RFP.pdf",
            "bucket": "tenders",
            "key": "ntc/rfp.pdf",
        }],
        "tender": {
            "tenderName": "NTC Citizen Portal Modernisation",
            "customer": "National Telecommunication Corporation",
            "tenderReference": "NTC-PORTAL-2026",
            "submissionDeadline": "2026-12-15T12:00:00Z",
        },
    }


def _mock_qwen(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        assert kwargs.get("label", "").startswith("draft_")
        section_type = next(
            name for name, _title, _order in SECTION_SPECS if name in prompt
        )
        return json.dumps({
            "content": SECTION_BODIES[section_type],
            "sourceRequirementIds": ["REQ-001", "REQ-002", "REQ-003", "REQ-999"],
            "missingInformation": ["24/7 support is not evidenced in the CBS knowledge base."],
        })

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    return calls


def test_drafting_end_to_end_snapshot_outbound_and_word(monkeypatch):
    calls = _mock_qwen(monkeypatch)
    response = build_completed_response(_request(), _extracted_requirements())
    requirements = response["result"]["requirements"]
    assert [item["sourceItemKey"] for item in requirements] == [
        "REQ-001", "REQ-002", "REQ-003",
    ]
    assert requirements[0]["mandatory"] is True
    assert requirements[1]["mandatory"] is False
    assert requirements[2]["evidenceMatch"]["status"] == "NOT_FOUND"
    assert requirements[0]["evidenceMatch"]["evidence"][0]["source"] == (
        "RFP Response - Karandaaz final version copy.pdf"
    )

    generated = generate_draft_sections(
        response["result"],
        requirements,
        _request()["documents"],
    )
    assert len(calls) == 6
    assert [item["sectionType"] for item in generated] == [spec[0] for spec in SECTION_SPECS]

    for section in generated:
        assert DRAFT_FIELDS <= set(section)
        assert section["status"] == "DRAFT"
        assert section["approvalStatus"] == "DRAFT"
        assert section["model"] == MODEL_NAME
        assert "REQ-999" not in section["sourceRequirementIds"]
        assert "REQ-001" in section["sourceRequirementIds"]
        filenames = {item.get("filename") for item in section["sources"]}
        assert "RFP Response - Karandaaz final version copy.pdf" in filenames
        assert "NTC-Citizen-Portal-RFP.pdf" in filenames
        pages = {item.get("page") for item in section["sources"]}
        assert 12 in pages
        assert 8 in pages
        assert any(item.get("clause") == "3.1.1" for item in section["sources"])

    experience = next(
        item for item in generated if item["sectionType"] == "RELEVANT_EXPERIENCE"
    )
    joined_gaps = " ".join(experience["missingInformation"])
    assert "REQ-003" in joined_gaps
    assert "REQ-002" in joined_gaps
    assert "Information Required:" in experience["content"]

    response["draftSections"] = generated
    snapshot = snapshot_from_completed_response(response)
    assert len(snapshot["draftSections"]) == 6
    assert all(item["approvalStatus"] == "DRAFT" for item in snapshot["draftSections"])

    outbound = outbound_completion_payload(response)
    assert "draftSections" not in outbound
    assert "draftSections" not in outbound.get("result", {})
    assert "draftSections" in response
    assert "requirements" not in outbound["result"]

    mixed = []
    for section in snapshot["draftSections"]:
        copied = dict(section)
        if copied["sectionType"] == "UNDERSTANDING_OF_REQUIREMENTS":
            copied["approvalStatus"] = "APPROVED"
            copied["approvedBy"] = "reviewer-1"
        elif copied["sectionType"] == "COMPLIANCE_RESPONSE":
            copied["approvalStatus"] = "REJECTED"
            copied["rejectedBy"] = "reviewer-1"
        mixed.append(copied)
    snapshot["draftSections"] = mixed

    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "NTC requires SSO" in text
    assert "Requirements: REQ-001" in text
    assert "NTC-Citizen-Portal-RFP.pdf" in text
    assert "RFP Response - Karandaaz final version copy.pdf" in text
    assert "p.12" in text or "p.8" in text
    assert "clause 3.1.1" in text
    pending_at = text.index(PENDING_DRAFTS_HEADING)
    assert "NTC requires SSO" in text[:pending_at]
    assert "Acme Bank" not in text[:pending_at]
    assert "The proposed approach reuses" not in text[:pending_at]
    assert "REQ-001 has candidate CBS evidence" not in text[:pending_at]
    assert "Acme Bank" in text[pending_at:]
    assert "The proposed approach reuses" in text[pending_at:]
    assert NO_APPROVED_SECTIONS_TEXT not in text
