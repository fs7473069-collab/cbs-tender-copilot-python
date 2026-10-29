import hashlib
import json
import os
import time

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.document.table_extractor import extract_pdf_tables
from app.llm.ollama_client import extract_requirements
from app.rag.matcher import RequirementEvidenceMatcher


PROGRESS_DIR = os.path.join("chunks", "requirements", "progress")
OUTPUT_DIR = os.path.join("chunks", "requirements", "output")


def save_json(path, data):
    """Write JSON atomically so interrupted writes do not corrupt results."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    os.replace(temp_path, path)


def _fingerprint(data):
    serialized = json.dumps(
        data, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _deduplicate(requirements):
    """Keep the first traceable occurrence of repeated extracted wording."""
    unique, seen = [], set()
    for requirement in requirements:
        text = requirement.get("text", "") if isinstance(requirement, dict) else ""
        key = " ".join(text.casefold().split())
        if key and key not in seen:
            seen.add(key)
            unique.append(requirement)
    return unique


def _all_requirements(progress):
    return _deduplicate([
        requirement for chunk_requirements in progress.values()
        for requirement in chunk_requirements
    ])


def process_tender_document(file_path, resume=True):
    """Extract requirements, retain source metadata, and resume safely."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("A valid document file path is required")

    document_path = os.path.abspath(file_path)
    normalized_path = file_path.lower()

    if normalized_path.endswith(".pdf"):
        pages = extract_pdf_text(file_path)

        # Test doubles may supply extracted pages for a nonexistent path.
        # Only inspect tables when the actual PDF file is available.
        if os.path.isfile(file_path):
            tables = extract_pdf_tables(file_path)
            tables_by_page = {}

            for table in tables:
                tables_by_page.setdefault(table["page"], []).append(table)

            for page in pages:
                page["tables"] = tables_by_page.get(page["page"], [])

    elif normalized_path.endswith(".docx"):
        pages = extract_docx_text(file_path)
    else:
        raise ValueError(
            "Unsupported file type. Supported formats are PDF and DOCX."
        )

    if not pages or not any(
        isinstance(page.get("text"), str) and page["text"].strip()
        for page in pages
    ):
        has_tables = any(page.get("tables") for page in pages or [])
        if not has_tables:
            raise ValueError("The document contains no extractable text")

    chunks = create_chunks(pages)
    if not chunks:
        raise ValueError("No text chunks were created")

    # Include extracted content and chunk structure, not just the file path.
    content_hash = _fingerprint({
        "pages": pages,
        "chunks": chunks,
    })
    path_hash = hashlib.sha256(
        document_path.encode("utf-8")
    ).hexdigest()

    progress_path = os.path.join(
        PROGRESS_DIR, f"{path_hash}.json"
    )
    output_path = os.path.join(
        OUTPUT_DIR, f"{path_hash}.json"
    )

    progress = {}

    if resume and os.path.exists(progress_path):
        try:
            with open(progress_path, encoding="utf-8") as file:
                saved = json.load(file)

            if (
                saved.get("document") == document_path
                and saved.get("content_hash") == content_hash
                and saved.get("chunk_count") == len(chunks)
                and isinstance(saved.get("completed_chunks"), dict)
            ):
                progress = saved["completed_chunks"]
            else:
                print(
                    "Document content or chunk structure changed; "
                    "starting fresh."
                )
        except (OSError, json.JSONDecodeError):
            print("Saved progress is invalid; starting fresh.")

    try:
        matcher = RequirementEvidenceMatcher()
    except Exception as exc:
        matcher = None
        matcher_error = f"Evidence matching unavailable: {exc}"

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_key = str(chunk_index)

        if chunk_key in progress:
            print(f"Skipping completed chunk {chunk_index}/{len(chunks)}")
            continue

        print(f"Processing chunk {chunk_index}/{len(chunks)}...")
        print(
            f"Chunk telemetry: index={chunk_index} page={chunk.get('page')} "
            f"source_type={chunk.get('source_type')} chars={len(chunk.get('text') or '')}",
            flush=True,
        )
        extraction_started = time.perf_counter()
        raw_result = extract_requirements(chunk)
        extraction_seconds = time.perf_counter() - extraction_started
        print(f"Requirement extraction: {extraction_seconds:.3f}s", flush=True)

        if isinstance(raw_result, str):
            try:
                result = json.loads(raw_result)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON returned for chunk {chunk_index}"
                ) from exc
        elif isinstance(raw_result, dict):
            result = raw_result
        else:
            raise ValueError(
                f"Unexpected response type for chunk {chunk_index}: "
                f"{type(raw_result).__name__}"
            )

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
                "page": chunk.get("page"),
                "chunk": chunk_index,
                "source_type": chunk.get("source_type", "text"),
                "table_index": chunk.get("table_index"),
                "excerpt": chunk.get("text", ""),
                "confidence": item.get("confidence"),
                "page_reference_type": chunk.get("page_reference_type"),
            }
            item["processing_metrics"] = {
                "requirement_extraction_seconds": round(extraction_seconds, 6),
            }
            valid_requirements.append(item)

        if matcher is None:
            for item in valid_requirements:
                item["evidence_match"] = {
                    "status": "NOT_FOUND",
                    "evidence": [],
                    "note": matcher_error,
                }
        elif valid_requirements:
            try:
                match_started = time.perf_counter()
                match_many = getattr(matcher, "match_requirements", None)
                if callable(match_many):
                    evidence_results = match_many(valid_requirements, top_k=3)
                else:
                    evidence_results = [
                        matcher.match_requirement(item, top_k=3)
                        for item in valid_requirements
                    ]
                match_seconds = time.perf_counter() - match_started
                if len(evidence_results) != len(valid_requirements):
                    raise ValueError(
                        "Evidence matcher returned a mismatched result count."
                    )
                total_llm_calls = 0
                total_relevance_llm_s = 0.0
                total_cache_hits = 0
                for item, evidence_match in zip(
                    valid_requirements, evidence_results
                ):
                    item["evidence_match"] = evidence_match
                    timings = (evidence_match or {}).get("timings") or {}
                    total_llm_calls += int(timings.get("relevance_llm_calls") or 0)
                    total_relevance_llm_s += float(
                        timings.get("relevance_llm_seconds") or 0.0
                    )
                    total_cache_hits += int(timings.get("relevance_cache_hits") or 0)
                    # Per-requirement line: do NOT repeat batch wall_s (avoids
                    # double-counting when summing logs).
                    print(
                        "Evidence match item: "
                        f"chunk={chunk_index} "
                        f"status={evidence_match.get('status')} "
                        f"retrieved={timings.get('candidates_retrieved')} "
                        f"prefiltered={timings.get('candidates_prefiltered')} "
                        f"llm_calls={timings.get('relevance_llm_calls')} "
                        f"cache_hits={timings.get('relevance_cache_hits')} "
                        f"embed_s={timings.get('query_embedding_seconds')} "
                        f"relevance_llm_s={timings.get('relevance_llm_seconds')}",
                        flush=True,
                    )
                print(
                    "Evidence match batch: "
                    f"chunk={chunk_index} batch_wall_s={match_seconds:.3f} "
                    f"requirements={len(valid_requirements)} "
                    f"total_llm_calls={total_llm_calls} "
                    f"total_cache_hits={total_cache_hits} "
                    f"sum_relevance_llm_s={total_relevance_llm_s:.3f}",
                    flush=True,
                )
            except Exception as exc:
                for item in valid_requirements:
                    item["evidence_match"] = {
                        "status": "REVIEW_REQUIRED",
                        "evidence": [],
                        "note": (
                            "Evidence matching failed and requires review: "
                            f"{exc}"
                        ),
                    }

        progress[chunk_key] = valid_requirements

        # Progress remains the durable resume checkpoint after every chunk.
        # Final output JSON is written once at the end to avoid rewriting the
        # full requirements list on each chunk.
        save_json(progress_path, {
            "document": document_path,
            "content_hash": content_hash,
            "chunk_count": len(chunks),
            "completed_chunks": progress,
        })

        all_requirements = _all_requirements(progress)
        print(f"Saved progress: {len(progress)}/{len(chunks)} chunks")
        print(f"Requirements extracted so far: {len(all_requirements)}")

    all_requirements = _all_requirements(progress)
    save_json(output_path, all_requirements)

    print("\nExtraction completed successfully.")
    print(f"Total chunks: {len(chunks)}")
    print(f"Total requirements: {len(all_requirements)}")
    print(f"Saved results to: {output_path}")

    return all_requirements
