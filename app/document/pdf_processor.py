"""PDF text extraction with optional OCR."""

import os

import pymupdf


def _ocr_page(page):
    """OCR a page without making Tesseract mandatory for normal PDFs."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return "", f"OCR unavailable: {exc.name} is not installed"

    configured_path = os.getenv("TESSERACT_CMD")
    if configured_path:
        pytesseract.pytesseract.tesseract_cmd = configured_path
    try:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return pytesseract.image_to_string(image, lang="eng").strip(), None
    except (OSError, RuntimeError, pytesseract.TesseractError) as exc:
        return "", f"OCR failed: {exc}"


def extract_pdf_text(pdf_path, *, enable_ocr=True):
    """Return actual PDF pages with text and non-fatal OCR diagnostics."""
    pages = []
    try:
        document = pymupdf.open(pdf_path)
    except (OSError, RuntimeError, pymupdf.FileDataError) as exc:
        raise ValueError(f"Unable to open PDF '{pdf_path}': {exc}") from exc

    with document:
        for page_number, page in enumerate(document, start=1):
            page_data = {
                "page": page_number,
                "text": page.get_text("text").strip(),
                "page_reference_type": "actual_pdf_page",
            }
            if not page_data["text"] and enable_ocr:
                text, error = _ocr_page(page)
                page_data["text"] = text
                page_data["ocr_used"] = True
                if error:
                    page_data["ocr_error"] = error
            pages.append(page_data)
    return pages
