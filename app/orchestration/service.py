
import json

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.llm.ollama_client import extract_requirements


SUPPORTED_EXTENSIONS = (".pdf", ".docx")


def process_tender_document(file_path):
    """
    Orchestrate tender document processing and requirement extraction.

    Returns a list of requirements with source information.
    """

    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("A valid document file path is required")

    normalized_path = file_path.lower()

    if normalized_path.endswith(".pdf"):
        pages = extract_pdf_text(file_path)
    elif normalized_path.endswith(".docx"):
        pages = extract_docx_text(file_path)
    else:
        raise ValueError(
            "Unsupported file type. Supported formats: PDF and DOCX."
        )

    if not pages or not any(page.get("text", "").strip() for page in pages):
        raise ValueError("The document contains no extractable text")

    chunks = create_chunks(pages)
    all_requirements = []

    for chunk_index, chunk in enumerate(chunks, start=1):
        raw_result = extract_requirements(chunk)

        try:
            result = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"Invalid JSON returned for chunk {chunk_index}"
            ) from exc

        if not isinstance(result, dict):
            raise ValueError(
                f"Invalid response structure for chunk {chunk_index}"
            )

        requirements = result.get("requirements")

        if not isinstance(requirements, list):
            raise ValueError(
                f"Missing or invalid requirements list in chunk {chunk_index}"
            )

        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise ValueError(
                    f"Invalid requirement in chunk {chunk_index}"
                )

            text = requirement.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Requirement has missing text in chunk {chunk_index}"
                )

            item = requirement.copy()
            item["source"] = {
                "page": chunk["page"],
                "chunk": chunk_index,
                "excerpt": chunk["text"],
                "confidence": item.get("confidence"),
            }

            all_requirements.append(item)

    return all_requirements
