
from pathlib import Path

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.rag.retriever import TenderRetriever

KB_DIR = Path("local_data/cbs_knowledge_base/documents")
SUPPORTED_EXTENSIONS = {".pdf", ".docx"}


def load_document(file_path):
    """Extract text from a supported document and preserve page numbers."""
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        pages = extract_pdf_text(str(file_path))
    elif suffix == ".docx":
        pages = extract_docx_text(str(file_path))
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return pages


def build_document_chunks(file_path):
    """Create chunks with source filenames and page metadata."""
    pages = load_document(file_path)

    usable_pages = [
        page for page in pages
        if isinstance(page.get("text"), str) and page["text"].strip()
    ]

    if not usable_pages:
        print(f"Skipping empty document: {file_path.name}")
        return []

    chunks = create_chunks(usable_pages)

    for chunk in chunks:
        chunk["source"] = file_path.name

    return chunks


def ingest_knowledge_base():
    """Index documents from the CBS knowledge-base folder."""
    KB_DIR.mkdir(parents=True, exist_ok=True)

    document_paths = sorted(
        path for path in KB_DIR.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not document_paths:
        print(f"No PDF or DOCX documents found in {KB_DIR.resolve()}")
        return 0

    all_chunks = []

    for path in document_paths:
        print(f"Reading: {path.name}")
        chunks = build_document_chunks(path)
        all_chunks.extend(chunks)
        print(f"  Chunks created: {len(chunks)}")

    if not all_chunks:
        print("No usable text chunks found.")
        return 0

    # A new full index is built from the folder's current contents.
    retriever = TenderRetriever(persist=False)
    count = retriever.ingest(all_chunks)

    # Persist the complete index and its matching metadata.
    retriever.persist = True
    retriever.save()

    print(f"\nKnowledge-base ingestion completed: {count} chunks")
    return count


if __name__ == "__main__":
    ingest_knowledge_base()
