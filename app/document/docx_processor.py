
import os
import tempfile

import win32com.client

from app.document.pdf_processor import extract_pdf_text


def extract_docx_text(docx_path):
    word = None
    document = None
    temp_pdf = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
        ) as temp_file:
            temp_pdf = temp_file.name

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        document = word.Documents.Open(
            os.path.abspath(docx_path),
            ReadOnly=True,
            AddToRecentFiles=False,
        )

        # Export the document using Word's actual page layout.
        document.ExportAsFixedFormat(
            OutputFileName=temp_pdf,
            ExportFormat=17,  # PDF
        )

        document.Close(False)
        document = None

        # Extract text with the correct rendered page numbers.
        return extract_pdf_text(temp_pdf)

    finally:
        if document is not None:
            document.Close(False)

        if word is not None:
            word.Quit()

        if temp_pdf and os.path.exists(temp_pdf):
            os.remove(temp_pdf)
