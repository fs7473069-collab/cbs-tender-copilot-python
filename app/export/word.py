"""Word export: tender information plus approved draft sections only."""

import re
from io import BytesIO

from docx import Document

from app.export.snapshot import is_approved_section, normalize_snapshot
from app.export.text import to_text

NO_APPROVED_SECTIONS_TEXT = (
    "No approved draft sections are available for this export."
)
UNTITLED_SECTION = "(Untitled section)"
TENDER_FIELDS = (
    ("title", "Title"),
    ("customerName", "Customer"),
    ("tenderReference", "Reference"),
    ("submissionDeadline", "Submission deadline"),
)
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")


def approved_sections(sections):
    """APPROVED sections ordered by explicit ``order``, then input order."""
    indexed = [
        (index, section)
        for index, section in enumerate(sections)
        if is_approved_section(section)
    ]
    indexed.sort(
        key=lambda pair: (
            pair[1].get("order") is None,
            pair[1].get("order") if pair[1].get("order") is not None else 0,
            pair[0],
        )
    )
    return [section for _, section in indexed]


def _paragraphs(content):
    text = to_text(content).replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in _BLANK_LINE_RE.split(text) if part.strip()]


def render_word(snapshot):
    """Render the snapshot to .docx bytes. Content is treated as plain text."""
    normalized = normalize_snapshot(snapshot)
    tender = normalized["tender"]

    document = Document()
    document.add_heading(to_text(tender.get("title")) or "Tender Export", level=0)

    tender_lines = [
        f"{label}: {to_text(tender.get(field))}"
        for field, label in TENDER_FIELDS
        if to_text(tender.get(field)).strip()
    ]
    if tender_lines:
        document.add_heading("Tender Information", level=1)
        for line in tender_lines:
            document.add_paragraph(line)

    document.add_heading("Approved Draft Sections", level=1)
    sections = approved_sections(normalized["draftSections"])
    if not sections:
        document.add_paragraph(NO_APPROVED_SECTIONS_TEXT)
    for section in sections:
        document.add_heading(
            to_text(section.get("title")).strip() or UNTITLED_SECTION, level=2
        )
        for paragraph in _paragraphs(section.get("content")):
            document.add_paragraph(paragraph)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
