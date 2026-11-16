"""Word export: structured tender response from an existing snapshot only."""

from collections import Counter
from datetime import datetime
from io import BytesIO

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor, Twips

from app.export.snapshot import (
    ACTUAL_PDF_PAGE,
    DRAFT_PENDING_REVIEW_LABEL,
    MISSING_DRAFT_SECTION,
    decision_status,
    draft_generated_content,
    draft_missing_information_text,
    draft_requirement_ids_text,
    draft_review_status_label,
    draft_source_references_text,
    is_approved_section,
    missing_expected_draft_specs,
    normalize_snapshot,
    pending_review_sections,
    source_validation_banner,
)
from app.export.text import to_text
from app.export.word_pagination import (
    TOC_PAGE_UNRESOLVED,
    locate_pages_with_libreoffice,
    should_paginate,
)

NO_APPROVED_SECTIONS_TEXT = (
    "Draft response sections are pending human review and approval. "
    "No approved draft sections are available for this export. "
    "This document does not invent company experience or unapproved content."
)
PENDING_DRAFTS_HEADING = "AI-Generated Draft Responses — Pending Review"
PENDING_DRAFTS_INTRO = (
    "The following AI-generated sections are unapproved working drafts. "
    "They are not part of the approved final bid response. "
    "Reviewers must verify content, sources, and missing-information notes "
    "before approval."
)
INFORMATION_REQUIRED = "Information Required"
UNTITLED_SECTION = "(Untitled section)"
NOT_PROVIDED = "Not provided"
UNCLASSIFIED = "Unclassified"
PENDING_REVIEW = "Pending Review"
DOCUMENT_KIND_RESPONSE = "Tender Response"
DOCUMENT_KIND_ANALYSIS = "Tender Analysis Report"
EXCERPT_QUOTE_LIMIT = 280
AI_EVIDENCE_CANDIDATE_LABEL = (
    "AI-retrieved candidate evidence. These passages are not confirmed, "
    "approved, or a human compliance decision. REVIEW_REQUIRED means a "
    "reviewer must verify the match."
)
TENDER_FIELDS = (
    ("customerName", "Customer"),
    ("tenderReference", "Reference"),
    ("submissionDeadline", "Deadline"),
)
NAVY = RGBColor(0x1F, 0x4E, 0x78)
NAVY_HEX = "1F4E78"
NAVY_PALE_HEX = "EAF1F8"
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
AMBER = RGBColor(0x7D, 0x66, 0x08)
FONT_NAME = "Calibri"
A4_WIDTH = Cm(21.0)
A4_HEIGHT = Cm(29.7)

NARRATIVE_HEADINGS = (
    "Executive Summary",
    "Understanding of Requirements",
    "Proposed Technical Approach",
    "Implementation Methodology",
    "Company Experience and Capabilities",
    "Conclusion",
)
COMPLIANCE_HEADERS = (
    "Requirement ID",
    "Requirement",
    "Mandatory",
    "Category",
    "AI Evidence Status",
    "Human Decision",
    "Source",
)
COMPLIANCE_WIDTHS_CM = (2.6, 8.8, 2.0, 2.8, 3.4, 3.3, 3.6)
APPENDIX_REQ_WIDTHS_CM = (5.0, 8.5, 13.0)
EVIDENCE_WIDTHS_CM = (2.4, 4.4, 1.6, 2.2, 3.4, 12.5)
INFO_WIDTHS_CM = (4.4, 12.2)
INFO_WIDTHS_LANDSCAPE_CM = (6.0, 20.5)
CATEGORY_WIDTHS_CM = (10.6, 6.0)
ACTION_WIDTHS_CM = (2.2, 2.4, 4.4, 2.4, 2.6, 2.6)
TOC_WIDTHS_CM = (14.6, 2.0)


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


def collapse_cell_text(value):
    """Flatten PDF/chunk newlines so table cells wrap as ordinary prose."""
    return " ".join(to_text(value).split())


def truncate_quote(value, limit=EXCERPT_QUOTE_LIMIT):
    """Short display quote; does not alter the snapshot."""
    text = collapse_cell_text(value)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rsplit(" ", 1)[0]
    if not trimmed:
        trimmed = text[:limit]
    return trimmed + "…"


def format_display_date(value):
    """Format ISO dates for display; leave unparseable values unchanged."""
    text = to_text(value).strip()
    if not text:
        return NOT_PROVIDED
    parsed = _parse_iso_datetime(text)
    if parsed is None:
        return text
    month = parsed.strftime("%B")
    if _source_includes_time(text):
        suffix = " UTC" if text.rstrip().endswith("Z") or text.endswith("+00:00") else ""
        return f"{parsed.day} {month} {parsed.year}, {parsed.strftime('%H:%M')}{suffix}"
    return f"{parsed.day} {month} {parsed.year}"


def toc_entries_for_snapshot(snapshot):
    """TOC section titles in document order. Page numbers are not estimated."""
    normalized = normalize_snapshot(snapshot)
    _, unmatched = _partition_drafts(normalized["draftSections"])
    pending = pending_review_sections(normalized["draftSections"])
    missing = (
        missing_expected_draft_specs(normalized["draftSections"])
        if normalized["draftSections"]
        else []
    )
    return _toc_titles(unmatched, include_pending=bool(pending or missing))


def _parse_iso_datetime(text):
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _source_includes_time(text):
    stripped = text.strip()
    if "T" in stripped:
        return True
    if len(stripped) > 10 and ":" in stripped[10:]:
        return True
    return False


def _toc_titles(unmatched, include_pending=False):
    titles = [
        "Cover Page",
        "Executive Summary",
        "Understanding of Requirements",
        "Proposed Technical Approach",
        "Implementation Methodology",
        "Company Experience and Capabilities",
        "Requirement-by-Requirement Compliance Response",
        "Assumptions and Information Required",
        "Conclusion",
    ]
    if unmatched:
        titles.append("Approved Draft Sections")
    if include_pending:
        titles.append(PENDING_DRAFTS_HEADING)
    titles.extend([
        "Appendix A — Extracted Requirements Summary",
        "Appendix B — Evidence References",
    ])
    return titles


def apply_toc_pages(docx_bytes, pages_by_title):
    """Patch the static TOC page column in an already rendered document."""
    document = Document(BytesIO(docx_bytes))
    for table in document.tables:
        if not table.rows or table.rows[0].cells[0].text != "Section":
            continue
        for row in table.rows[1:]:
            title = row.cells[0].text.strip()
            if title not in (pages_by_title or {}):
                continue
            page = pages_by_title[title]
            label = str(page) if page is not None else TOC_PAGE_UNRESOLVED
            _set_cell_text(row.cells[1], label, size=11)
        break
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _set_run_font(run, *, bold=False, size=11, color=None, name=FONT_NAME):
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = name
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    r_fonts.set(qn("w:ascii"), name)
    r_fonts.set(qn("w:hAnsi"), name)
    if color is not None:
        run.font.color.rgb = color


def _shade_cell(cell, fill_hex):
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), fill_hex)
    shading.set(qn("w:val"), "clear")
    cell._tc.get_or_add_tcPr().append(shading)


def _set_cell_border(cell, color="D0D7DE", size="4"):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    existing = tc_pr.find(qn("w:tcBorders"))
    if existing is not None:
        tc_pr.remove(existing)
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:color"), color)
        borders.append(element)
    tc_pr.append(borders)


def _set_cell_margins(cell, twips=80):
    tc_pr = cell._tc.get_or_add_tcPr()
    existing = tc_pr.find(qn("w:tcMar"))
    if existing is not None:
        tc_pr.remove(existing)
    margins = OxmlElement("w:tcMar")
    for edge in ("top", "left", "bottom", "right"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:w"), str(twips))
        node.set(qn("w:type"), "dxa")
        margins.append(node)
    tc_pr.append(margins)


def _set_cell_width(cell, width_cm):
    tc_pr = cell._tc.get_or_add_tcPr()
    existing = tc_pr.find(qn("w:tcW"))
    if existing is not None:
        tc_pr.remove(existing)
    width = OxmlElement("w:tcW")
    width.set(qn("w:w"), str(int(Cm(width_cm).twips)))
    width.set(qn("w:type"), "dxa")
    tc_pr.append(width)
    no_wrap = tc_pr.find(qn("w:noWrap"))
    if no_wrap is not None:
        tc_pr.remove(no_wrap)


def _set_cell_text(cell, text, *, bold=False, color=None, size=9, fill=None, align=None):
    cell.text = ""
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.08
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    if align is not None:
        paragraph.alignment = align
    run = paragraph.add_run(collapse_cell_text(text))
    _set_run_font(run, bold=bold, size=size, color=color)
    _set_cell_border(cell)
    _set_cell_margins(cell)
    if fill:
        _shade_cell(cell, fill)


def _mark_header_row(row):
    tr = row._tr
    tr_pr = tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def _set_table_layout(table, widths_cm):
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.allow_autofit = False
    tbl = table._tbl
    tbl_pr = tbl.tblPr
    if tbl_pr is None:
        tbl_pr = OxmlElement("w:tblPr")
        tbl.insert(0, tbl_pr)
    existing_layout = tbl_pr.find(qn("w:tblLayout"))
    if existing_layout is not None:
        tbl_pr.remove(existing_layout)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    total = Twips(sum(int(Cm(width).twips) for width in widths_cm))
    existing_width = tbl_pr.find(qn("w:tblW"))
    if existing_width is None:
        existing_width = OxmlElement("w:tblW")
        tbl_pr.append(existing_width)
    existing_width.set(qn("w:w"), str(int(total.twips)))
    existing_width.set(qn("w:type"), "dxa")
    existing_grid = tbl.find(qn("w:tblGrid"))
    if existing_grid is not None:
        tbl.remove(existing_grid)
    grid = OxmlElement("w:tblGrid")
    for width in widths_cm:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(int(Cm(width).twips)))
        grid.append(col)
    tbl.insert(1, grid)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            _set_cell_width(cell, widths_cm[index])


def _add_page_field(paragraph):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(separate)
    run._r.append(end)
    _set_run_font(run, size=9, color=NAVY)


def _enable_update_fields(document):
    settings = document.settings.element
    for child in settings.findall(qn("w:updateFields")):
        settings.remove(child)
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)


def _set_bottom_border(paragraph, color=NAVY_HEX, size="12"):
    p_pr = paragraph._p.get_or_add_pPr()
    existing = p_pr.find(qn("w:pBdr"))
    if existing is not None:
        p_pr.remove(existing)
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), size)
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def _set_page_number_start(section, start=None):
    sect_pr = section._sectPr
    for child in sect_pr.findall(qn("w:pgNumType")):
        sect_pr.remove(child)
    if start is None:
        return
    node = OxmlElement("w:pgNumType")
    node.set(qn("w:start"), str(start))
    sect_pr.append(node)


def _apply_page_size(section, *, landscape):
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = A4_HEIGHT
        section.page_height = A4_WIDTH
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.6)
        section.right_margin = Cm(1.6)
    else:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = A4_WIDTH
        section.page_height = A4_HEIGHT
        section.top_margin = Cm(1.8)
        section.bottom_margin = Cm(1.8)
        section.left_margin = Cm(2.2)
        section.right_margin = Cm(2.2)


def _configure_section(
    section, *, cover_section=False, landscape=False, kind=DOCUMENT_KIND_ANALYSIS
):
    _apply_page_size(section, landscape=landscape)
    section.different_first_page_header_footer = cover_section
    header = section.header
    footer = section.footer
    header.is_linked_to_previous = False
    footer.is_linked_to_previous = False
    header_para = header.paragraphs[0]
    header_para.text = ""
    header_run = header_para.add_run(f"CBS  |  {kind}  |  Confidential")
    _set_run_font(header_run, size=9, color=NAVY, bold=True)
    _set_bottom_border(header_para, size="8")
    footer_para = footer.paragraphs[0]
    footer_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer_para.text = ""
    footer_run = footer_para.add_run("CBS Confidential  |  Page ")
    _set_run_font(footer_run, size=9, color=NAVY)
    _add_page_field(footer_para)
    if cover_section:
        first_header = section.first_page_header
        first_footer = section.first_page_footer
        first_header.is_linked_to_previous = False
        first_footer.is_linked_to_previous = False
        first_header.paragraphs[0].text = ""
        first_footer.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
        first_footer.paragraphs[0].text = ""
        first_run = first_footer.paragraphs[0].add_run(
            "CBS  |  Confidential  |  Not for distribution"
        )
        _set_run_font(first_run, size=9, color=NAVY)
        _set_page_number_start(section, 1)
    else:
        _set_page_number_start(section, None)


def _add_oriented_section(document, *, landscape, kind=DOCUMENT_KIND_ANALYSIS):
    section = document.add_section(WD_SECTION.NEW_PAGE)
    _configure_section(
        section, cover_section=False, landscape=landscape, kind=kind
    )
    return section


def _add_heading(document, text, level):
    heading = document.add_heading(text, level=level)
    for run in heading.runs:
        run.font.color.rgb = NAVY
        run.font.name = FONT_NAME
    heading.paragraph_format.space_before = Pt(8 if level == 1 else 6)
    heading.paragraph_format.space_after = Pt(6)
    heading.paragraph_format.keep_with_next = True
    heading.paragraph_format.keep_together = True
    return heading


def _add_style_label(document, text, *, size=11, bold=True, space_after=4, space_before=0):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(text)
    _set_run_font(run, size=size, bold=bold, color=NAVY)
    return paragraph


def _add_body(
    document,
    text,
    *,
    italic=False,
    bold=False,
    color=None,
    size=11,
    space_after=6,
    keep_together=True,
):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.12
    paragraph.paragraph_format.keep_together = keep_together
    run = paragraph.add_run(to_text(text))
    _set_run_font(run, size=size, bold=bold, color=color)
    run.italic = italic
    return paragraph


def _display(value, *, date=False):
    if date:
        return format_display_date(value)
    text = to_text(value).strip()
    return text if text else NOT_PROVIDED


def _category(value):
    text = to_text(value).strip()
    return text if text else UNCLASSIFIED


def _decision_label(requirement):
    status = decision_status(requirement)
    return status if status else PENDING_REVIEW


def _mandatory_label(value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return NOT_PROVIDED


def _source_file(source):
    return to_text(source.get("filename")).strip()


def _page_number(source):
    page = to_text(source.get("pageNo")).strip()
    if not page:
        return ""
    reference = source.get("pageReferenceType") or source.get("page_reference_type")
    if (
        isinstance(reference, str)
        and reference.strip()
        and reference.strip() != ACTUAL_PDF_PAGE
    ):
        return f"{page} (unverified)"
    return page


def _source_citation(source):
    parts = []
    filename = _source_file(source)
    if filename:
        parts.append(filename)
    page = _page_number(source)
    if page:
        parts.append(f"p.{page}")
    clause = to_text(source.get("clause")).strip()
    if clause:
        parts.append(f"cl. {clause}")
    section = to_text(source.get("section")).strip()
    if section:
        parts.append(section)
    return ", ".join(parts)


def _add_info_table(document, rows, widths=INFO_WIDTHS_CM):
    table = document.add_table(rows=len(rows), cols=2)
    _set_table_layout(table, widths)
    for index, (label, value) in enumerate(rows):
        _set_cell_text(
            table.rows[index].cells[0], label, bold=True, color=NAVY, fill=NAVY_PALE_HEX
        )
        _set_cell_text(table.rows[index].cells[1], value)
    return table


def _add_data_table(document, headers, rows, widths, column_sizes=None):
    table = document.add_table(rows=1 + len(rows), cols=len(headers))
    _set_table_layout(table, widths)
    sizes = column_sizes or [9] * len(headers)
    header_row = table.rows[0]
    _mark_header_row(header_row)
    for index, header in enumerate(headers):
        _set_cell_text(
            header_row.cells[index], header, bold=True, color=WHITE, fill=NAVY_HEX, size=9
        )
    for row_index, values in enumerate(rows, start=1):
        row = table.rows[row_index]
        fill = NAVY_PALE_HEX if row_index % 2 == 0 else None
        for column, value in enumerate(values):
            _set_cell_text(
                row.cells[column],
                value,
                fill=fill,
                size=sizes[column] if column < len(sizes) else 9,
            )
    return table


def _paragraphs(content):
    text = to_text(content).replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in text.split("\n\n") if part.strip()]


def _normalise_title(title):
    cleaned = (
        to_text(title)
        .replace("—", " ")
        .replace("–", " ")
        .replace(":", " ")
    )
    return " ".join(cleaned.split()).casefold()


def matched_chapter(title):
    """Return the narrative chapter a draft title belongs to, if any."""
    key = _normalise_title(title)
    if not key:
        return None
    for name in NARRATIVE_HEADINGS:
        target = _normalise_title(name)
        if key == target or key.startswith(target + " "):
            return name
    return None


def _partition_drafts(sections):
    matched = {name: [] for name in NARRATIVE_HEADINGS}
    unmatched = []
    for section in approved_sections(sections):
        chapter = matched_chapter(section.get("title"))
        if chapter:
            matched[chapter].append(section)
        else:
            unmatched.append(section)
    return matched, unmatched


def _draft_trace_line(section):
    """Source line from snapshot fields only; never invents references."""
    parts = []
    ids = [
        to_text(item).strip()
        for item in (section.get("sourceRequirementIds") or [])
        if to_text(item).strip()
    ]
    if ids:
        parts.append("Requirements: " + ", ".join(ids))
    citations = []
    for source in section.get("sources") or []:
        if not isinstance(source, dict):
            continue
        filename = to_text(source.get("filename")).strip()
        page = source.get("page")
        clause = to_text(source.get("clause")).strip()
        bit = filename or ""
        if page is not None and to_text(page).strip():
            bit = f"{bit}, p.{to_text(page).strip()}" if bit else f"p.{to_text(page).strip()}"
        if clause:
            bit = f"{bit}, clause {clause}" if bit else f"clause {clause}"
        if bit:
            citations.append(bit)
    if citations:
        parts.append("Sources: " + "; ".join(citations))
    gaps = [
        to_text(item).strip()
        for item in (section.get("missingInformation") or [])
        if to_text(item).strip()
    ]
    if gaps:
        parts.append("Information Required: " + "; ".join(gaps))
    return " ".join(parts)


def _write_approved_content(document, sections):
    for section in sections:
        title = to_text(section.get("title")).strip()
        paragraphs = _paragraphs(section.get("content"))
        if title and matched_chapter(title) is None:
            _add_heading(document, title or UNTITLED_SECTION, 2)
        if not paragraphs:
            _add_body(document, INFORMATION_REQUIRED, italic=True, color=AMBER)
            continue
        for paragraph in paragraphs:
            _add_body(document, paragraph)
        trace = _draft_trace_line(section)
        if trace:
            _add_body(document, trace, italic=True, size=10, color=NAVY)


def _write_narrative_chapter(document, heading, sections, extra_paragraphs=None):
    _add_heading(document, heading, 1)
    for paragraph in extra_paragraphs or []:
        _add_body(document, paragraph)
    if sections:
        _write_approved_content(document, sections)
        return
    _add_body(document, INFORMATION_REQUIRED, italic=True, color=AMBER, space_after=8)


def _write_pending_draft_section(document, section):
    title = to_text(section.get("title")).strip() or UNTITLED_SECTION
    _add_heading(document, title, 2)
    _add_body(
        document,
        draft_review_status_label(section),
        bold=True,
        color=AMBER,
        size=10,
    )
    content = draft_generated_content(section)
    for paragraph in _paragraphs(content) or [content]:
        _add_body(document, paragraph, keep_together=False)
    requirement_ids = draft_requirement_ids_text(section)
    if requirement_ids:
        _add_body(
            document,
            "Requirements: " + requirement_ids,
            italic=True,
            size=10,
            color=NAVY,
        )
    sources = draft_source_references_text(section)
    if sources:
        _add_body(document, "Sources: " + sources, italic=True, size=10, color=NAVY)
    missing = draft_missing_information_text(section)
    if missing:
        _add_body(
            document,
            "Information Required: " + missing,
            italic=True,
            size=10,
            color=AMBER,
        )


def _write_pending_drafts(document, sections):
    pending = pending_review_sections(sections)
    missing = missing_expected_draft_specs(sections) if sections else []
    if not pending and not missing:
        return
    document.add_page_break()
    _add_heading(document, PENDING_DRAFTS_HEADING, 1)
    _add_body(document, PENDING_DRAFTS_INTRO, italic=True, size=10, color=AMBER)
    for section in pending:
        _write_pending_draft_section(document, section)
    for section_type, title, _order in missing:
        _add_heading(document, title, 2)
        _add_body(
            document,
            DRAFT_PENDING_REVIEW_LABEL,
            bold=True,
            color=AMBER,
            size=10,
        )
        _add_body(
            document,
            f"{MISSING_DRAFT_SECTION} ({section_type})",
            italic=True,
            color=AMBER,
        )


def _add_cover_banner(document, kind):
    table = document.add_table(rows=1, cols=2)
    _set_table_layout(table, (8.3, 8.3))
    left, right = table.rows[0].cells
    _set_cell_text(
        left, "CBS", bold=True, color=WHITE, size=16, fill=NAVY_HEX
    )
    _set_cell_text(
        right,
        kind,
        bold=True,
        color=WHITE,
        size=12,
        fill=NAVY_HEX,
        align=WD_ALIGN_PARAGRAPH.RIGHT,
    )
    _set_cell_border(left, color=NAVY_HEX, size="8")
    _set_cell_border(right, color=NAVY_HEX, size="8")
    _set_cell_margins(left, twips=140)
    _set_cell_margins(right, twips=140)


def _cover(document, tender, banner, kind):
    title = to_text(tender.get("title")).strip() or "Tender Export"
    _add_cover_banner(document, kind)
    _add_style_label(
        document, kind.upper(), size=11, space_before=14, space_after=2
    )
    title_para = document.add_paragraph()
    title_para.paragraph_format.space_before = Pt(0)
    title_para.paragraph_format.space_after = Pt(8)
    title_para.paragraph_format.keep_with_next = True
    title_run = title_para.add_run(title)
    _set_run_font(title_run, size=22, bold=True, color=NAVY)
    _set_bottom_border(title_para, size="18")

    customer = _display(tender.get("customerName"))
    prepared = document.add_paragraph()
    prepared.paragraph_format.space_before = Pt(10)
    prepared.paragraph_format.space_after = Pt(8)
    label = prepared.add_run("Prepared for  ")
    _set_run_font(label, size=11, color=NAVY, bold=True)
    value = prepared.add_run(customer)
    _set_run_font(value, size=11)

    _add_info_table(document, [
        (label, _display(tender.get(field), date=(field == "submissionDeadline")))
        for field, label in TENDER_FIELDS
    ])
    _add_body(
        document,
        "Prepared from the analysis snapshot. Approved draft sections are included "
        "only when explicitly marked APPROVED. Company experience and commitments "
        "are not invented.",
        italic=True,
        size=10,
        space_after=8,
    )
    if banner:
        _add_body(document, banner, bold=True, color=AMBER, size=10)
    document.add_page_break()
    return title


def _contents(document, titles):
    heading = _add_style_label(
        document, "Table of Contents", size=16, space_before=0, space_after=6
    )
    _set_bottom_border(heading, size="12")
    _add_body(
        document,
        "This contents list is ordinary text so it remains visible in Microsoft "
        "Word and LibreOffice PDF conversion. Page numbers are copied from a "
        "LibreOffice layout of this file when that tool is available. Numbers "
        f"that cannot be verified are shown as {TOC_PAGE_UNRESOLVED}; they are "
        "not estimates.",
        italic=True,
        size=10,
        space_after=8,
    )
    rows = []
    for title in titles:
        page = "1" if title == "Cover Page" else TOC_PAGE_UNRESOLVED
        rows.append([title, page])
    _add_data_table(
        document,
        ["Section", "Page"],
        rows,
        TOC_WIDTHS_CM,
        column_sizes=[11, 11],
    )
    document.add_page_break()


def _requirement_rows(requirements):
    rows = []
    for item in requirements:
        sources = item.get("sources") or []
        citations = [_source_citation(source) for source in sources if _source_citation(source)]
        evidence = item.get("evidenceMatch") or {}
        rows.append([
            to_text(item.get("requirementId") or item.get("sourceItemKey")),
            to_text(item.get("content")),
            _mandatory_label(item.get("mandatory")),
            _category(item.get("category")),
            to_text(evidence.get("status")).strip() or NOT_PROVIDED,
            _decision_label(item),
            "; ".join(citations) if citations else INFORMATION_REQUIRED,
        ])
    return rows


def _similarity_label(passage):
    value = passage.get("similarity")
    if value is None:
        value = passage.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_PROVIDED
    return f"{round(float(value), 3):.3f}"


def _evidence_rows(requirements):
    rows = []
    for item in requirements:
        evidence = item.get("evidenceMatch") or {}
        requirement_id = to_text(item.get("requirementId") or item.get("sourceItemKey"))
        passages = evidence.get("evidence") or []
        if not passages:
            continue
        status = to_text(evidence.get("status")).strip() or NOT_PROVIDED
        for passage in passages:
            if not isinstance(passage, dict):
                continue
            source = to_text(passage.get("source")).strip() or INFORMATION_REQUIRED
            page = to_text(passage.get("page")).strip()
            quote = truncate_quote(passage.get("text")) or INFORMATION_REQUIRED
            rows.append([
                requirement_id,
                source,
                page or INFORMATION_REQUIRED,
                _similarity_label(passage),
                status,
                quote,
            ])
    return rows


def _appendix_requirement_rows(requirements):
    """One row per distinct source excerpt, listing every requirement that uses it."""
    groups = {}
    order = []
    for item in requirements:
        requirement_id = to_text(item.get("requirementId") or item.get("sourceItemKey"))
        sources = item.get("sources") or []
        if not sources:
            key = ""
            if key not in groups:
                groups[key] = {"ids": [], "citations": []}
                order.append(key)
            if requirement_id and requirement_id not in groups[key]["ids"]:
                groups[key]["ids"].append(requirement_id)
            continue
        for source in sources:
            excerpt = to_text(source.get("excerpt")).strip()
            citation = _source_citation(source)
            key = excerpt
            if key not in groups:
                groups[key] = {"ids": [], "citations": []}
                order.append(key)
            group = groups[key]
            if requirement_id and requirement_id not in group["ids"]:
                group["ids"].append(requirement_id)
            if citation and citation not in group["citations"]:
                group["citations"].append(citation)
    rows = []
    for excerpt in order:
        group = groups[excerpt]
        quote = truncate_quote(excerpt) if excerpt else INFORMATION_REQUIRED
        rows.append([
            ", ".join(group["ids"]) or INFORMATION_REQUIRED,
            "; ".join(group["citations"]) if group["citations"] else INFORMATION_REQUIRED,
            quote,
        ])
    return rows


def render_word(snapshot, *, paginator=None):
    """Render the snapshot to .docx bytes. Content is treated as plain text.

    ``paginator`` is ``None`` (auto LibreOffice when available), ``False``
    (leave unverified page numbers), or a callable ``(docx_bytes, titles) ->
    {title: page}``.
    """
    normalized = normalize_snapshot(snapshot)
    tender = normalized["tender"]
    requirements = normalized["requirements"]
    matched, unmatched = _partition_drafts(normalized["draftSections"])
    any_approved = bool(approved_sections(normalized["draftSections"]))
    pending = pending_review_sections(normalized["draftSections"])
    missing_drafts = (
        missing_expected_draft_specs(normalized["draftSections"])
        if normalized["draftSections"]
        else []
    )
    toc_titles = _toc_titles(
        unmatched, include_pending=bool(pending or missing_drafts)
    )
    kind = DOCUMENT_KIND_RESPONSE if any_approved else DOCUMENT_KIND_ANALYSIS

    document = Document()
    _enable_update_fields(document)
    for section in document.sections:
        _configure_section(
            section, cover_section=True, landscape=False, kind=kind
        )

    banner = source_validation_banner(normalized)
    _cover(document, tender, banner, kind)
    _contents(document, toc_titles)

    if not any_approved:
        _add_body(document, NO_APPROVED_SECTIONS_TEXT, italic=True, color=AMBER)

    mandatory = sum(1 for item in requirements if item.get("mandatory") is True)
    pending_decisions = sum(
        1 for item in requirements if _decision_label(item) == PENDING_REVIEW
    )
    evidence_counts = Counter(
        to_text((item.get("evidenceMatch") or {}).get("status")).strip() or NOT_PROVIDED
        for item in requirements
    )
    decision_counts = Counter(_decision_label(item) for item in requirements)
    category_counts = Counter(_category(item.get("category")) for item in requirements)

    exec_extra = [
        f"This document covers {len(requirements)} extracted requirement(s) "
        f"({mandatory} marked mandatory). "
        "Figures below are copied from the snapshot and are not a bid narrative.",
        f"{pending_decisions} requirement(s) have no human compliance decision "
        f"({PENDING_REVIEW}). AI evidence status is not a compliance decision.",
    ]
    if category_counts:
        exec_extra.append(
            "Categories as extracted: "
            + ", ".join(f"{name} ({total})" for name, total in sorted(category_counts.items()))
            + "."
        )
    _write_narrative_chapter(
        document,
        "Executive Summary",
        matched["Executive Summary"],
        extra_paragraphs=exec_extra,
    )

    document.add_page_break()
    _write_narrative_chapter(
        document,
        "Understanding of Requirements",
        matched["Understanding of Requirements"],
    )
    if requirements:
        _add_data_table(
            document,
            ["Category", "Count"],
            [[name, str(total)] for name, total in sorted(category_counts.items())],
            CATEGORY_WIDTHS_CM,
        )

    document.add_page_break()
    _write_narrative_chapter(
        document,
        "Proposed Technical Approach",
        matched["Proposed Technical Approach"],
    )

    document.add_page_break()
    _write_narrative_chapter(
        document,
        "Implementation Methodology",
        matched["Implementation Methodology"],
    )
    _write_narrative_chapter(
        document,
        "Company Experience and Capabilities",
        matched["Company Experience and Capabilities"],
    )

    _add_oriented_section(document, landscape=True, kind=kind)
    _add_heading(document, "Requirement-by-Requirement Compliance Response", 1)
    _add_body(
        document,
        "This matrix is landscape so requirement wording can wrap at a readable "
        "width. Human decisions are shown only when supplied on the snapshot. "
        "AI evidence status is listed separately and is never treated as a "
        "final compliance decision. Header rows repeat when the table continues.",
        size=10,
    )
    if not requirements:
        _add_body(document, "No requirements are present in this snapshot.", italic=True)
    else:
        _add_data_table(
            document,
            COMPLIANCE_HEADERS,
            _requirement_rows(requirements),
            COMPLIANCE_WIDTHS_CM,
            column_sizes=(9, 10, 9, 9, 9, 9, 8),
        )

    _add_oriented_section(document, landscape=False, kind=kind)
    _add_heading(document, "Assumptions and Information Required", 1)
    if pending_decisions:
        _add_body(
            document,
            f"{INFORMATION_REQUIRED}: {pending_decisions} requirement(s) still "
            f"need a human compliance decision.",
        )
    if not normalized["actions"]:
        _add_body(
            document,
            f"{INFORMATION_REQUIRED}: no reviewer actions are recorded in this snapshot.",
            italic=True,
        )
    else:
        _add_data_table(
            document,
            ["Action ID", "Requirement ID", "Title", "Owner", "Due Date", "Status"],
            [
                [
                    to_text(item.get("actionId")),
                    to_text(item.get("requirementId")),
                    to_text(item.get("title")),
                    to_text(item.get("owner")) or INFORMATION_REQUIRED,
                    format_display_date(item.get("dueDate"))
                    if to_text(item.get("dueDate")).strip()
                    else INFORMATION_REQUIRED,
                    to_text(item.get("status")) or INFORMATION_REQUIRED,
                ]
                for item in normalized["actions"]
            ],
            ACTION_WIDTHS_CM,
        )

    _write_narrative_chapter(document, "Conclusion", matched["Conclusion"])
    if unmatched:
        document.add_page_break()
        _add_heading(document, "Approved Draft Sections", 1)
        _add_body(
            document,
            "The following approved drafts do not match a named response chapter. "
            "Matching titles are placed under their chapters above.",
            size=10,
            italic=True,
        )
        _write_approved_content(document, unmatched)

    _write_pending_drafts(document, normalized["draftSections"])

    _add_oriented_section(document, landscape=True, kind=kind)
    _add_heading(document, "Appendix A — Extracted Requirements Summary", 1)
    _add_body(
        document,
        "Source citations from the snapshot, with a short excerpt. Identical "
        "excerpts are listed once and show every requirement that shares them. "
        "Full chunk text is not repeated.",
        size=10,
    )
    if requirements:
        _add_data_table(
            document,
            ["Requirement ID(s)", "Source", "Excerpt (truncated)"],
            _appendix_requirement_rows(requirements),
            APPENDIX_REQ_WIDTHS_CM,
            column_sizes=(9, 9, 10),
        )
    else:
        _add_body(document, "No extracted requirements are available.", italic=True)

    document.add_page_break()
    _add_heading(document, "Appendix B — Evidence References", 1)
    _add_body(
        document,
        AI_EVIDENCE_CANDIDATE_LABEL,
        size=10,
    )
    if evidence_counts:
        _add_info_table(
            document,
            [(f"AI evidence: {status}", str(total)) for status, total in sorted(evidence_counts.items())]
            + [(f"Human decision: {status}", str(total)) for status, total in sorted(decision_counts.items())],
            widths=INFO_WIDTHS_LANDSCAPE_CM,
        )
    evidence_rows = _evidence_rows(requirements)
    if evidence_rows:
        _add_data_table(
            document,
            [
                "Requirement ID",
                "Evidence source",
                "Page",
                "Similarity",
                "AI Evidence Status",
                "Candidate excerpt (unverified)",
            ],
            evidence_rows,
            EVIDENCE_WIDTHS_CM,
            column_sizes=(9, 9, 9, 9, 9, 10),
        )
    else:
        _add_body(
            document,
            f"{INFORMATION_REQUIRED}: no evidence passages are attached in this snapshot.",
            italic=True,
        )

    buffer = BytesIO()
    document.save(buffer)
    raw = buffer.getvalue()
    pages = None
    if paginator is False:
        return raw
    if callable(paginator):
        pages = paginator(raw, toc_titles)
    elif should_paginate():
        try:
            pages = locate_pages_with_libreoffice(raw, toc_titles)
        except (OSError, RuntimeError, TimeoutError):
            pages = None
    if pages:
        return apply_toc_pages(raw, pages)
    return raw
