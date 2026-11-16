import json

import pytest
import requests

from app.drafting.generator import (
    DRAFT_NUM_PREDICT,
    MAX_REQUIREMENTS,
    SECTION_SPECS,
    generate_draft_sections,
    merge_preserved_drafts,
    select_requirements_for_section,
)
from app.llm.ollama_client import MODEL_NAME
from app.export.local_export import snapshot_from_completed_response
from app.export.word import (
    NO_APPROVED_SECTIONS_TEXT,
    PENDING_DRAFTS_HEADING,
    render_word,
)
from app.messaging.consumer import outbound_completion_payload
from docx import Document
from io import BytesIO


REQUIREMENTS = [
    {
        "sourceItemKey": "REQ-001",
        "content": "Provide SSO login for the customer portal.",
        "mandatory": True,
        "sources": [{
            "filename": "tender.pdf",
            "pageNo": 4,
            "clause": "3.1",
            "excerpt": "The supplier shall provide SSO.",
        }],
        "evidenceMatch": {
            "status": "REVIEW_REQUIRED",
            "evidence": [{
                "text": "CBS delivered SSO for a prior portal.",
                "source": "karandaaz.pdf",
                "page": 2,
            }],
        },
    },
    {
        "sourceItemKey": "REQ-002",
        "content": "Provide 24/7 support.",
        "mandatory": True,
        "sources": [{
            "filename": "tender.pdf",
            "pageNo": 5,
            "excerpt": "Support must be 24/7.",
        }],
        "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
    },
]


def _payload(section_type):
    return json.dumps({
        "content": (
            f"{section_type} addresses REQ-001 using the cited SSO evidence. "
            "Information Required: 24/7 support has no CBS evidence."
        ),
        "sourceRequirementIds": ["REQ-001", "REQ-002", "REQ-999"],
        "missingInformation": ["No CBS evidence for 24/7 support."],
    })


@pytest.fixture
def ollama(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        for section_type, _title, _order in SECTION_SPECS:
            if section_type in prompt:
                return _payload(section_type)
        return _payload("UNKNOWN")

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    return calls


def test_all_six_section_types_are_generated(ollama):
    sections = generate_draft_sections(
        {"title": "Portal RFP", "customerName": "NTC"},
        REQUIREMENTS,
    )
    types = [item["sectionType"] for item in sections]
    assert types == [spec[0] for spec in SECTION_SPECS]
    assert [item["title"] for item in sections] == [spec[1] for spec in SECTION_SPECS]
    assert len(ollama) == 6
    assert all(item["kwargs"]["label"].startswith("draft_") for item in ollama)
    assert all(item["kwargs"]["num_predict"] == DRAFT_NUM_PREDICT for item in ollama)
    assert DRAFT_NUM_PREDICT == 1024


def test_draft_status_and_traceability_by_default(ollama):
    section = generate_draft_sections({"title": "Portal RFP"}, REQUIREMENTS)[0]
    assert section["status"] == "DRAFT"
    assert section["approvalStatus"] == "DRAFT"
    assert section["model"] == MODEL_NAME
    assert section["sourceRequirementIds"] == ["REQ-001", "REQ-002"]
    assert "REQ-999" not in section["sourceRequirementIds"]
    filenames = {item.get("filename") for item in section["sources"]}
    assert "tender.pdf" in filenames
    assert "karandaaz.pdf" in filenames
    pages = {item.get("page") for item in section["sources"]}
    assert 4 in pages
    assert 2 in pages
    assert any(item.get("clause") == "3.1" for item in section["sources"])
    assert section["generatedAt"].endswith("Z")


def test_missing_evidence_is_marked_not_fabricated(monkeypatch):
    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "content": (
                "CBS holds ISO 27001 and delivered work for Acme Bank in 2019 "
                "with 40 certified engineers."
            ),
            "sourceRequirementIds": ["REQ-001"],
            "missingInformation": [],
        })

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    bare = [{
        "sourceItemKey": "REQ-001",
        "content": "Provide SSO.",
        "mandatory": True,
        "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
    }]
    sections = {
        item["sectionType"]: item
        for item in generate_draft_sections({"title": "T"}, bare)
    }
    experience = sections["RELEVANT_EXPERIENCE"]
    assert any("No verified CBS" in gap for gap in experience["missingInformation"])
    assert "Information Required:" in experience["content"]
    capability = sections["CAPABILITY_RESPONSE"]
    assert "Information Required:" in capability["content"]


def test_malformed_and_timeout_leave_other_sections(monkeypatch):
    def fake_generate(prompt, **kwargs):
        if "CAPABILITY_RESPONSE" in prompt:
            raise requests.Timeout("timed out")
        if "TECHNICAL_APPROACH" in prompt:
            return "not json at all"
        return _payload("UNDERSTANDING_OF_REQUIREMENTS")

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    sections = generate_draft_sections({"title": "T"}, REQUIREMENTS)
    types = {item["sectionType"] for item in sections}
    assert "CAPABILITY_RESPONSE" not in types
    assert "TECHNICAL_APPROACH" not in types
    assert "UNDERSTANDING_OF_REQUIREMENTS" in types
    assert "IMPLEMENTATION_METHODOLOGY" in types


def test_approved_section_is_not_overwritten_and_metadata_is_kept(ollama):
    generated = generate_draft_sections({"title": "T"}, REQUIREMENTS)
    existing = [{
        "sectionId": "sec-human-1",
        "sectionType": "CAPABILITY_RESPONSE",
        "title": "Executive Summary",
        "content": "Human-approved executive summary.",
        "approvalStatus": "APPROVED",
        "approvedBy": "reviewer-1",
        "approvedAt": "2099-01-01T00:00:00Z",
        "order": 1,
    }]
    merged = merge_preserved_drafts(generated, existing)
    capability = [item for item in merged if item.get("sectionType") == "CAPABILITY_RESPONSE"]
    assert len(capability) == 1
    assert capability[0]["content"] == "Human-approved executive summary."
    assert capability[0]["approvedBy"] == "reviewer-1"
    assert capability[0]["approvalStatus"] == "APPROVED"
    types = [item["sectionType"] for item in merged if item.get("sectionType") != "CAPABILITY_RESPONSE"]
    assert "UNDERSTANDING_OF_REQUIREMENTS" in types


def test_snapshot_copies_drafts_without_publishing():
    response = {
        "organizationId": "org",
        "workspaceId": "ws",
        "jobId": "job",
        "analysisId": "a",
        "documents": [],
        "draftSections": [{
            "sectionId": "DRAFT-TECHNICAL_APPROACH",
            "sectionType": "TECHNICAL_APPROACH",
            "title": "Proposed Technical Approach",
            "content": "Draft technical approach.",
            "status": "DRAFT",
            "approvalStatus": "DRAFT",
            "sourceRequirementIds": ["REQ-001"],
            "sources": [{"filename": "tender.pdf", "page": 4}],
            "missingInformation": [],
            "order": 3,
        }],
        "result": {
            "title": "T",
            "customerName": "C",
            "tenderReference": "R",
            "submissionDeadline": None,
            "requirements": [{
                "sourceItemKey": "REQ-001",
                "content": "Provide SSO.",
                "mandatory": True,
                "sources": [],
                "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
            }],
        },
    }
    snapshot = snapshot_from_completed_response(response)
    assert snapshot["draftSections"][0]["status"] == "DRAFT"
    outbound = outbound_completion_payload(response)
    assert "draftSections" not in outbound
    assert "draftSections" in response


def test_word_includes_only_approved_sections_and_sources():
    snapshot = {
        "tender": {"title": "Portal RFP"},
        "requirements": [{
            "requirementId": "REQ-001",
            "content": "Provide SSO.",
            "mandatory": True,
        }],
        "draftSections": [
            {
                "title": "Understanding of Requirements",
                "content": "Approved understanding of SSO.",
                "approvalStatus": "APPROVED",
                "sourceRequirementIds": ["REQ-001"],
                "sources": [{"filename": "tender.pdf", "page": 4, "clause": "3.1"}],
                "order": 2,
            },
            {
                "title": "Proposed Technical Approach",
                "content": "Secret draft approach.",
                "approvalStatus": "DRAFT",
            },
            {
                "title": "Compliance Response",
                "content": "Rejected compliance narrative.",
                "approvalStatus": "REJECTED",
            },
        ],
        "actions": [],
    }
    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Approved understanding of SSO." in text
    assert "Requirements: REQ-001" in text
    assert "tender.pdf" in text
    assert "p.4" in text
    pending_at = text.index(PENDING_DRAFTS_HEADING)
    approved_at = text.index("Approved understanding of SSO.")
    assert approved_at < pending_at
    assert "Secret draft approach." not in text[:pending_at]
    assert "Secret draft approach." in text[pending_at:]
    assert "Rejected compliance narrative." in text[pending_at:]
    assert NO_APPROVED_SECTIONS_TEXT not in text


def _mixed_requirements():
    items = []
    for index in range(20):
        items.append({
            "sourceItemKey": f"REQ-OPT-{index:02d}",
            "content": f"Optional branding colour palette {index} for printed stationery.",
            "mandatory": False,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        })
    items.extend([
        {
            "sourceItemKey": "REQ-M-TECH",
            "content": "The system shall expose a REST API for portal integration.",
            "mandatory": True,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-M-METHOD",
            "content": "The supplier shall provide a phased implementation plan and training.",
            "mandatory": True,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-M-EXP",
            "content": "Demonstrate similar past experience and certifications.",
            "mandatory": True,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-M-COMPLY",
            "content": "The supplier shall comply with ISO 27001.",
            "mandatory": True,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-OPT-API",
            "content": "Document the application REST interface with OpenAPI schemas.",
            "mandatory": False,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-OPT-PORTFOLIO",
            "content": "Include a previous client portfolio of similar past experience.",
            "mandatory": False,
            "evidenceMatch": {"status": "NOT_FOUND", "evidence": []},
        },
        {
            "sourceItemKey": "REQ-M-SSO",
            "content": "Provide SSO login for the customer portal.",
            "mandatory": True,
            "evidenceMatch": {
                "status": "REVIEW_REQUIRED",
                "evidence": [{
                    "text": "CBS delivered SSO for a prior portal.",
                    "source": "karandaaz.pdf",
                    "page": 2,
                }],
            },
        },
    ])
    return items


def _ids_from_prompt(prompt):
    marker = "REQUIREMENTS AND FILTERED CBS EVIDENCE (JSON):"
    start = prompt.index(marker) + len(marker)
    compact = json.loads(prompt[start:].split("RULES:", 1)[0])
    return [item["id"] for item in compact]


def test_context_selection_differs_by_section():
    mixed = _mixed_requirements()
    technical = {
        _requirement_id_safe(item)
        for item in select_requirements_for_section("TECHNICAL_APPROACH", mixed)
    }
    experience = {
        _requirement_id_safe(item)
        for item in select_requirements_for_section("RELEVANT_EXPERIENCE", mixed)
    }
    understanding = {
        _requirement_id_safe(item)
        for item in select_requirements_for_section("UNDERSTANDING_OF_REQUIREMENTS", mixed)
    }
    assert "REQ-M-TECH" in technical
    assert "REQ-M-SSO" in technical
    assert "REQ-OPT-API" in technical
    assert "REQ-OPT-19" not in technical
    assert "REQ-OPT-PORTFOLIO" not in technical
    assert "REQ-M-EXP" in experience
    assert "REQ-OPT-PORTFOLIO" in experience
    assert "REQ-OPT-API" not in experience
    assert "REQ-OPT-19" not in experience
    assert technical != experience
    assert understanding != technical


def test_mandatory_requirements_relevant_to_section_are_preserved():
    mixed = _mixed_requirements()
    mandatory_ids = {
        "REQ-M-TECH", "REQ-M-METHOD", "REQ-M-EXP", "REQ-M-COMPLY", "REQ-M-SSO",
    }
    for section_type in ("UNDERSTANDING_OF_REQUIREMENTS", "COMPLIANCE_RESPONSE"):
        selected = {
            _requirement_id_safe(item)
            for item in select_requirements_for_section(section_type, mixed)
        }
        assert mandatory_ids <= selected
    technical = {
        _requirement_id_safe(item)
        for item in select_requirements_for_section("TECHNICAL_APPROACH", mixed)
    }
    assert "REQ-M-TECH" in technical
    assert "REQ-M-SSO" in technical


def test_section_context_is_capped_when_many_mandatories_exist():
    bulky = []
    for index in range(40):
        bulky.append({
            "sourceItemKey": f"REQ-M-{index:02d}",
            "content": f"The supplier shall provide numbered service item {index}.",
            "mandatory": True,
            "evidenceMatch": {
                "status": "REVIEW_REQUIRED" if index == 7 else "NOT_FOUND",
                "evidence": [] if index != 7 else [{
                    "text": "CBS delivered a numbered service item.",
                    "source": "karandaaz.pdf",
                    "page": 2,
                }],
            },
        })
    selected = select_requirements_for_section("UNDERSTANDING_OF_REQUIREMENTS", bulky)
    ids = [_requirement_id_safe(item) for item in selected]
    assert len(selected) <= MAX_REQUIREMENTS
    assert "REQ-M-07" in ids


def _requirement_id_safe(item):
    return item.get("sourceItemKey") or item.get("requirementId")


def test_valid_json_becomes_draft_section(ollama):
    sections = generate_draft_sections({"title": "Portal RFP"}, REQUIREMENTS)
    assert sections
    assert all(item["status"] == "DRAFT" for item in sections)
    assert all(item["content"] for item in sections)


def test_truncated_json_is_not_a_successful_draft(monkeypatch):
    def fake_generate(prompt, **kwargs):
        if "CAPABILITY_RESPONSE" in prompt:
            return '{"content": "partial executive summary", "sourceRequirementIds": ["REQ-001"'
        return _payload("UNDERSTANDING_OF_REQUIREMENTS")

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    sections = generate_draft_sections({"title": "T"}, REQUIREMENTS)
    types = {item["sectionType"] for item in sections}
    assert "CAPABILITY_RESPONSE" not in types
    assert "UNDERSTANDING_OF_REQUIREMENTS" in types


def test_partial_section_failures_keep_other_drafts(monkeypatch):
    def fake_generate(prompt, **kwargs):
        if "CAPABILITY_RESPONSE" in prompt:
            raise requests.Timeout("timed out")
        if "TECHNICAL_APPROACH" in prompt:
            return '{"content": "broken", "sourceRequirementIds": ["REQ-001"], "missingInformation": ["gap"'
        if "IMPLEMENTATION_METHODOLOGY" in prompt:
            return json.dumps({
                "content": "",
                "sourceRequirementIds": ["REQ-001"],
                "missingInformation": [],
            })
        return _payload("UNDERSTANDING_OF_REQUIREMENTS")

    monkeypatch.setattr("app.drafting.generator.generate_response", fake_generate)
    sections = generate_draft_sections({"title": "T"}, REQUIREMENTS)
    types = {item["sectionType"] for item in sections}
    assert "CAPABILITY_RESPONSE" not in types
    assert "TECHNICAL_APPROACH" not in types
    assert "IMPLEMENTATION_METHODOLOGY" not in types
    assert "UNDERSTANDING_OF_REQUIREMENTS" in types
    assert "RELEVANT_EXPERIENCE" in types
    assert all(item["status"] == "DRAFT" for item in sections)


def test_draft_section_logs_start_and_finish(ollama, capsys):
    generate_draft_sections({"title": "Portal RFP"}, REQUIREMENTS)
    output = capsys.readouterr().out
    assert "DRAFT_SECTION started type=CAPABILITY_RESPONSE model=" in output
    assert "DRAFT_SECTION finished type=CAPABILITY_RESPONSE success=true" in output
    assert "duration_ms=" in output
    assert "response_chars=" in output


def test_section_prompts_use_selected_context(ollama):
    generate_draft_sections({"title": "Portal RFP"}, _mixed_requirements())
    by_type = {}
    for call in ollama:
        for section_type, _title, _order in SECTION_SPECS:
            if f"SECTION TYPE: {section_type}" in call["prompt"]:
                by_type[section_type] = _ids_from_prompt(call["prompt"])
    assert "REQ-M-TECH" in by_type["TECHNICAL_APPROACH"]
    assert "REQ-OPT-API" in by_type["TECHNICAL_APPROACH"]
    assert "REQ-M-EXP" in by_type["RELEVANT_EXPERIENCE"]
    assert "REQ-OPT-PORTFOLIO" in by_type["RELEVANT_EXPERIENCE"]
    assert "REQ-OPT-19" not in by_type["TECHNICAL_APPROACH"]
    assert "REQ-OPT-API" not in by_type["RELEVANT_EXPERIENCE"]
    assert set(by_type["UNDERSTANDING_OF_REQUIREMENTS"]) >= {
        "REQ-M-TECH", "REQ-M-METHOD", "REQ-M-EXP", "REQ-M-COMPLY", "REQ-M-SSO",
    }
    assert by_type["TECHNICAL_APPROACH"] != by_type["RELEVANT_EXPERIENCE"]


def test_word_pending_message_when_no_section_is_approved():
    snapshot = {
        "tender": {"title": "Portal RFP"},
        "requirements": [{
            "requirementId": "REQ-001",
            "content": "Provide SSO.",
            "mandatory": True,
        }],
        "draftSections": [
            {
                "title": "Executive Summary",
                "content": "Unapproved capability draft.",
                "approvalStatus": "DRAFT",
            }
        ],
        "actions": [],
    }
    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert NO_APPROVED_SECTIONS_TEXT in text
    assert PENDING_DRAFTS_HEADING in text
    pending_at = text.index(PENDING_DRAFTS_HEADING)
    assert "Unapproved capability draft." not in text[:pending_at]
    assert "Unapproved capability draft." in text[pending_at:]
