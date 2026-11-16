"""Fill Word TOC page numbers from a LibreOffice (or equivalent) PDF layout."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TOC_PAGE_UNRESOLVED = "—"

_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
)


def find_soffice():
    """Return a LibreOffice binary path, or None."""
    env = os.environ.get("TENDER_LIBREOFFICE") or os.environ.get("SOFFICE")
    if env and Path(env).exists():
        return env
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _SOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    try:
        cask = Path("/opt/homebrew/Caskroom/libreoffice")
        matches = sorted(cask.glob("*/LibreOffice.app/Contents/MacOS/soffice"))
        if matches:
            return str(matches[-1])
    except OSError:
        pass
    return None


def should_paginate():
    """True when production exports should ask LibreOffice for page numbers.

    Unit tests skip this unless ``TENDER_WORD_PAGINATE=1`` so pytest does not
    convert every document.
    """
    flag = os.environ.get("TENDER_WORD_PAGINATE", "").strip()
    if flag == "0":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and flag != "1":
        return False
    return find_soffice() is not None


def fold_heading(text):
    cleaned = (text or "").replace("—", " ").replace("–", " ").replace("-", " ")
    return " ".join(cleaned.split()).casefold()


def _page_lines(page):
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            size = max((span.get("size") or 0 for span in spans), default=0)
            lines.append((text, size))
    return lines


def _is_toc_page(lines):
    joined = "\n".join(text for text, _ in lines)
    return "Table of Contents" in joined and "Section" in joined and "Page" in joined


def _line_matches(title, text, size):
    folded_title = fold_heading(title)
    folded_line = fold_heading(text)
    if not folded_title or not folded_line:
        return False
    if folded_line == folded_title:
        return True
    if size >= 13 and folded_title in folded_line:
        return True
    return False


def heading_pages_from_pdf(pdf_path, titles):
    """Map TOC titles to 1-based PDF page numbers.

    TOC pages are ignored when locating chapter headings so the contents table
    itself does not match. Missing titles are omitted rather than guessed.
    """
    import fitz

    document = fitz.open(pdf_path)
    pages = [_page_lines(page) for page in document]
    toc_indexes = [index for index, lines in enumerate(pages) if _is_toc_page(lines)]
    after_toc = (max(toc_indexes) + 1) if toc_indexes else 1

    located = {}
    for title in titles:
        if title == "Cover Page":
            located[title] = 1
            continue
        found = None
        for index in range(after_toc, len(pages)):
            for text, size in pages[index]:
                if _line_matches(title, text, size):
                    found = index + 1
                    break
            if found is not None:
                break
        if found is not None:
            located[title] = found
    return located


def convert_docx_to_pdf(docx_bytes, output_dir, *, soffice=None):
    binary = soffice or find_soffice()
    if not binary:
        raise FileNotFoundError("LibreOffice soffice is not installed")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = output_dir / "export.docx"
    source.write_bytes(docx_bytes)
    profile = (output_dir / "lo-profile").resolve()
    profile.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            binary,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(output_dir),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    pdf_path = output_dir / "export.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"LibreOffice PDF conversion failed: {detail}")
    return pdf_path


def locate_pages_with_libreoffice(docx_bytes, titles, *, soffice=None):
    with tempfile.TemporaryDirectory(prefix="tender-word-toc-") as tmp:
        pdf_path = convert_docx_to_pdf(docx_bytes, tmp, soffice=soffice)
        return heading_pages_from_pdf(pdf_path, titles)
