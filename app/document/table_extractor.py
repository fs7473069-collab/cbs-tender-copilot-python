import contextlib
import io

import pymupdf


def extract_pdf_tables(pdf_path):
    tables = []

    with pymupdf.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            with contextlib.redirect_stdout(io.StringIO()):
                page_tables = page.find_tables()

            for table in page_tables.tables:
                rows = table.extract()

                tables.append({
                    "page": page_number,
                    "rows": rows
                })

    return tables