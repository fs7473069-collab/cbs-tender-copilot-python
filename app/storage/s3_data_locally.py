import json
import os
from pathlib import Path

from docx import Document
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_INPUT_FILE = (
    PROJECT_ROOT
    / "chunks"
    / "requirements"
    / "output"
    / "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)
# Backward-compatible alias
INPUT_FILE = DEFAULT_INPUT_FILE

OUTPUT_DIR = PROJECT_ROOT / "local_data" / "reports"
OUTPUT_FILE = OUTPUT_DIR / "tender_analysis_local.xlsx"
WORD_OUTPUT_FILE = OUTPUT_DIR / "tender_analysis_local.docx"


def resolve_analysis_json(requirements_path=None):
    """Resolve the single completed-analysis JSON used for Excel and Word."""
    if requirements_path:
        path = Path(requirements_path)
    else:
        env_path = os.getenv("ANALYSIS_JSON", "").strip()
        path = Path(env_path) if env_path else DEFAULT_INPUT_FILE
    if not path.exists():
        raise FileNotFoundError(f"Analysis JSON not found: {path}")
    return path


def load_completed_requirements(requirements_path=None):
    path = resolve_analysis_json(requirements_path)
    with path.open("r", encoding="utf-8") as file:
        requirements = json.load(file)
    if not isinstance(requirements, list):
        raise ValueError("Expected a list of requirements.")
    return path, requirements


def generate_local_excel(requirements_path=None, output_file=None):
    """Build Excel from one completed analysis JSON. Never re-extracts or rematches."""
    source_path, requirements = load_completed_requirements(requirements_path)
    out = Path(output_file) if output_file else OUTPUT_FILE
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    # Sheet 1: Requirements and evidence status
    ws = workbook.active
    ws.title = "Requirements"
    ws.append([
        "Requirement ID",
        "Requirement",
        "Category",
        "Mandatory",
        "Confidence",
        "Source Page",
        "Source Chunk",
        "RAG Status",
        "Accepted Evidence Count",
        "Evidence Note",
    ])

    # Sheet 2: Compliance review
    compliance_ws = workbook.create_sheet("Compliance Matrix")
    compliance_ws.append([
        "Requirement ID",
        "Requirement",
        "Mandatory",
        "RAG Evidence Status",
        "Compliance Decision",
        "Reviewer Comments",
    ])

    # Sheet 3: Accepted evidence passages
    sources_ws = workbook.create_sheet("Sources")
    sources_ws.append([
        "Requirement ID",
        "Requirement",
        "Evidence Document",
        "Evidence Page",
        "Similarity",
        "Relevance Reason",
        "Evidence Text",
    ])

    for index, requirement in enumerate(requirements, start=1):
        source = requirement.get("source") or {}
        match = requirement.get("evidence_match") or {}
        evidence = match.get("evidence") or []

        requirement_id = f"REQ-{index:03d}"
        text = requirement.get("text", "")
        mandatory = requirement.get("mandatory", False)
        confidence = requirement.get("confidence", "")
        status = match.get("status", "NOT_CHECKED")

        ws.append([
            requirement_id,
            text,
            requirement.get("category", ""),
            mandatory,
            confidence,
            source.get("page", ""),
            source.get("chunk", ""),
            status,
            len(evidence),
            match.get("note", ""),
        ])

        # Evidence status is not itself a compliance decision.
        compliance_ws.append([
            requirement_id,
            text,
            mandatory,
            status,
            "Not assessed",
            "",
        ])

        for item in evidence:
            sources_ws.append([
                requirement_id,
                text,
                item.get("source", ""),
                item.get("page", ""),
                item.get("similarity", ""),
                item.get("relevance_reason", ""),
                item.get("text", ""),
            ])

    widths = {
        "Requirements": [16, 75, 18, 14, 16, 13, 13, 18, 22, 70],
        "Compliance Matrix": [16, 75, 14, 20, 22, 35],
        "Sources": [16, 65, 45, 13, 14, 65, 90],
    }

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.row_dimensions[1].height = 32

        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(
                fill_type="solid",
                fgColor="1F4E78",
            )
            cell.alignment = Alignment(
                vertical="center",
                wrap_text=True,
            )

        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=True,
                )

        for column, width in enumerate(widths[sheet.title], start=1):
            sheet.column_dimensions[get_column_letter(column)].width = width

        if sheet.max_row > 1:
            table = Table(
                displayName=sheet.title.replace(" ", "") + "Table",
                ref=sheet.dimensions,
            )
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2",
                showRowStripes=True,
                showColumnStripes=False,
            )
            sheet.add_table(table)

    workbook.save(out)

    # Reopen the file to verify it was saved correctly.
    check = load_workbook(out, read_only=True, data_only=True)
    try:
        requirement_rows = check["Requirements"].max_row - 1
        compliance_rows = check["Compliance Matrix"].max_row - 1
        evidence_rows = check["Sources"].max_row - 1

        if requirement_rows != len(requirements):
            raise ValueError("Requirement count verification failed.")

        if compliance_rows != len(requirements):
            raise ValueError("Compliance count verification failed.")

        print("Local Excel report created and verified.")
        print("Source analysis:", source_path.resolve())
        print("Requirements:", requirement_rows)
        print("Compliance rows:", compliance_rows)
        print("Accepted evidence rows:", evidence_rows)
        print("Sheets:", check.sheetnames)
        print("File:", out.resolve())
        print("Size:", f"{out.stat().st_size:,} bytes")
    finally:
        check.close()

    return out


def generate_local_word(requirements_path=None, output_file=None):
    """Build Word from the same completed analysis JSON as Excel. No LLM calls."""
    source_path, requirements = load_completed_requirements(requirements_path)
    out = Path(output_file) if output_file else WORD_OUTPUT_FILE
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    document = Document()
    document.add_heading("Tender Analysis Report", level=0)
    document.add_paragraph(
        f"Generated from completed analysis only (no re-extraction): {source_path.name}"
    )
    document.add_paragraph(f"Requirements: {len(requirements)}")

    document.add_heading("Requirements Summary", level=1)
    for index, requirement in enumerate(requirements, start=1):
        source = requirement.get("source") or {}
        match = requirement.get("evidence_match") or {}
        evidence = match.get("evidence") or []
        requirement_id = f"REQ-{index:03d}"
        document.add_heading(requirement_id, level=2)
        document.add_paragraph(requirement.get("text", ""))
        document.add_paragraph(
            f"Category: {requirement.get('category', '')} | "
            f"Mandatory: {requirement.get('mandatory', False)} | "
            f"Confidence: {requirement.get('confidence', '')}"
        )
        document.add_paragraph(
            f"Source page: {source.get('page', '')} | "
            f"chunk: {source.get('chunk', '')}"
        )
        document.add_paragraph(
            f"Evidence status: {match.get('status', 'NOT_CHECKED')} "
            f"({len(evidence)} accepted)"
        )
        note = match.get("note")
        if note:
            document.add_paragraph(f"Note: {note}")
        for item in evidence:
            document.add_paragraph(
                f"Evidence ({item.get('source', '')}, p.{item.get('page', '')}, "
                f"sim={item.get('similarity', '')}): {item.get('text', '')}"
            )

    document.save(out)
    print("Local Word report created.")
    print("Source analysis:", source_path.resolve())
    print("Requirements:", len(requirements))
    print("File:", out.resolve())
    print("Size:", f"{out.stat().st_size:,} bytes")
    return out


def generate_local_reports(requirements_path=None):
    """Excel + Word from one completed analysis result."""
    excel_path = generate_local_excel(requirements_path=requirements_path)
    word_path = generate_local_word(requirements_path=requirements_path)
    return excel_path, word_path


if __name__ == "__main__":
    generate_local_reports()
