"""
DOCX text extractor — Linux-compatible replacement.

The original implementation used win32com to convert DOCX → PDF via
Microsoft Word on Windows and then reused the PDF extractor for
accurate page numbers. On Linux we extract text directly from the DOCX
using python-docx, assigning page breaks to synthetic page boundaries.
"""

from docx import Document


def extract_docx_text(docx_path):
    """
    Extract text from a DOCX file, returning a list of page dicts that
    match the format produced by extract_pdf_text:

        [{"page": <int>, "text": <str>}, ...]

    Page boundaries are inferred from explicit page-break runs inside
    the document; if none are present, the whole document is returned
    as a single page.
    """
    doc = Document(docx_path)
    pages = []
    current_page = 1
    current_lines = []

    for para in doc.paragraphs:
        # Check each run for a page-break character.
        has_page_break = any(
            "\f" in (run.text or "") or run._r.xml.find("w:lastRenderedPageBreak") != -1
            for run in para.runs
        )

        text = para.text.strip()
        if text:
            current_lines.append(text)

        if has_page_break:
            pages.append({
                "page": current_page,
                "text": "\n".join(current_lines),
            })
            current_page += 1
            current_lines = []

    # Flush remaining text as the last page.
    if current_lines:
        pages.append({
            "page": current_page,
            "text": "\n".join(current_lines),
        })

    # Guarantee at least one page entry even for empty documents.
    if not pages:
        pages.append({"page": 1, "text": ""})

    return pages
