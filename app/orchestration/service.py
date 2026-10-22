
import hashlib
import json
import os

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.llm.ollama_client import extract_requirements


PROGRESS_DIR = os.path.join("chunks", "requirements", "progress")
OUTPUT_DIR = os.path.join("chunks", "requirements", "output")


def save_json(path, data):
    """Save JSON atomically to reduce the risk of partial files."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    os.replace(temp_path, path)


def process_tender_document(file_path, resume=True):
    """Extract requirements and save progress after each completed chunk."""

    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("A valid document file path is required")

    normalized_path = file_path.lower()

    if normalized_path.endswith(".pdf"):
        pages = extract_pdf_text(file_path)
    elif normalized_path.endswith(".docx"):
        pages = extract_docx_text(file_path)
    else:
        raise ValueError("Unsupported file type. Supported formats: PDF and DOCX.")

    if not pages or not any(
        isinstance(page.get("text"), str) and page["text"].strip()
        for page in pages
    ):
        raise ValueError("The document contains no extractable text")

    chunks = create_chunks(pages)
    if not chunks:
        raise ValueError("No text chunks were created")

    document_path = os.path.abspath(file_path)
    document_hash = hashlib.sha256(
        document_path.encode("utf-8")
    ).hexdigest()

    progress_path = os.path.join(PROGRESS_DIR, f"{document_hash}.json")
    output_path = os.path.join(OUTPUT_DIR, f"{document_hash}.json")

    progress = {}
    if resume and os.path.exists(progress_path):
        with open(progress_path, encoding="utf-8") as file:
            saved = json.load(file)

        if (
            saved.get("document") == document_path
            and saved.get("chunk_count") == len(chunks)
        ):
            progress = saved.get("completed_chunks", {})
            if not isinstance(progress, dict):
                progress = {}
        else:
            print("Saved progress does not match this document; starting fresh.")

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_key = str(chunk_index)

        if chunk_key in progress:
            continue

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

        valid_requirements = []

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
            valid_requirements.append(item)

        progress[chunk_key] = valid_requirements

        save_json(progress_path, {
            "document": document_path,
            "chunk_count": len(chunks),
            "completed_chunks": progress,
        })

        all_requirements = [
            requirement
            for chunk_requirements in progress.values()
            for requirement in chunk_requirements
        ]
        save_json(output_path, all_requirements)

        print(f"Saved progress: {len(progress)}/{len(chunks)} chunks")

    all_requirements = [
        requirement
        for chunk_requirements in progress.values()
        for requirement in chunk_requirements
    ]
    save_json(output_path, all_requirements)

    print(f"Total requirements: {len(all_requirements)}")
    print(f"Saved results to: {output_path}")

    return all_requirements
