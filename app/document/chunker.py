"""Source-preserving chunk creation."""

import re

MAX_CHUNK_CHARS = 1600


def _chunk_text(page, text):
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    chunks, current = [], []

    def emit(parts):
        if parts:
            chunks.append({"page": page["page"], "text": "\n\n".join(parts),
                           "source_type": "text", "page_reference_type": page.get("page_reference_type")})

    for paragraph in paragraphs:
        if len(paragraph) > MAX_CHUNK_CHARS:
            emit(current)
            current = []
            words, piece = paragraph.split(), []
            for word in words:
                candidate = " ".join(piece + [word])
                if piece and len(candidate) > MAX_CHUNK_CHARS:
                    emit(piece)
                    piece = [word]
                else:
                    piece.append(word)
            emit(piece)
        elif current and len("\n\n".join(current + [paragraph])) > MAX_CHUNK_CHARS:
            emit(current)
            current = [paragraph]
        else:
            current.append(paragraph)
    emit(current)
    return chunks


def _chunk_table(page, table, table_index):
    rows = [[str(cell).strip() if cell is not None else "" for cell in row] for row in table.get("rows", [])]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    headers = rows[0]
    header_line = "Table headers: " + " | ".join(headers)
    chunks, body = [], []

    def formatted(row):
        return " | ".join(
            f"{headers[index] if index < len(headers) and headers[index] else f'Column {index + 1}'}: {value}"
            for index, value in enumerate(row) if value
        ) or " | ".join(row)

    def emit(rows_to_emit):
        if rows_to_emit:
            # Pack rows until MAX_CHUNK_CHARS. Do not emit one chunk per row:
            # that inflates extract calls and changes checkpoint fingerprints.
            chunks.append({"page": page["page"], "text": "\n".join([header_line, *rows_to_emit]),
                           "source_type": "table", "table_index": table_index,
                           "page_reference_type": page.get("page_reference_type")})

    for row in rows[1:] or [headers]:
        line = formatted(row)
        if body and len("\n".join([header_line, *body, line])) > MAX_CHUNK_CHARS:
            emit(body)
            body = [line]
        else:
            body.append(line)
    emit(body)
    return chunks


def create_chunks(pages):
    """Keep paragraphs intact where possible and tables traceable by row."""
    chunks = []
    for page in pages:
        if not isinstance(page, dict) or "page" not in page:
            raise ValueError("Every extracted page must include a page number.")
        chunks.extend(_chunk_text(page, page.get("text", "")))
        for table_index, table in enumerate(page.get("tables", []), start=1):
            chunks.extend(_chunk_table(page, table, table_index))
    return chunks
