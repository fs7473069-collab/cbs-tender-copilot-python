"""Excel export: overview, requirements, compliance, and actions."""

from collections import Counter
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from app.export.snapshot import (
    ACTUAL_PDF_PAGE,
    APPROVED,
    DRAFT_PENDING_REVIEW_LABEL,
    MISSING_DRAFT_SECTION,
    NO_DRAFT_SECTIONS,
    decision_status,
    draft_generated_content,
    draft_missing_information_text,
    draft_requirement_ids_text,
    draft_review_status_label,
    draft_reviewer_comments_text,
    draft_source_references_text,
    missing_expected_draft_specs,
    normalize_snapshot,
    ordered_draft_sections,
    source_validation_banner,
)
from app.export.text import excel_text, to_text

NOT_PROVIDED = "Not provided"
PENDING_REVIEW = "Pending Review"
NO_DECISION = PENDING_REVIEW
UNCLASSIFIED = "Unclassified"
EMPTY_ACTIONS_MESSAGE = (
    "No actions are available in this analysis. "
    "Actions are created by reviewers; this export does not invent them."
)

HEADER_FILL = PatternFill(fill_type="solid", fgColor="1F4E78")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(name="Calibri", bold=True, color="1F4E78", size=18)
SECTION_FONT = Font(name="Calibri", bold=True, color="1F4E78", size=12)
LABEL_FONT = Font(name="Calibri", bold=True, size=11)
BODY_FONT = Font(name="Calibri", size=11)
BANNER_FILL = PatternFill(fill_type="solid", fgColor="FFF4D5")
ALT_ROW_FILL = PatternFill(fill_type="solid", fgColor="F7F9FC")
THIN = Border(
    left=Side(style="thin", color="D0D7DE"),
    right=Side(style="thin", color="D0D7DE"),
    top=Side(style="thin", color="D0D7DE"),
    bottom=Side(style="thin", color="D0D7DE"),
)
WRAP_TOP = Alignment(vertical="top", wrap_text=True)
WRAP_CENTER = Alignment(vertical="center", wrap_text=True)

STATUS_FILLS = {
    "REVIEW_REQUIRED": PatternFill(fill_type="solid", fgColor="FDEBD0"),
    "NOT_FOUND": PatternFill(fill_type="solid", fgColor="EAECEE"),
    "NOT_CHECKED": PatternFill(fill_type="solid", fgColor="D6EAF8"),
    "FOUND": PatternFill(fill_type="solid", fgColor="D5F5E3"),
    PENDING_REVIEW: PatternFill(fill_type="solid", fgColor="FCF3CF"),
    "COMPLIANT": PatternFill(fill_type="solid", fgColor="D5F5E3"),
    "PARTIALLY_COMPLIANT": PatternFill(fill_type="solid", fgColor="FDEBD0"),
    "NON_COMPLIANT": PatternFill(fill_type="solid", fgColor="FADBD8"),
    "NOT_APPLICABLE": PatternFill(fill_type="solid", fgColor="EAECEE"),
}

REQUIREMENTS_HEADERS = [
    "Requirement ID",
    "Source Item Key",
    "Requirement",
    "Mandatory",
    "Category",
    "Confidence",
    "Source File",
    "Page Number",
    "Section",
    "Clause",
    "Sheet",
    "Cell Range",
    "Original Tender Text",
]
COMPLIANCE_HEADERS = [
    "Requirement ID",
    "Requirement",
    "Mandatory",
    "AI Evidence Status",
    "AI Evidence Note",
    "Evidence References",
    "AI Recommendation",
    "Final Decision",
    "Decided By",
    "Decided At",
    "Decision Comment",
]
RECOMMENDATIONS_HEADERS = [
    "Requirement ID",
    "Recommendation",
    "Rationale",
]
ACTIONS_HEADERS = [
    "Action ID",
    "Requirement ID",
    "Title",
    "Description",
    "Owner",
    "Due Date",
    "Status",
]
DRAFT_SHEET_NAME = "AI Draft Responses"
DRAFT_HEADERS = [
    "Section ID",
    "Section Type",
    "Title",
    "Review Status",
    "Generated Content",
    "Related Requirement IDs",
    "Source References",
    "Missing Information",
    "Reviewer Comments",
]
DRAFT_COLUMN_WIDTHS = [18, 28, 36, 28, 80, 28, 40, 40, 28]


def _mandatory(value):
    return NOT_PROVIDED if value is None else to_text(value)


def _category(value):
    text = to_text(value).strip()
    return text if text else UNCLASSIFIED


def _display_or_not_provided(value):
    text = to_text(value).strip()
    return text if text else NOT_PROVIDED


def _source_file(source):
    """Visible filename only. documentId stays on the source object, unused here."""
    return to_text(source.get("filename"))


def _page_number(source):
    page = to_text(source.get("pageNo"))
    if not page.strip():
        return ""
    reference = source.get("pageReferenceType")
    if reference is None:
        reference = source.get("page_reference_type")
    if (
        isinstance(reference, str)
        and reference.strip()
        and reference.strip() != ACTUAL_PDF_PAGE
    ):
        return f"{page} (unverified)"
    return page


def _join_per_source(sources, extract):
    """One line per source, numbered when there is more than one."""
    values = [to_text(extract(source)) for source in sources]
    if not any(value.strip() for value in values):
        return ""
    if len(values) == 1:
        return values[0] if values else ""
    return "\n".join(f"[{index}] {value}" for index, value in enumerate(values, 1))


def _requirement_row(requirement):
    sources = requirement["sources"]
    return [
        requirement.get("requirementId"),
        requirement.get("sourceItemKey"),
        requirement.get("content"),
        _mandatory(requirement.get("mandatory")),
        _category(requirement.get("category")),
        requirement.get("confidence"),
        _join_per_source(sources, _source_file),
        _join_per_source(sources, _page_number),
        _join_per_source(sources, lambda s: s.get("section")),
        _join_per_source(sources, lambda s: s.get("clause")),
        _join_per_source(sources, lambda s: s.get("sheet")),
        _join_per_source(sources, lambda s: s.get("cellRange")),
        _join_per_source(sources, lambda s: s.get("excerpt")),
    ]


def _evidence_references(evidence_match):
    lines = []
    for item in (evidence_match or {}).get("evidence", []):
        parts = [to_text(item.get("source")) or "Unknown source"]
        if item.get("page") is not None:
            parts.append(f"p.{to_text(item.get('page'))}")
        if item.get("similarity") is not None:
            parts.append(f"similarity={to_text(item.get('similarity'))}")
        lines.append(", ".join(parts))
    return "\n".join(lines)


def _ai_recommendation(value):
    if isinstance(value, str):
        return value if value.strip() else NOT_PROVIDED
    if isinstance(value, dict):
        recommendation = to_text(value.get("recommendation")).strip()
        rationale = to_text(value.get("rationale")).strip()
        if not recommendation:
            return NOT_PROVIDED
        return f"{recommendation}: {rationale}" if rationale else recommendation
    return NOT_PROVIDED


def _human_decision_label(requirement):
    status = decision_status(requirement)
    return status if status else PENDING_REVIEW


def _compliance_row(requirement):
    evidence_match = requirement.get("evidenceMatch")
    decision = requirement.get("decision") or {}
    return [
        requirement.get("requirementId") or requirement.get("sourceItemKey"),
        requirement.get("content"),
        _mandatory(requirement.get("mandatory")),
        (evidence_match or {}).get("status") or NOT_PROVIDED,
        (evidence_match or {}).get("note"),
        _evidence_references(evidence_match),
        _ai_recommendation(requirement.get("aiRecommendation")),
        _human_decision_label(requirement),
        decision.get("decidedBy"),
        decision.get("decidedAt"),
        decision.get("comment"),
    ]


def _recommendation_row(item):
    if isinstance(item, str):
        return ["", item, ""]
    return [
        item.get("requirementId"),
        item.get("recommendation") or item.get("title"),
        item.get("rationale") or item.get("description"),
    ]


def _action_row(action):
    return [
        action.get("actionId"),
        action.get("requirementId"),
        action.get("title"),
        action.get("description"),
        action.get("owner"),
        action.get("dueDate"),
        action.get("status"),
    ]


def _draft_row(section):
    return [
        section.get("sectionId"),
        section.get("sectionType"),
        section.get("title"),
        draft_review_status_label(section),
        draft_generated_content(section),
        draft_requirement_ids_text(section),
        draft_source_references_text(section),
        draft_missing_information_text(section),
        draft_reviewer_comments_text(section),
    ]


def _missing_draft_row(section_type, title):
    return [
        "",
        section_type,
        title,
        DRAFT_PENDING_REVIEW_LABEL,
        MISSING_DRAFT_SECTION,
        "",
        "",
        "",
        "",
    ]


def _empty_draft_row():
    return [
        "",
        "",
        "",
        DRAFT_PENDING_REVIEW_LABEL,
        NO_DRAFT_SECTIONS,
        "",
        "",
        "",
        "",
    ]


def _draft_row_height(content):
    text = to_text(content)
    lines = text.count("\n") + 1
    return min(409, max(72, lines * 14))


def _write_row(sheet, row_index, values):
    for column, value in enumerate(values, start=1):
        cell = sheet.cell(row=row_index, column=column, value=excel_text(value))
        cell.data_type = "s"
        cell.font = BODY_FONT
        cell.border = THIN
        cell.alignment = WRAP_TOP


def _style_header(sheet, widths):
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 28
    for cell in sheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = WRAP_CENTER
        cell.border = THIN
    for row_index, row in enumerate(sheet.iter_rows(min_row=2), start=2):
        sheet.row_dimensions[row_index].height = 48
        stripe = ALT_ROW_FILL if row_index % 2 == 0 else None
        for cell in row:
            cell.alignment = WRAP_TOP
            if stripe is not None and cell.fill.fill_type is None:
                cell.fill = stripe
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    last_col = get_column_letter(sheet.max_column)
    table = Table(
        displayName=sheet.title.replace(" ", "") + "Table",
        ref=f"A1:{last_col}{sheet.max_row}",
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)


def _apply_status_colour(sheet, headers, column_name):
    if column_name not in headers:
        return
    column = headers.index(column_name) + 1
    for row in sheet.iter_rows(min_row=2, min_col=column, max_col=column):
        cell = row[0]
        fill = STATUS_FILLS.get(to_text(cell.value).strip())
        if fill is not None:
            cell.fill = fill


def _overview_counts(normalized):
    requirements = normalized["requirements"]
    evidence_counts = Counter()
    decision_counts = Counter()
    mandatory = 0
    for requirement in requirements:
        evidence = requirement.get("evidenceMatch") or {}
        evidence_counts[to_text(evidence.get("status")).strip() or NOT_PROVIDED] += 1
        decision_counts[_human_decision_label(requirement)] += 1
        if requirement.get("mandatory") is True:
            mandatory += 1
    return {
        "total": len(requirements),
        "mandatory": mandatory,
        "evidence": evidence_counts,
        "decisions": decision_counts,
        "actions": len(normalized["actions"]),
        "recommendations": len(normalized["recommendations"]),
    }


def _write_overview(sheet, normalized):
    tender = normalized["tender"]
    counts = _overview_counts(normalized)
    banner = source_validation_banner(normalized)
    sheet.sheet_view.showGridLines = False
    sheet.column_dimensions["A"].width = 38
    sheet.column_dimensions["B"].width = 72
    sheet.row_dimensions[1].height = 28

    title = sheet.cell(row=1, column=1, value="Tender Copilot — Analysis Export")
    title.font = TITLE_FONT
    sheet.merge_cells("A1:B1")

    row = 3
    if banner:
        cell = sheet.cell(row=row, column=1, value=excel_text(banner))
        cell.fill = BANNER_FILL
        cell.font = Font(name="Calibri", bold=True, size=11, color="7D6608")
        cell.alignment = WRAP_TOP
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        sheet.row_dimensions[row].height = 48
        row += 2

    sheet.cell(row=row, column=1, value="Tender information").font = SECTION_FONT
    row += 1
    for label, value in (
        ("Title", _display_or_not_provided(tender.get("title"))),
        ("Customer", _display_or_not_provided(tender.get("customerName"))),
        ("Reference", _display_or_not_provided(tender.get("tenderReference"))),
        ("Deadline", _display_or_not_provided(tender.get("submissionDeadline"))),
        ("Total requirements", str(counts["total"])),
        ("Mandatory requirements", str(counts["mandatory"])),
    ):
        sheet.cell(row=row, column=1, value=label).font = LABEL_FONT
        sheet.cell(row=row, column=2, value=excel_text(value)).font = BODY_FONT
        row += 1

    row += 1
    sheet.cell(row=row, column=1, value="AI evidence status").font = SECTION_FONT
    row += 1
    sheet.cell(row=row, column=2, value="Not a human compliance decision").font = BODY_FONT
    row += 1
    if counts["evidence"]:
        for status, total in sorted(counts["evidence"].items()):
            sheet.cell(row=row, column=1, value=status).font = LABEL_FONT
            sheet.cell(row=row, column=2, value=str(total)).font = BODY_FONT
            fill = STATUS_FILLS.get(status)
            if fill is not None:
                sheet.cell(row=row, column=1).fill = fill
            row += 1

    row += 1
    sheet.cell(row=row, column=1, value="Human compliance decisions").font = SECTION_FONT
    row += 1
    for status, total in sorted(counts["decisions"].items()):
        sheet.cell(row=row, column=1, value=status).font = LABEL_FONT
        sheet.cell(row=row, column=2, value=str(total)).font = BODY_FONT
        fill = STATUS_FILLS.get(status)
        if fill is not None:
            sheet.cell(row=row, column=1).fill = fill
        row += 1

    row += 1
    notes = [
        "Source file names are shown when present. Document IDs are not displayed.",
        EMPTY_ACTIONS_MESSAGE if counts["actions"] == 0 else None,
    ]
    sheet.cell(row=row, column=1, value="Notes").font = SECTION_FONT
    row += 1
    for note in notes:
        if not note:
            continue
        cell = sheet.cell(row=row, column=1, value=excel_text(note))
        cell.alignment = WRAP_TOP
        cell.font = BODY_FONT
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        sheet.row_dimensions[row].height = 28
        row += 1


def _add_data_sheet(workbook, title, headers, widths, rows):
    sheet = workbook.create_sheet(title)
    _write_row(sheet, 1, headers)
    for row_index, values in enumerate(rows, start=2):
        _write_row(sheet, row_index, values)
    _style_header(sheet, widths)
    return sheet


def render_excel(snapshot):
    """Render the snapshot to .xlsx bytes."""
    normalized = normalize_snapshot(snapshot)
    action_items = normalized["actions"]
    action_rows = (
        [_action_row(item) for item in action_items]
        if action_items
        else [["", "", EMPTY_ACTIONS_MESSAGE, "", "", "", ""]]
    )

    workbook = Workbook()
    workbook.remove(workbook.active)
    overview = workbook.create_sheet("Tender Overview", 0)
    _write_overview(overview, normalized)

    _add_data_sheet(
        workbook,
        "Requirements",
        REQUIREMENTS_HEADERS,
        [16, 16, 70, 12, 16, 12, 28, 16, 22, 14, 14, 14, 60],
        [_requirement_row(item) for item in normalized["requirements"]],
    )
    compliance = _add_data_sheet(
        workbook,
        "Compliance Matrix",
        COMPLIANCE_HEADERS,
        [16, 70, 12, 20, 45, 45, 32, 18, 18, 22, 40],
        [_compliance_row(item) for item in normalized["requirements"]],
    )
    _apply_status_colour(compliance, COMPLIANCE_HEADERS, "AI Evidence Status")
    _apply_status_colour(compliance, COMPLIANCE_HEADERS, "Final Decision")

    if normalized["recommendations"]:
        _add_data_sheet(
            workbook,
            "Recommendations",
            RECOMMENDATIONS_HEADERS,
            [16, 50, 70],
            [_recommendation_row(item) for item in normalized["recommendations"]],
        )

    _add_data_sheet(
        workbook,
        "Actions",
        ACTIONS_HEADERS,
        [16, 16, 36, 60, 20, 14, 14],
        action_rows,
    )

    draft_sections = ordered_draft_sections(normalized["draftSections"])
    if draft_sections:
        draft_rows = [_draft_row(item) for item in draft_sections]
        for section_type, title, _order in missing_expected_draft_specs(draft_sections):
            draft_rows.append(_missing_draft_row(section_type, title))
    else:
        draft_rows = [_empty_draft_row()]
    drafts = _add_data_sheet(
        workbook,
        DRAFT_SHEET_NAME,
        DRAFT_HEADERS,
        DRAFT_COLUMN_WIDTHS,
        draft_rows,
    )
    for row_index, values in enumerate(draft_rows, start=2):
        drafts.row_dimensions[row_index].height = _draft_row_height(values[4])
        status = to_text(values[3]).strip()
        fill = STATUS_FILLS.get(APPROVED if status == APPROVED else PENDING_REVIEW)
        if fill is not None:
            drafts.cell(row=row_index, column=4).fill = fill

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
