"""DOCX extraction with approximate page and table provenance."""

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def _has_page_break(paragraph):
    return any(
        "w:lastRenderedPageBreak" in run._r.xml
        or 'w:type="page"' in run._r.xml
        or "\f" in (run.text or "")
        for run in paragraph.runs
    )


def extract_docx_text(docx_path):
    """Extract content in order; DOCX page boundaries are approximate."""
    try:
        document = Document(docx_path)
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"Unable to open DOCX '{docx_path}': {exc}") from exc

    pages, lines, tables = [], [], []
    page_number = 1

    def flush_page():
        nonlocal page_number, lines, tables
        if lines or tables:
            pages.append({
                "page": page_number,
                "text": "\n\n".join(lines).strip(),
                "tables": tables,
                "page_reference_type": "approximate_docx_boundary",
            })
        page_number += 1
        lines, tables = [], []

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            if paragraph.text.strip():
                lines.append(paragraph.text.strip())
            if _has_page_break(paragraph):
                flush_page()
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            rows = [[" ".join(cell.text.split()) for cell in row.cells] for row in table.rows]
            rows = [row for row in rows if any(row)]
            if rows:
                tables.append({"rows": rows, "source_type": "docx_table"})

    if lines or tables or not pages:
        pages.append({
            "page": page_number,
            "text": "\n\n".join(lines).strip(),
            "tables": tables,
            "page_reference_type": "approximate_docx_boundary",
        })
    return pages
