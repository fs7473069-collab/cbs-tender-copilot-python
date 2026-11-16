import json
import os
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from app.export.excel import render_excel
from app.export.local_export import snapshot_from_completed_response
from app.export.word import render_word


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_INPUT_FILE = (
    PROJECT_ROOT
    / "chunks"
    / "requirements"
    / "output"
    / "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)
INPUT_FILE = DEFAULT_INPUT_FILE

OUTPUT_DIR = PROJECT_ROOT / "local_data" / "reports"
OUTPUT_FILE = OUTPUT_DIR / "tender_analysis_local.xlsx"
WORD_OUTPUT_FILE = OUTPUT_DIR / "tender_analysis_local.docx"
DEMO_DIR = OUTPUT_DIR / "demo"
DEMO_EXCEL_FILE = DEMO_DIR / "tender-copilot-demo.xlsx"
DEMO_WORD_FILE = DEMO_DIR / "tender-copilot-demo.docx"
DEMO_RESPONSE_FILE = PROJECT_ROOT / "local_data" / "analysis" / "actual_response.json"
DEMO_ANALYSIS_FILE = (
    PROJECT_ROOT
    / "chunks"
    / "requirements"
    / "output"
    / "00eb20ca98fbc77e204ff91366a539ca7f84196dbd1d3a92abac2c6b9b67fcd6.json"
)


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


def snapshot_from_analysis_records(records, tender=None):
    """Map a completed requirements list to the export snapshot. No LLM calls."""
    requirements = []
    for index, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            raise ValueError("Expected each requirement to be an object.")
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        evidence = item.get("evidence_match")
        requirement_id = item.get("sourceItemKey") or f"REQ-{index:03d}"
        requirements.append({
            "requirementId": requirement_id,
            "sourceItemKey": requirement_id,
            "content": item.get("text") or item.get("content"),
            "mandatory": item.get("mandatory"),
            "category": item.get("category"),
            "confidence": item.get("confidence"),
            "sources": [{
                "documentId": source.get("documentId"),
                "filename": source.get("filename"),
                "pageNo": source.get("page"),
                "section": source.get("section"),
                "clause": source.get("clause"),
                "sheet": source.get("sheet"),
                "cellRange": source.get("cellRange"),
                "excerpt": source.get("excerpt"),
                "page_reference_type": source.get("page_reference_type"),
            }],
            "evidenceMatch": evidence if isinstance(evidence, dict) else None,
        })
    return {
        "tender": dict(tender or {}),
        "requirements": requirements,
        "actions": [],
        "draftSections": [],
        "recommendations": [],
    }


def _overlay_source_fields(snapshot, records):
    """Copy category and page-reference type from the analysis list when present."""
    requirements = snapshot.get("requirements") or []
    for requirement, record in zip(requirements, records or []):
        if not isinstance(record, dict):
            continue
        if requirement.get("category") in (None, "") and record.get("category"):
            requirement["category"] = record.get("category")
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        reference = source.get("page_reference_type")
        for item in requirement.get("sources") or []:
            if not isinstance(item, dict):
                continue
            if reference and not item.get("page_reference_type") and not item.get("pageReferenceType"):
                item["page_reference_type"] = reference
            if source.get("excerpt") and not item.get("excerpt"):
                item["excerpt"] = source.get("excerpt")
            if source.get("filename") and not item.get("filename"):
                item["filename"] = source.get("filename")
    return snapshot


def _write_export(snapshot, excel_path, word_path):
    excel_path = Path(excel_path)
    word_path = Path(word_path)
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    word_path.parent.mkdir(parents=True, exist_ok=True)
    excel_path.write_bytes(render_excel(snapshot))
    word_path.write_bytes(render_word(snapshot))
    return excel_path, word_path


def generate_local_excel(requirements_path=None, output_file=None):
    """Build Excel from one completed analysis JSON. Never re-extracts or rematches."""
    source_path, requirements = load_completed_requirements(requirements_path)
    out = Path(output_file) if output_file else OUTPUT_FILE
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_from_analysis_records(requirements)
    out.write_bytes(render_excel(snapshot))
    check = load_workbook(out, read_only=True, data_only=True)
    try:
        requirement_rows = check["Requirements"].max_row - 1
        if requirement_rows != len(requirements):
            raise ValueError("Requirement count verification failed.")
        print("Local Excel report created and verified.")
        print("Source analysis:", source_path.resolve())
        print("Requirements:", requirement_rows)
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
    snapshot = snapshot_from_analysis_records(requirements)
    out.write_bytes(render_word(snapshot))
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


def generate_demo_reports(excel_path=None, word_path=None):
    """Write demo copies from completed analysis. Does not overwrite local originals."""
    if not DEMO_RESPONSE_FILE.exists():
        raise FileNotFoundError(f"Completed analysis response not found: {DEMO_RESPONSE_FILE}")
    response = json.loads(DEMO_RESPONSE_FILE.read_text(encoding="utf-8"))
    snapshot = snapshot_from_completed_response(response)
    if DEMO_ANALYSIS_FILE.exists():
        records = json.loads(DEMO_ANALYSIS_FILE.read_text(encoding="utf-8"))
        if isinstance(records, list):
            _overlay_source_fields(snapshot, records)
    excel_out = Path(excel_path) if excel_path else DEMO_EXCEL_FILE
    word_out = Path(word_path) if word_path else DEMO_WORD_FILE
    return _write_export(snapshot, excel_out, word_out)


if __name__ == "__main__":
    generate_demo_reports()
