"""Excel export: Requirements, Compliance Matrix and Actions sheets."""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.export.snapshot import decision_status, normalize_snapshot
from app.export.text import excel_text, to_text

NOT_PROVIDED = "Not provided"
NO_DECISION = "No decision"

REQUIREMENTS_HEADERS = [
    "Requirement ID",
    "Source Item Key",
    "Requirement",
    "Mandatory",
    "Category",
    "Confidence",
    "Source Count",
    "Source Document",
    "Source Page",
    "Source Section",
    "Source Clause",
    "Source Sheet",
    "Source Cell Range",
    "Source Excerpt",
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
ACTIONS_HEADERS = [
    "Action ID",
    "Requirement ID",
    "Title",
    "Description",
    "Owner",
    "Due Date",
    "Status",
]
SHEETS = (
    ("Requirements", REQUIREMENTS_HEADERS,
     [16, 16, 70, 11, 18, 11, 9, 28, 10, 22, 14, 14, 14, 60]),
    ("Compliance Matrix", COMPLIANCE_HEADERS,
     [16, 70, 11, 20, 45, 45, 30, 18, 20, 20, 40]),
    ("Actions", ACTIONS_HEADERS, [16, 16, 35, 60, 20, 14, 14]),
)


def _mandatory(value):
    return NOT_PROVIDED if value is None else to_text(value)


def _source_document(source):
    document_id = to_text(source.get("documentId"))
    filename = to_text(source.get("filename"))
    if document_id and filename:
        return f"{filename} ({document_id})"
    return filename or document_id


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
        requirement.get("category"),
        requirement.get("confidence"),
        len(sources),
        _join_per_source(sources, _source_document),
        _join_per_source(sources, lambda s: s.get("pageNo")),
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


def _compliance_row(requirement):
    evidence_match = requirement.get("evidenceMatch")
    decision = requirement.get("decision") or {}
    status = decision_status(requirement)
    return [
        requirement.get("requirementId") or requirement.get("sourceItemKey"),
        requirement.get("content"),
        _mandatory(requirement.get("mandatory")),
        (evidence_match or {}).get("status") or NOT_PROVIDED,
        (evidence_match or {}).get("note"),
        _evidence_references(evidence_match),
        _ai_recommendation(requirement.get("aiRecommendation")),
        status or NO_DECISION,
        decision.get("decidedBy"),
        decision.get("decidedAt"),
        decision.get("comment"),
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


def _write_row(sheet, row_index, values):
    for column, value in enumerate(values, start=1):
        cell = sheet.cell(row=row_index, column=column, value=excel_text(value))
        cell.data_type = "s"


def _style(sheet, widths):
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1F4E78")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width


def render_excel(snapshot):
    """Render the snapshot to .xlsx bytes."""
    normalized = normalize_snapshot(snapshot)
    rows_by_sheet = {
        "Requirements": [_requirement_row(r) for r in normalized["requirements"]],
        "Compliance Matrix": [
            _compliance_row(r) for r in normalized["requirements"]
        ],
        "Actions": [_action_row(a) for a in normalized["actions"]],
    }

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, headers, widths in SHEETS:
        sheet = workbook.create_sheet(title)
        _write_row(sheet, 1, headers)
        for row_index, values in enumerate(rows_by_sheet[title], start=2):
            _write_row(sheet, row_index, values)
        _style(sheet, widths)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
