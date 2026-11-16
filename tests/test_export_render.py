from io import BytesIO
from pathlib import Path
import shutil
import subprocess

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT
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
    DRAFT_HEADERS,
    DRAFT_SHEET_NAME,
    EMPTY_ACTIONS_MESSAGE,
    PENDING_REVIEW,
    RECOMMENDATIONS_HEADERS,
    REQUIREMENTS_HEADERS,
    UNCLASSIFIED,
)
from app.export.snapshot import (
    DRAFT_PENDING_REVIEW_LABEL,
    MISSING_DRAFT_CONTENT,
    MISSING_DRAFT_SECTION,
)
from app.export.word import (
    AI_EVIDENCE_CANDIDATE_LABEL,
    DOCUMENT_KIND_ANALYSIS,
    DOCUMENT_KIND_RESPONSE,
    EXCERPT_QUOTE_LIMIT,
    NO_APPROVED_SECTIONS_TEXT,
    PENDING_DRAFTS_HEADING,
    apply_toc_pages,
    collapse_cell_text,
    format_display_date,
    matched_chapter,
    toc_entries_for_snapshot,
    truncate_quote,
)
from app.export.word_pagination import (
    TOC_PAGE_UNRESOLVED,
    convert_docx_to_pdf,
    heading_pages_from_pdf,
)
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
    texts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.append(cell.text)
    return texts


# Excel -------------------------------------------------------------------------

def test_excel_has_overview_and_core_sheets_in_order_with_headers():
    workbook = _workbook(make_snapshot())
    assert workbook.sheetnames == [
        "Tender Overview",
        "Requirements",
        "Compliance Matrix",
        "Actions",
        DRAFT_SHEET_NAME,
    ]
    assert _rows(workbook["Requirements"])[0] == REQUIREMENTS_HEADERS
    assert _rows(workbook["Compliance Matrix"])[0] == COMPLIANCE_HEADERS
    assert _rows(workbook["Actions"])[0] == ACTIONS_HEADERS
    assert _rows(workbook[DRAFT_SHEET_NAME])[0] == DRAFT_HEADERS
    assert "Recommendations" not in workbook.sheetnames


def test_recommendations_sheet_only_when_snapshot_has_recommendations():
    snapshot = make_snapshot()
    snapshot["recommendations"] = [{
        "requirementId": "req-test-1",
        "recommendation": "Keep existing control",
        "rationale": "Already evidenced",
    }]
    workbook = _workbook(snapshot)
    assert workbook.sheetnames == [
        "Tender Overview",
        "Requirements",
        "Compliance Matrix",
        "Recommendations",
        "Actions",
        DRAFT_SHEET_NAME,
    ]
    assert _rows(workbook["Recommendations"])[0] == RECOMMENDATIONS_HEADERS
    assert _by_header(workbook["Recommendations"], 1)["Recommendation"] == (
        "Keep existing control"
    )


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
    assert row["Source File"] == "[1] synthetic.pdf\n[2] "
    assert "doc-test-1" not in (row["Source File"] or "")
    assert row["Page Number"] == "[1] 3\n[2] "
    assert row["Clause"] == "[1] 2.1.4\n[2] "
    assert row["Sheet"] == "[1] \n[2] Pricing"
    assert row["Cell Range"] == "[1] \n[2] B2:C4"
    assert "Synthetic excerpt two." in row["Original Tender Text"]
    third = _by_header(sheet, 3)
    assert third["Source File"] is None
    assert third["Category"] == UNCLASSIFIED


def test_compliance_matrix_keeps_ai_evidence_recommendation_and_decision_separate():
    sheet = _workbook(make_snapshot())["Compliance Matrix"]
    first = _by_header(sheet, 1)
    assert first["AI Evidence Status"] == "REVIEW_REQUIRED"
    assert first["AI Evidence Note"] == "Synthetic evidence note."
    assert first["Evidence References"] == "kb-test.pdf, p.7, similarity=0.81"
    assert first["AI Recommendation"] == "Not provided"
    assert first["Final Decision"] == PENDING_REVIEW
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
    assert row["Final Decision"] == PENDING_REVIEW
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
    rows = _rows(sheet)
    assert rows[0] == ACTIONS_HEADERS
    assert EMPTY_ACTIONS_MESSAGE in rows[1]


def test_excel_draft_sheet_includes_unapproved_content_and_labels():
    snapshot = make_snapshot()
    snapshot["draftSections"][3]["sourceRequirementIds"] = ["REQ-001"]
    snapshot["draftSections"][3]["sources"] = [
        {"filename": "tender.pdf", "page": 4, "clause": "3.1"}
    ]
    snapshot["draftSections"][3]["missingInformation"] = ["24/7 support gap"]
    snapshot["draftSections"][3]["content"] = "Paragraph one.\n\nParagraph two."
    sheet = _workbook(snapshot)[DRAFT_SHEET_NAME]
    rows = _rows(sheet)
    assert rows[0] == DRAFT_HEADERS
    by_title = {
        dict(zip(rows[0], row))["Title"]: dict(zip(rows[0], row))
        for row in rows[1:]
    }
    draft = by_title["Draft"]
    assert draft["Review Status"] == DRAFT_PENDING_REVIEW_LABEL
    assert "Paragraph one." in draft["Generated Content"]
    assert "Paragraph two." in draft["Generated Content"]
    assert "\n" in draft["Generated Content"]
    assert draft["Related Requirement IDs"] == "REQ-001"
    assert "tender.pdf" in draft["Source References"]
    assert "p.4" in draft["Source References"]
    assert "24/7 support gap" in draft["Missing Information"]
    assert draft["Reviewer Comments"] in (None, "")
    approved = by_title["First"]
    assert approved["Review Status"] == "APPROVED"
    rejected = by_title["Rejected"]
    assert rejected["Review Status"] == DRAFT_PENDING_REVIEW_LABEL
    assert rejected["Review Status"] != "APPROVED"


def test_excel_missing_and_truncated_drafts_are_honest():
    snapshot = make_snapshot()
    snapshot["draftSections"] = [
        {
            "sectionId": "DRAFT-CAPABILITY_RESPONSE",
            "sectionType": "CAPABILITY_RESPONSE",
            "title": "Executive Summary",
            "content": "Capability draft.",
            "approvalStatus": "DRAFT",
            "order": 1,
        },
        {
            "sectionId": "DRAFT-TECHNICAL_APPROACH",
            "sectionType": "TECHNICAL_APPROACH",
            "title": "Proposed Technical Approach",
            "content": "",
            "approvalStatus": "DRAFT",
            "order": 3,
        },
    ]
    sheet = _workbook(snapshot)[DRAFT_SHEET_NAME]
    raw = _rows(sheet)
    rows = [dict(zip(raw[0], row)) for row in raw[1:]]
    by_type = {row["Section Type"]: row for row in rows}
    assert by_type["CAPABILITY_RESPONSE"]["Generated Content"] == "Capability draft."
    assert by_type["TECHNICAL_APPROACH"]["Generated Content"] == MISSING_DRAFT_CONTENT
    assert by_type["RELEVANT_EXPERIENCE"]["Generated Content"] == MISSING_DRAFT_SECTION
    assert by_type["RELEVANT_EXPERIENCE"]["Review Status"] == DRAFT_PENDING_REVIEW_LABEL
    assert all(row["Review Status"] != "APPROVED" for row in rows)


def test_word_pending_review_section_holds_drafts_not_approved_chapters():
    snapshot = make_snapshot()
    snapshot["draftSections"] = [
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
            "content": "Pending technical draft.",
            "approvalStatus": "DRAFT",
            "missingInformation": ["No CBS evidence for 24/7 support."],
            "order": 3,
        },
    ]
    paragraphs = [item for item in _doc_text(snapshot) if item]
    pending_at = paragraphs.index(PENDING_DRAFTS_HEADING)
    understanding_at = paragraphs.index("Understanding of Requirements")
    approach_chapter = paragraphs.index("Proposed Technical Approach")
    assert understanding_at < pending_at
    assert "Approved understanding of SSO." in paragraphs[understanding_at:pending_at]
    assert "Pending technical draft." not in paragraphs[:pending_at]
    assert "Pending technical draft." in paragraphs[pending_at:]
    assert DRAFT_PENDING_REVIEW_LABEL in paragraphs[pending_at:]
    assert any(
        "No CBS evidence for 24/7 support." in item
        for item in paragraphs[pending_at:]
    )
    assert paragraphs[approach_chapter + 1] != "Pending technical draft."


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
    assert "TEST-ONLY Synthetic Tender" in text
    assert "Test Customer (synthetic)" in text
    assert "TEST-REF-001" in text
    assert "31 January 2099, 12:00 UTC" in text
    assert "2099-01-31T12:00:00Z" not in text
    paragraphs = [item for item in _doc_text(make_snapshot()) if item]
    start = paragraphs.index("First")
    headings = paragraphs[start:]
    assert headings[:7] == ["First", "A one", "A two", "Second", "B para", "Unordered", "C para"]
    assert paragraphs.index("Approved Draft Sections") < start
    joined = "\n".join(text)
    pending_at = paragraphs.index(PENDING_DRAFTS_HEADING)
    assert pending_at > paragraphs.index("Approved Draft Sections")
    for body in ("PENDING TEXT", "DRAFT TEXT", "REJECTED TEXT",
                 "MISSING TEXT", "NULL TEXT", "UNKNOWN TEXT"):
        assert body not in paragraphs[:pending_at]
        assert body in paragraphs[pending_at:]
    assert DRAFT_PENDING_REVIEW_LABEL in paragraphs[pending_at:]
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
    assert DOCUMENT_KIND_ANALYSIS in text
    assert DOCUMENT_KIND_RESPONSE not in "\n".join(text)
    joined = "\n".join(text)
    if sections:
        assert PENDING_DRAFTS_HEADING in text
        assert "secret draft" in joined
        assert DRAFT_PENDING_REVIEW_LABEL in joined
        exec_at = text.index("Executive Summary")
        pending_at = text.index(PENDING_DRAFTS_HEADING)
        assert exec_at < pending_at
        assert "secret draft" not in "\n".join(text[exec_at:pending_at])
    else:
        assert "secret draft" not in joined
        assert PENDING_DRAFTS_HEADING not in text


def test_unverified_page_numbers_are_labelled_without_inventing_values():
    snapshot = make_snapshot()
    snapshot["requirements"][0]["sources"][0]["page_reference_type"] = (
        "approximate_docx_boundary"
    )
    workbook = _workbook(snapshot)
    row = _by_header(workbook["Requirements"], 1)
    assert row["Page Number"] == "[1] 3 (unverified)\n[2] "
    overview_text = " ".join(
        str(cell or "")
        for excel_row in _rows(workbook["Tender Overview"])
        for cell in excel_row
    )
    assert "pending source validation" in overview_text.lower()


def test_word_shows_not_provided_for_missing_tender_fields():
    snapshot = make_snapshot()
    snapshot["tender"] = {}
    text = _doc_text(snapshot)
    assert "Tender Export" in text
    assert "Customer" in text
    assert "Reference" in text
    assert "Deadline" in text
    assert "Table of Contents" in text
    assert "Executive Summary" in text
    assert "Understanding of Requirements" in text
    assert "Proposed Technical Approach" in text
    assert "Implementation Methodology" in text
    assert "Company Experience and Capabilities" in text
    assert "Requirement-by-Requirement Compliance Response" in text
    assert "Assumptions and Information Required" in text
    assert "Conclusion" in text
    assert "Appendix A — Extracted Requirements Summary" in text
    assert "Appendix B — Evidence References" in text
    assert "\n".join(text).count("Not provided") >= 3


def test_word_empty_requirements_snapshot_has_structure_without_invented_drafts():
    text = _doc_text({"requirements": []})
    assert "Tender Export" in text
    assert "Requirement-by-Requirement Compliance Response" in text
    assert NO_APPROVED_SECTIONS_TEXT in text
    assert "secret draft" not in "\n".join(text)
    assert "PENDING TEXT" not in "\n".join(text)


def test_word_dates_are_human_readable():
    assert format_display_date("2099-01-31T12:00:00Z") == "31 January 2099, 12:00 UTC"
    assert format_display_date("2099-01-15") == "15 January 2099"
    assert format_display_date("not a date") == "not a date"
    assert format_display_date("") == "Not provided"
    snapshot = make_snapshot()
    snapshot["actions"][0]["dueDate"] = "2099-01-15"
    text = "\n".join(_doc_text(snapshot))
    assert "15 January 2099" in text
    assert "2099-01-15" not in text


def test_word_places_matching_approved_drafts_under_chapters():
    snapshot = make_snapshot()
    snapshot["draftSections"] = [
        {
            "title": "Executive Summary",
            "content": "Approved exec body.",
            "approvalStatus": "APPROVED",
            "order": 1,
        },
        {
            "title": "Proposed Technical Approach: delivery",
            "content": "Approved approach body.",
            "approvalStatus": "APPROVED",
            "order": 2,
        },
        {
            "title": "Quality Assurance",
            "content": "Approved unmatched body.",
            "approvalStatus": "APPROVED",
            "order": 3,
        },
        {
            "title": "Executive Summary",
            "content": "Secret draft body.",
            "approvalStatus": "DRAFT",
        },
    ]
    assert matched_chapter("Executive Summary") == "Executive Summary"
    assert matched_chapter("Proposed Technical Approach: delivery") == (
        "Proposed Technical Approach"
    )
    assert matched_chapter("Quality Assurance") is None
    document = Document(BytesIO(render_word(snapshot)))
    paragraphs = [p.text for p in document.paragraphs if p.text]
    exec_at = paragraphs.index("Executive Summary")
    approach_at = paragraphs.index("Proposed Technical Approach")
    unmatched_at = paragraphs.index("Approved Draft Sections")
    qa_at = paragraphs.index("Quality Assurance")
    body_at = paragraphs.index("Approved exec body.")
    assert exec_at < body_at < approach_at
    assert "Approved approach body." in paragraphs[approach_at:unmatched_at]
    assert qa_at > unmatched_at
    pending_at = paragraphs.index(PENDING_DRAFTS_HEADING)
    assert pending_at > unmatched_at
    assert "Secret draft body." not in paragraphs[:pending_at]
    assert "Secret draft body." in paragraphs[pending_at:]
    assert DRAFT_PENDING_REVIEW_LABEL in paragraphs[pending_at:]
    assert paragraphs.count("Approved Draft Sections") == 1


def test_word_omits_generic_approved_chapter_when_all_drafts_match():
    snapshot = make_snapshot()
    snapshot["draftSections"] = [{
        "title": "Conclusion",
        "content": "Approved conclusion body.",
        "approvalStatus": "APPROVED",
    }]
    text = _doc_text(snapshot)
    assert "Approved conclusion body." in text
    assert "Approved Draft Sections" not in text
    assert text.count(NO_APPROVED_SECTIONS_TEXT) == 0


def test_word_empty_drafts_state_missing_content_once():
    snapshot = make_snapshot()
    snapshot["draftSections"] = [
        {"title": "Executive Summary", "content": "hidden", "approvalStatus": "DRAFT"},
    ]
    text = _doc_text(snapshot)
    joined = "\n".join(text)
    assert joined.count(NO_APPROVED_SECTIONS_TEXT) == 1
    assert joined.count("Information Required") >= 1
    assert "an approved executive summary has not been supplied" not in joined.lower()
    assert PENDING_DRAFTS_HEADING in text
    pending_at = text.index(PENDING_DRAFTS_HEADING)
    assert "hidden" not in "\n".join(text[:pending_at])
    assert "hidden" in joined
    assert "Approved Draft Sections" not in text


def test_word_long_compliance_table_repeats_header_and_sets_widths():
    snapshot = make_snapshot()
    base = snapshot["requirements"][0]
    snapshot["requirements"] = [
        dict(
            base,
            requirementId=f"req-long-{index}",
            content=f"Long synthetic requirement {index} " + ("wording " * 20),
        )
        for index in range(40)
    ]
    document = Document(BytesIO(render_word(snapshot)))
    compliance = max(document.tables, key=lambda table: len(table.rows))
    assert len(compliance.rows) == 41
    assert [cell.text for cell in compliance.rows[0].cells] == [
        "Requirement ID",
        "Requirement",
        "Mandatory",
        "Category",
        "AI Evidence Status",
        "Human Decision",
        "Source",
    ]
    xml = compliance._tbl.xml
    assert "tblHeader" in xml
    assert 'w:type="fixed"' in xml or "tblLayout" in xml
    assert "req-long-0" in compliance.rows[1].cells[0].text
    assert "req-long-39" in compliance.rows[40].cells[0].text
    assert "REVIEW_REQUIRED" in compliance.rows[1].cells[4].text
    assert "Pending Review" in compliance.rows[1].cells[5].text
    assert document.sections[0].orientation == WD_ORIENT.PORTRAIT
    landscape = [section for section in document.sections if section.orientation == WD_ORIENT.LANDSCAPE]
    assert landscape
    assert landscape[0].page_width > landscape[0].page_height
    settings_xml = document.settings.element.xml
    assert "updateFields" in settings_xml
    body_xml = document.element.xml
    assert "TOC \\o" not in body_xml
    toc = next(
        table for table in document.tables
        if table.rows[0].cells[0].text == "Section"
    )
    toc_text = "\n".join(cell.text for row in toc.rows for cell in row.cells)
    assert "Requirement-by-Requirement Compliance Response" in toc_text
    pages = [row.cells[1].text for row in toc.rows[1:]]
    assert pages[0] == "1"
    assert TOC_PAGE_UNRESOLVED in pages


def _toc_table(document):
    return next(
        table for table in document.tables
        if table.rows and table.rows[0].cells[0].text == "Section"
    )


def test_word_static_toc_lists_chapters_with_page_numbers():
    snapshot = make_snapshot()
    document = Document(BytesIO(render_word(snapshot)))
    toc = _toc_table(document)
    rows = [[cell.text for cell in row.cells] for row in toc.rows]
    assert rows[0] == ["Section", "Page"]
    titles = [row[0] for row in rows[1:]]
    pages = [row[1] for row in rows[1:]]
    assert titles[0] == "Cover Page"
    assert pages[0] == "1"
    for heading in (
        "Executive Summary",
        "Understanding of Requirements",
        "Proposed Technical Approach",
        "Implementation Methodology",
        "Company Experience and Capabilities",
        "Requirement-by-Requirement Compliance Response",
        "Assumptions and Information Required",
        "Conclusion",
        "Approved Draft Sections",
        PENDING_DRAFTS_HEADING,
        "Appendix A — Extracted Requirements Summary",
        "Appendix B — Evidence References",
    ):
        assert heading in titles
    assert pages[1:] == [TOC_PAGE_UNRESOLVED] * (len(pages) - 1)
    assert toc_entries_for_snapshot(snapshot) == titles
    xml = document.element.xml
    assert "TOC \\o" not in xml
    assert xml.count('w:type="page"') >= 4
    footer_xml = "".join(section.footer._element.xml for section in document.sections)
    assert "PAGE" in footer_xml


def test_word_compliance_and_appendices_use_landscape():
    document = Document(BytesIO(render_word(make_snapshot())))
    orientations = [section.orientation for section in document.sections]
    assert orientations[0] == WD_ORIENT.PORTRAIT
    assert WD_ORIENT.LANDSCAPE in orientations
    landscape = [section for section in document.sections if section.orientation == WD_ORIENT.LANDSCAPE]
    assert len(landscape) >= 2
    for section in landscape:
        assert section.page_width > section.page_height
    compliance = next(
        table for table in document.tables
        if table.rows[0].cells[0].text == "Requirement ID"
        and table.rows[0].cells[1].text == "Requirement"
        and len(table.columns) == 7
    )
    assert "tblHeader" in compliance._tbl.xml
    assert "cantSplit" not in compliance.rows[1]._tr.xml
    appendix = next(
        table for table in document.tables
        if [cell.text for cell in table.rows[0].cells]
        == ["Requirement ID(s)", "Source", "Excerpt (truncated)"]
    )
    assert "Synthetic excerpt one." in appendix.rows[1].cells[2].text
    evidence = next(
        table for table in document.tables
        if table.rows[0].cells[-1].text == "Candidate excerpt (unverified)"
    )
    assert "kb-test.pdf" in evidence.rows[1].cells[1].text


def _soffice_binary():
    for candidate in (
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def test_heading_pages_from_pdf_ignores_toc_table_matches(tmp_path):
    import fitz
    pdf = fitz.open()
    cover = pdf.new_page()
    cover.insert_text((72, 72), "Cover")
    toc = pdf.new_page()
    toc.insert_text((72, 72), "Table of Contents")
    toc.insert_text((72, 100), "Section")
    toc.insert_text((72, 128), "Page")
    toc.insert_text((72, 156), "Executive Summary")
    body = pdf.new_page()
    body.insert_text((72, 72), "Executive Summary")
    path = tmp_path / "headings.pdf"
    pdf.save(path)
    located = heading_pages_from_pdf(path, ["Cover Page", "Executive Summary"])
    assert located["Cover Page"] == 1
    assert located["Executive Summary"] == 3


def test_word_cover_is_analysis_report_without_approved_drafts():
    snapshot = make_snapshot()
    snapshot["draftSections"] = []
    text = "\n".join(_doc_text(snapshot))
    assert DOCUMENT_KIND_ANALYSIS in text
    assert "TENDER ANALYSIS REPORT" in text
    assert DOCUMENT_KIND_RESPONSE not in text


def test_word_cover_is_tender_response_when_drafts_are_approved():
    text = "\n".join(_doc_text(make_snapshot()))
    assert DOCUMENT_KIND_RESPONSE in text
    assert "TENDER RESPONSE" in text


def test_word_table_cells_collapse_newlines_to_spaces():
    assert collapse_cell_text("Note:\nConstelle\nwill\nprovide") == (
        "Note: Constelle will provide"
    )
    snapshot = make_snapshot()
    snapshot["requirements"][0]["content"] = "Line one.\nLine two."
    snapshot["requirements"][0]["sources"][0]["excerpt"] = "Word\none\nper\nline " + ("x" * 40)
    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    joined = "\n".join(
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    )
    assert "Line one. Line two." in joined
    assert "Word\none\nper\nline" not in joined
    assert "w:br" not in document.tables[-1]._tbl.xml or "Word one per line" in joined


def test_word_appendix_a_deduplicates_and_truncates_excerpts():
    snapshot = make_snapshot()
    shared = ("Shared synthetic excerpt. " * 40).strip()
    snapshot["requirements"][0]["sources"] = [{
        "filename": "synthetic.pdf", "pageNo": 3, "clause": "2.1", "excerpt": shared,
    }]
    snapshot["requirements"][1]["sources"] = [{
        "filename": "synthetic.pdf", "pageNo": 3, "clause": "2.1", "excerpt": shared,
    }]
    snapshot["requirements"][2]["sources"] = [{
        "filename": "other.pdf", "pageNo": 9, "excerpt": "Short unique excerpt.",
    }]
    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    appendix = next(
        table for table in document.tables
        if table.rows[0].cells[0].text == "Requirement ID(s)"
    )
    body = [[cell.text for cell in row.cells] for row in appendix.rows[1:]]
    shared_row = next(row for row in body if "req-test-1" in row[0])
    assert "req-test-2" in shared_row[0]
    assert shared_row[2] == truncate_quote(shared)
    assert len(shared_row[2]) <= EXCERPT_QUOTE_LIMIT + 1
    assert shared not in "\n".join(cell for row in body for cell in row)
    unique_row = next(row for row in body if "req-test-3" in row[0])
    assert unique_row[2] == "Short unique excerpt."
    assert len(appendix.rows) == 3


def test_word_appendix_b_labels_unverified_candidates_and_truncates():
    snapshot = make_snapshot()
    long_text = "Constelle\nwill\nprovide " + ("evidence word " * 80)
    snapshot["requirements"][0]["evidenceMatch"]["evidence"][0]["text"] = long_text
    snapshot["requirements"][0]["evidenceMatch"]["evidence"][0]["similarity"] = 0.6395
    text = "\n".join(_doc_text(snapshot))
    assert AI_EVIDENCE_CANDIDATE_LABEL in text
    assert "confirmed evidence" not in text.lower()
    assert "approved evidence" not in text.lower()
    document = Document(BytesIO(render_word(snapshot, paginator=False)))
    evidence = next(
        table for table in document.tables
        if table.rows[0].cells[-1].text == "Candidate excerpt (unverified)"
    )
    headers = [cell.text for cell in evidence.rows[0].cells]
    assert "Similarity" in headers
    assert "AI Evidence Status" in headers
    row = [cell.text for cell in evidence.rows[1].cells]
    assert row[4] == "REVIEW_REQUIRED"
    assert row[3] == "0.639"
    assert "\n" not in row[-1]
    assert row[-1] == truncate_quote(long_text)
    assert long_text not in row[-1]


def test_word_toc_uses_rendered_pages_from_paginator():
    snapshot = make_snapshot()
    pages = {
        "Cover Page": 1,
        "Executive Summary": 3,
        "Assumptions and Information Required": 11,
        "Conclusion": 11,
        "Approved Draft Sections": 12,
        "Appendix A — Extracted Requirements Summary": 13,
        "Appendix B — Evidence References": 19,
    }

    def paginator(_docx, titles):
        assert "Appendix B — Evidence References" in titles
        return pages

    document = Document(BytesIO(render_word(snapshot, paginator=paginator)))
    toc = {row.cells[0].text: row.cells[1].text for row in _toc_table(document).rows[1:]}
    assert toc["Cover Page"] == "1"
    assert toc["Assumptions and Information Required"] == "11"
    assert toc["Conclusion"] == "11"
    assert toc["Approved Draft Sections"] == "12"
    assert toc["Appendix A — Extracted Requirements Summary"] == "13"
    assert toc["Appendix B — Evidence References"] == "19"
    assert toc["Understanding of Requirements"] == TOC_PAGE_UNRESOLVED


def test_apply_toc_pages_does_not_invent_missing_entries():
    snapshot = make_snapshot()
    raw = render_word(snapshot, paginator=False)
    patched = apply_toc_pages(raw, {"Executive Summary": 3})
    toc = {row.cells[0].text: row.cells[1].text for row in _toc_table(Document(BytesIO(patched))).rows[1:]}
    assert toc["Executive Summary"] == "3"
    assert toc["Appendix B — Evidence References"] == TOC_PAGE_UNRESOLVED


@pytest.mark.skipif(_soffice_binary() is None, reason="LibreOffice is not installed")
def test_libreoffice_pdf_toc_matches_heading_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_WORD_PAGINATE", "1")
    snapshot = make_snapshot()
    data = render_word(snapshot)
    pdf_path = convert_docx_to_pdf(data, tmp_path)
    toc = {
        row.cells[0].text: row.cells[1].text
        for row in _toc_table(Document(BytesIO(data))).rows[1:]
    }
    located = heading_pages_from_pdf(pdf_path, list(toc))
    for title, page in toc.items():
        if page == TOC_PAGE_UNRESOLVED:
            continue
        assert located[title] == int(page), (title, page, located.get(title))
    assert toc["Cover Page"] == "1"
    assert all(
        page == TOC_PAGE_UNRESOLVED or page.isdigit()
        for page in toc.values()
    )
    import fitz
    pdf_text = "\n".join(page.get_text() for page in fitz.open(pdf_path))
    assert "Table of Contents" in pdf_text
    assert "Executive Summary" in pdf_text


def test_word_requirement_table_keeps_evidence_separate_from_decisions():
    text = _doc_text(make_snapshot())
    joined = "\n".join(text)
    assert "Synthetic requirement one." in joined
    assert "REVIEW_REQUIRED" in joined
    assert "NOT_FOUND" in joined
    assert "kb-test.pdf" in joined
    assert "Synthetic excerpt one." in joined
    assert "COMPLIANT" in joined
    first = _by_header(_workbook(make_snapshot())["Compliance Matrix"], 1)
    assert first["Final Decision"] == PENDING_REVIEW
    assert first["AI Evidence Status"] == "REVIEW_REQUIRED"


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
    assert row["Final Decision"] == PENDING_REVIEW
    assert count_mandatory_without_decision(snapshot) == 1
    if isinstance(status, str) and status.strip():
        assert status not in [
            v for k, v in row.items() if k not in ("Requirement", "AI Evidence Status")
        ]


@pytest.mark.parametrize("decision", [None, {}, {"decidedBy": "user-test"}])
def test_missing_decision_is_no_decision(decision):
    snapshot = _single_requirement(decision)
    assert _by_header(_workbook(snapshot)["Compliance Matrix"], 1)["Final Decision"] == PENDING_REVIEW
    assert count_mandatory_without_decision(snapshot) == 1


@pytest.mark.parametrize("evidence_status", [
    "COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE",
])
def test_evidence_status_never_becomes_a_decision(evidence_status):
    snapshot = _single_requirement({"status": "TBD"}, evidence_status=evidence_status)
    snapshot["requirements"][0]["aiRecommendation"] = evidence_status
    row = _by_header(_workbook(snapshot)["Compliance Matrix"], 1)
    assert row["AI Evidence Status"] == evidence_status
    assert row["Final Decision"] == PENDING_REVIEW
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
