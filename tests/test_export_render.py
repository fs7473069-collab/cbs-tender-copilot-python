from io import BytesIO

import pytest
from docx import Document
from openpyxl import load_workbook

from app.export import (
    DECISION_VALUES,
    SnapshotError,
    count_mandatory_without_decision,
    render_excel,
    render_word,
)
from app.export.excel import (
    ACTIONS_HEADERS,
    COMPLIANCE_HEADERS,
    REQUIREMENTS_HEADERS,
)
from app.export.word import NO_APPROVED_SECTIONS_TEXT
from tests.export_fixtures import make_snapshot


def _workbook(snapshot):
    return load_workbook(BytesIO(render_excel(snapshot)))


def _rows(sheet):
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def _by_header(sheet, row_index):
    rows = _rows(sheet)
    return dict(zip(rows[0], rows[row_index]))


def _doc_text(snapshot):
    document = Document(BytesIO(render_word(snapshot)))
    return [p.text for p in document.paragraphs]


# Excel -------------------------------------------------------------------------

def test_excel_has_exactly_three_sheets_in_order_with_headers():
    workbook = _workbook(make_snapshot())
    assert workbook.sheetnames == ["Requirements", "Compliance Matrix", "Actions"]
    assert _rows(workbook["Requirements"])[0] == REQUIREMENTS_HEADERS
    assert _rows(workbook["Compliance Matrix"])[0] == COMPLIANCE_HEADERS
    assert _rows(workbook["Actions"])[0] == ACTIONS_HEADERS


def test_requirements_sheet_one_row_per_requirement_with_sources():
    sheet = _workbook(make_snapshot())["Requirements"]
    assert sheet.max_row == 4
    row = _by_header(sheet, 1)
    assert row["Requirement ID"] == "req-test-1"
    assert row["Source Item Key"] == "REQ-001"
    assert row["Requirement"] == "Synthetic requirement one."
    assert row["Mandatory"] == "Yes"
    assert row["Category"] == "Test category"
    assert row["Confidence"] == "0.9"
    assert row["Source Count"] == "2"
    assert row["Source Document"] == "[1] synthetic.pdf (doc-test-1)\n[2] doc-test-2"
    assert row["Source Page"] == "[1] 3\n[2] "
    assert row["Source Clause"] == "[1] 2.1.4\n[2] "
    assert row["Source Sheet"] == "[1] \n[2] Pricing"
    assert row["Source Cell Range"] == "[1] \n[2] B2:C4"
    assert "Synthetic excerpt two." in row["Source Excerpt"]
    # No sources -> blank source columns, nothing invented.
    third = _by_header(sheet, 3)
    assert third["Source Count"] == "0"
    assert third["Source Document"] is None
    assert third["Category"] is None


def test_compliance_matrix_keeps_ai_evidence_recommendation_and_decision_separate():
    sheet = _workbook(make_snapshot())["Compliance Matrix"]
    first = _by_header(sheet, 1)
    assert first["AI Evidence Status"] == "REVIEW_REQUIRED"
    assert first["AI Evidence Note"] == "Synthetic evidence note."
    assert first["Evidence References"] == "kb-test.pdf, p.7, similarity=0.81"
    assert first["AI Recommendation"] == "Not provided"
    assert first["Final Decision"] == "No decision"
    assert first["Decided By"] is None

    second = _by_header(sheet, 2)
    assert second["AI Evidence Status"] == "NOT_FOUND"
    assert second["AI Recommendation"] == "LIKELY_COMPLIANT: synthetic rationale"
    assert second["Final Decision"] == "COMPLIANT"
    assert second["Decided By"] == "user-test"
    assert second["Decided At"] == "2099-01-01T00:00:00Z"
    assert second["Decision Comment"] == "synthetic comment"


@pytest.mark.parametrize("evidence_status", ["REVIEW_REQUIRED", "NOT_FOUND", "COMPLIANT"])
def test_decision_is_never_derived_from_evidence_status(evidence_status):
    snapshot = make_snapshot()
    snapshot["requirements"] = [{
        "requirementId": "r", "content": "c", "mandatory": True,
        "evidenceMatch": {"status": evidence_status, "evidence": []},
        "aiRecommendation": "COMPLIANT",
    }]
    row = _by_header(_workbook(snapshot)["Compliance Matrix"], 1)
    assert row["Final Decision"] == "No decision"
    assert count_mandatory_without_decision(snapshot) == 1


def test_actions_sheet_rows_from_snapshot_only():
    sheet = _workbook(make_snapshot())["Actions"]
    assert sheet.max_row == 2
    row = _by_header(sheet, 1)
    assert row == {
        "Action ID": "act-test-1", "Requirement ID": "req-test-1",
        "Title": "Synthetic action", "Description": "desc",
        "Owner": "owner-test", "Due Date": "2099-01-15", "Status": "OPEN",
    }


@pytest.mark.parametrize("actions", [None, []])
def test_empty_actions_sheet_still_has_headers(actions):
    snapshot = make_snapshot()
    snapshot["actions"] = actions
    sheet = _workbook(snapshot)["Actions"]
    assert _rows(sheet) == [ACTIONS_HEADERS]


@pytest.mark.parametrize("payload", [
    "=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd",
])
def test_formula_injection_is_neutralised(payload):
    snapshot = make_snapshot()
    snapshot["requirements"][0]["content"] = payload
    snapshot["actions"][0]["title"] = payload
    workbook = _workbook(snapshot)
    for sheet_name, header in (
        ("Requirements", "Requirement"),
        ("Compliance Matrix", "Requirement"),
        ("Actions", "Title"),
    ):
        sheet = workbook[sheet_name]
        column = _rows(sheet)[0].index(header) + 1
        cell = sheet.cell(row=2, column=column)
        assert cell.data_type == "s"
        assert cell.value == "'" + payload


def test_illegal_control_characters_are_stripped():
    snapshot = make_snapshot()
    snapshot["requirements"][0]["content"] = "abc\x00\x07def"
    snapshot["draftSections"][0]["content"] = "x\x01y"
    row = _by_header(_workbook(snapshot)["Requirements"], 1)
    assert row["Requirement"] == "abcdef"
    assert "xy" in _doc_text(snapshot)


def test_invalid_snapshot_raises():
    with pytest.raises(SnapshotError):
        render_excel({"requirements": "nope"})
    with pytest.raises(SnapshotError):
        render_excel({"requirements": [{"requirementId": "r", "content": ""}]})
    with pytest.raises(SnapshotError):
        render_excel({"requirements": [{"content": "no id"}]})
    with pytest.raises(SnapshotError):
        render_word(None)


# Word --------------------------------------------------------------------------

def test_word_includes_tender_info_and_only_approved_sections_in_order():
    text = _doc_text(make_snapshot())
    assert text[0] == "TEST-ONLY Synthetic Tender"
    assert "Customer: Test Customer (synthetic)" in text
    assert "Reference: TEST-REF-001" in text
    assert "Submission deadline: 2099-01-31T12:00:00Z" in text
    headings = text[text.index("Approved Draft Sections") + 1:]
    assert headings == ["First", "A one", "A two", "Second", "B para", "Unordered", "C para"]
    joined = "\n".join(text)
    for excluded in ("PENDING TEXT", "DRAFT TEXT", "REJECTED TEXT",
                     "MISSING TEXT", "NULL TEXT", "UNKNOWN TEXT"):
        assert excluded not in joined
    assert NO_APPROVED_SECTIONS_TEXT not in joined


@pytest.mark.parametrize("sections", [None, [], [
    {"title": "P", "content": "secret draft", "approvalStatus": "PENDING"},
    {"title": "N", "content": "secret draft", "approvalStatus": None},
]])
def test_zero_approved_sections_states_so_without_fabricating(sections):
    snapshot = make_snapshot()
    snapshot["draftSections"] = sections
    text = _doc_text(snapshot)
    assert NO_APPROVED_SECTIONS_TEXT in text
    assert "secret draft" not in "\n".join(text)
    assert text[-1] == NO_APPROVED_SECTIONS_TEXT


def test_word_omits_missing_tender_fields():
    snapshot = make_snapshot()
    snapshot["tender"] = {}
    text = _doc_text(snapshot)
    assert text[0] == "Tender Export"
    assert "Tender Information" not in text


# Warnings ----------------------------------------------------------------------

def test_count_mandatory_without_decision():
    snapshot = make_snapshot()
    # req-1 mandatory/no decision, req-2 mandatory/decided, req-3 optional.
    assert count_mandatory_without_decision(snapshot) == 1
    snapshot["requirements"][1]["decision"]["status"] = "pending"
    assert count_mandatory_without_decision(snapshot) == 2
    snapshot["requirements"][1]["decision"]["status"] = "  "
    assert count_mandatory_without_decision(snapshot) == 2
    snapshot["requirements"][2]["decision"] = None
    assert count_mandatory_without_decision(snapshot) == 2


# Compliance decision allowlist ---------------------------------------------------

def _single_requirement(decision, mandatory=True, evidence_status="REVIEW_REQUIRED"):
    snapshot = make_snapshot()
    snapshot["requirements"] = [{
        "requirementId": "r", "content": "c", "mandatory": mandatory,
        "evidenceMatch": {"status": evidence_status, "evidence": []},
        "decision": decision,
    }]
    return snapshot


def test_decision_values_constant_is_the_documented_allowlist():
    assert DECISION_VALUES == frozenset({
        "COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE",
    })


@pytest.mark.parametrize("status, expected", [
    ("COMPLIANT", "COMPLIANT"),
    ("compliant", "COMPLIANT"),
    ("  Partially_Compliant\t", "PARTIALLY_COMPLIANT"),
    ("non_compliant", "NON_COMPLIANT"),
    ("\nNOT_APPLICABLE ", "NOT_APPLICABLE"),
])
def test_allowed_decisions_are_normalised(status, expected):
    snapshot = _single_requirement({"status": status, "decidedBy": "user-test"})
    row = _by_header(_workbook(snapshot)["Compliance Matrix"], 1)
    assert row["Final Decision"] == expected
    assert count_mandatory_without_decision(snapshot) == 0


@pytest.mark.parametrize("status", [
    None, "", "   ", "UNKNOWN", "unknown", "TBD", "N/A", "n/a", "PENDING",
    "pending", "NO_DECISION", "UNDECIDED", "PARTIALLY COMPLIANT",
    "PARTIALLY-COMPLIANT", "COMPLIANT?", "APPROVED", "YES", "LIKELY_COMPLIANT",
    "REVIEW_REQUIRED", 1, True, ["COMPLIANT"], {"value": "COMPLIANT"},
])
def test_unrecognised_decisions_are_no_decision(status):
    snapshot = _single_requirement({"status": status, "decidedBy": "user-test"})
    row = _by_header(_workbook(snapshot)["Compliance Matrix"], 1)
    assert row["Final Decision"] == "No decision"
    assert count_mandatory_without_decision(snapshot) == 1
    if isinstance(status, str) and status.strip():
        assert status not in [
            v for k, v in row.items() if k not in ("Requirement", "AI Evidence Status")
        ]


@pytest.mark.parametrize("decision", [None, {}, {"decidedBy": "user-test"}])
def test_missing_decision_is_no_decision(decision):
    snapshot = _single_requirement(decision)
    assert _by_header(_workbook(snapshot)["Compliance Matrix"], 1)["Final Decision"] == "No decision"
    assert count_mandatory_without_decision(snapshot) == 1


@pytest.mark.parametrize("evidence_status", [
    "COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE",
])
def test_evidence_status_never_becomes_a_decision(evidence_status):
    snapshot = _single_requirement({"status": "TBD"}, evidence_status=evidence_status)
    snapshot["requirements"][0]["aiRecommendation"] = evidence_status
    row = _by_header(_workbook(snapshot)["Compliance Matrix"], 1)
    assert row["AI Evidence Status"] == evidence_status
    assert row["Final Decision"] == "No decision"
    assert count_mandatory_without_decision(snapshot) == 1


def test_mandatory_without_decision_counts_only_mandatory_unrecognised():
    snapshot = make_snapshot()
    base = snapshot["requirements"][0]
    snapshot["requirements"] = [
        dict(base, requirementId=f"r{i}", mandatory=mandatory, decision=decision)
        for i, (mandatory, decision) in enumerate([
            (True, {"status": "COMPLIANT"}),
            (True, {"status": "non_compliant "}),
            (True, {"status": "N/A"}),
            (True, {"status": "PENDING"}),
            (True, None),
            (False, {"status": "TBD"}),
            (None, None),
            (True, {"status": "Not_Applicable"}),
        ])
    ]
    assert count_mandatory_without_decision(snapshot) == 3
