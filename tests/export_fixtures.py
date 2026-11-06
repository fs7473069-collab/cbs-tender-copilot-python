"""SYNTHETIC TEST DATA for export tests. Not real tender data."""

import copy

_SNAPSHOT = {
    "tender": {
        "title": "TEST-ONLY Synthetic Tender",
        "customerName": "Test Customer (synthetic)",
        "tenderReference": "TEST-REF-001",
        "submissionDeadline": "2099-01-31T12:00:00Z",
    },
    "requirements": [
        {
            "requirementId": "req-test-1",
            "sourceItemKey": "REQ-001",
            "content": "Synthetic requirement one.",
            "mandatory": True,
            "category": "Test category",
            "confidence": 0.9,
            "sources": [
                {
                    "documentId": "doc-test-1",
                    "filename": "synthetic.pdf",
                    "pageNo": 3,
                    "section": "2.1",
                    "clause": "2.1.4",
                    "sheet": None,
                    "cellRange": None,
                    "excerpt": "Synthetic excerpt one.",
                    "sourceRole": "PRIMARY",
                    "confidence": 0.9,
                },
                {
                    "documentId": "doc-test-2",
                    "pageNo": None,
                    "sheet": "Pricing",
                    "cellRange": "B2:C4",
                    "excerpt": "Synthetic excerpt two.",
                },
            ],
            "evidenceMatch": {
                "status": "REVIEW_REQUIRED",
                "note": "Synthetic evidence note.",
                "evidence": [
                    {"text": "kb text", "source": "kb-test.pdf", "page": 7,
                     "similarity": 0.81},
                ],
            },
            "aiRecommendation": None,
            "decision": None,
        },
        {
            "requirementId": "req-test-2",
            "sourceItemKey": "REQ-002",
            "content": "Synthetic requirement two.",
            "mandatory": True,
            "evidenceMatch": {"status": "NOT_FOUND", "note": "n", "evidence": []},
            "aiRecommendation": {"recommendation": "LIKELY_COMPLIANT",
                                 "rationale": "synthetic rationale"},
            "decision": {"status": "COMPLIANT", "decidedBy": "user-test",
                         "decidedAt": "2099-01-01T00:00:00Z",
                         "comment": "synthetic comment"},
        },
        {
            "requirementId": "req-test-3",
            "sourceItemKey": "REQ-003",
            "content": "Synthetic optional requirement.",
            "mandatory": False,
            "evidenceMatch": {"status": "NOT_CHECKED", "note": None, "evidence": []},
        },
    ],
    "actions": [
        {"actionId": "act-test-1", "requirementId": "req-test-1",
         "title": "Synthetic action", "description": "desc", "owner": "owner-test",
         "dueDate": "2099-01-15", "status": "OPEN"},
    ],
    "draftSections": [
        {"sectionId": "s-b", "title": "Second", "content": "B para",
         "approvalStatus": "APPROVED", "order": 2},
        {"sectionId": "s-a", "title": "First", "content": "A one\n\nA two",
         "approvalStatus": "approved", "order": 1},
        {"sectionId": "s-p", "title": "Pending", "content": "PENDING TEXT",
         "approvalStatus": "PENDING"},
        {"sectionId": "s-d", "title": "Draft", "content": "DRAFT TEXT",
         "approvalStatus": "DRAFT"},
        {"sectionId": "s-r", "title": "Rejected", "content": "REJECTED TEXT",
         "approvalStatus": "REJECTED"},
        {"sectionId": "s-m", "title": "Missing", "content": "MISSING TEXT"},
        {"sectionId": "s-n", "title": "Null", "content": "NULL TEXT",
         "approvalStatus": None},
        {"sectionId": "s-u", "title": "Unknown", "content": "UNKNOWN TEXT",
         "approvalStatus": "APPROVED_MAYBE"},
        {"sectionId": "s-c", "title": "Unordered", "content": "C para",
         "approvalStatus": " Approved "},
    ],
}


def make_snapshot():
    return copy.deepcopy(_SNAPSHOT)


def make_export_request(**overrides):
    request = {
        "schemaVersion": "1.0",
        "eventType": "RFP_EXPORT_REQUESTED",
        "eventId": "evt-test-1",
        "exportId": "exp-test-1",
        "organizationId": "org-test",
        "workspaceId": "ws-test",
        "tenderId": "tender-test",
        "correlationId": "corr-test",
        "attempt": 1,
        "output": {"bucket": "test-bucket-not-real"},
        "snapshot": make_snapshot(),
    }
    request.update(overrides)
    return request
