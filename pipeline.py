
import json
import hashlib
import os

from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.document.chunker import create_chunks
from app.llm.ollama_client import extract_requirements


CHUNKS_PATH = "chunks/cbs_tender_chunks.json"
PROGRESS_PATH = "chunks/requirements/cbs_tender_progress.json"
OUTPUT_PATH = "chunks/requirements/cbs_tender_requirements.json"


def save_json(path, data):
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    os.replace(temp_path, path)


def process_document(file_path, resume=True):
    if file_path.lower().endswith(".pdf"):
        pages = extract_pdf_text(file_path)
    elif file_path.lower().endswith(".docx"):
        pages = extract_docx_text(file_path)
    else:
        raise ValueError(f"Unsupported file type: {file_path}")

    chunks = create_chunks(pages)

    os.makedirs("chunks/requirements", exist_ok=True)
    save_json(CHUNKS_PATH, chunks)

    print(f"Saved {len(chunks)} chunks to {CHUNKS_PATH}")

    progress = {}

    if resume and os.path.exists(PROGRESS_PATH):
        with open(PROGRESS_PATH, encoding="utf-8") as file:
            saved = json.load(file)

        if (
            saved.get("document") == os.path.abspath(file_path)
            and saved.get("chunk_count") == len(chunks)
        ):
            progress = saved.get("completed_chunks", {})
            print(f"Resuming: {len(progress)} chunks already completed")
        else:
            print("Progress belongs to a different document; starting fresh.")

    for index, chunk in enumerate(chunks, start=1):
        chunk_key = str(index)

        if chunk_key in progress:
            continue

        print(f"Processing chunk {index}/{len(chunks)}...")

        try:
            result = extract_requirements(chunk)
            data = json.loads(result)
            requirements = data.get("requirements", [])

            if not isinstance(requirements, list):
                raise ValueError("Model returned an invalid requirements list")

            valid_requirements = []

            for requirement in requirements:
                if not isinstance(requirement, dict):
                    continue

                requirement["source"] = {
                    "page": chunk["page"],
                    "chunk": index,
                    "excerpt": chunk["text"],
                    "confidence": requirement.get("confidence"),
                }
                valid_requirements.append(requirement)

            progress[chunk_key] = valid_requirements

            save_json(PROGRESS_PATH, {
                "document": os.path.abspath(file_path),
                "chunk_count": len(chunks),
                "completed_chunks": progress,
            })

            all_requirements = [
                req
                for chunk_requirements in progress.values()
                for req in chunk_requirements
            ]
            save_json(OUTPUT_PATH, all_requirements)

            print(
                f"Saved progress: {len(progress)}/{len(chunks)} chunks; "
                f"{len(all_requirements)} requirements"
            )

        except Exception as exc:
            print(f"Chunk {index} failed: {exc}")
            print("Progress saved. Rerun to retry this chunk.")
            raise

    all_requirements = [
        req
        for chunk_requirements in progress.values()
        for req in chunk_requirements
    ]

    save_json(OUTPUT_PATH, all_requirements)
    print(f"Total requirements: {len(all_requirements)}")
    print(f"Saved to {OUTPUT_PATH}")

    return all_requirements


if __name__ == "__main__":
    file_path = "data/s3_test/tender.docx"
    process_document(file_path)
