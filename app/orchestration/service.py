import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.document.structure import describe_chunk_structure, locate_requirement
from app.document.table_extractor import extract_pdf_tables
from app.llm.ollama_client import extract_requirements
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.retriever import INGEST_HINT


PROGRESS_DIR = os.path.join("chunks", "requirements", "progress")
OUTPUT_DIR = os.path.join("chunks", "requirements", "output")
# 1 keeps the original strictly sequential extraction loop.
EXTRACT_MAX_WORKERS_ENV = "TENDER_EXTRACT_MAX_WORKERS"
EXTRACT_MAX_WORKERS_DEFAULT = 2


def extract_max_workers():
    """Concurrent chunk extractions, read at call time from the environment."""
    raw = os.getenv(EXTRACT_MAX_WORKERS_ENV)
    if raw is None or not raw.strip():
        return EXTRACT_MAX_WORKERS_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{EXTRACT_MAX_WORKERS_ENV} must be a positive integer, got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError(
            f"{EXTRACT_MAX_WORKERS_ENV} must be a positive integer, got {raw!r}"
        )
    return value


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


def _chunk_order(chunk_key):
    try:
        return 0, int(chunk_key)
    except (TypeError, ValueError):
        return 1, str(chunk_key)


def _all_requirements(progress):
    # Resumed parallel runs can complete chunks out of order; output follows
    # original chunk index regardless of checkpoint insertion order.
    return _deduplicate([
        requirement
        for chunk_key in sorted(progress, key=_chunk_order)
        for requirement in progress[chunk_key]
    ])


def _timed_extract(chunk):
    started = time.perf_counter()
    raw_result = extract_requirements(chunk)
    return raw_result, time.perf_counter() - started


def _build_chunk_requirements(
    chunk_index, chunk, raw_result, extraction_seconds, matcher, matcher_error
):
    """Validate one chunk's extraction, attach source metadata and evidence."""
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

    _attach_evidence(chunk_index, valid_requirements, matcher, matcher_error)
    return valid_requirements


_LEGACY_FAILED_MATCH_NOTES = (
    "Evidence matching was not run",
    "Evidence matching unavailable",
    "Evidence matching failed",
)


def _needs_evidence_rematch(requirement):
    """True when a checkpointed requirement's evidence check never finished."""
    evidence_match = requirement.get("evidence_match")
    if not isinstance(evidence_match, dict):
        return True
    if (evidence_match.get("retrieval") or {}).get("status") == "FAILED":
        return True
    if (evidence_match.get("relevance") or {}).get("status") == "INCOMPLETE":
        return True
    return str(evidence_match.get("note") or "").startswith(
        _LEGACY_FAILED_MATCH_NOTES
    )


def evidence_preflight(matcher, matcher_error=None):
    """Log whether evidence retrieval can run; return the retriever status.

    Processing continues when the index is unavailable: requirements are then
    marked NOT_CHECKED (never NOT_FOUND) and a later resume re-runs matching.
    """
    if matcher is None:
        print(
            "EVIDENCE_PREFLIGHT status=UNAVAILABLE code=matcher_unavailable; "
            "requirements will be marked NOT_CHECKED. "
            f"{matcher_error or ''}".rstrip(),
            flush=True,
        )
        return {"available": False, "error_code": "matcher_unavailable"}
    retriever = getattr(matcher, "retriever", None)
    status_fn = getattr(retriever, "status", None)
    if not callable(status_fn):
        return None
    status = status_fn()
    if status.get("available"):
        exclusions = getattr(retriever, "active_exclusions", None)
        excluded = exclusions() if callable(exclusions) else []
        print(
            "EVIDENCE_PREFLIGHT status=OK "
            f"chunks={status.get('chunks')} dimension={status.get('dimension')} "
            f"excluded_sources={len(excluded)}",
            flush=True,
        )
    else:
        print(
            "EVIDENCE_PREFLIGHT status=UNAVAILABLE "
            f"code={status.get('error_code')}; requirements will be marked "
            f"NOT_CHECKED (not NOT_FOUND). {INGEST_HINT}",
            flush=True,
        )
    return status


def _apply_document_structure(requirements, chunks):
    """Set source section/clause where the tender structure establishes them."""
    structures = describe_chunk_structure(chunks)
    for requirement in requirements:
        source = requirement.get("source")
        if not isinstance(source, dict):
            continue
        chunk_index = source.get("chunk")
        if not isinstance(chunk_index, int) or not 1 <= chunk_index <= len(chunks):
            continue
        located = locate_requirement(
            structures[chunk_index - 1], requirement.get("text", "")
        )
        for key in ("section", "clause"):
            if source.get(key) is None and located[key] is not None:
                source[key] = located[key]


def _attach_evidence(chunk_index, valid_requirements, matcher, matcher_error):
    """Set ``evidence_match`` on each requirement in place."""
    if matcher is None:
        for item in valid_requirements:
            item["evidence_match"] = {
                "status": "NOT_CHECKED",
                "evidence": [],
                "note": matcher_error,
                "retrieval": {
                    "status": "FAILED",
                    "error_code": "matcher_unavailable",
                },
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
            if not all(isinstance(result, dict) for result in evidence_results):
                raise ValueError("Evidence matcher returned an invalid result.")
            total_llm_calls = 0
            total_relevance_llm_s = 0.0
            total_cache_hits = 0
            status_counts = {}
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
                retrieval = (evidence_match or {}).get("retrieval") or {}
                status_counts[evidence_match.get("status")] = (
                    status_counts.get(evidence_match.get("status"), 0) + 1
                )
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
                    f"relevance_llm_s={timings.get('relevance_llm_seconds')} "
                    f"retrieval={retrieval.get('status')} "
                    f"error_code={retrieval.get('error_code')}",
                    flush=True,
                )
            print(
                "Evidence match batch: "
                f"chunk={chunk_index} batch_wall_s={match_seconds:.3f} "
                f"requirements={len(valid_requirements)} "
                f"total_llm_calls={total_llm_calls} "
                f"total_cache_hits={total_cache_hits} "
                f"sum_relevance_llm_s={total_relevance_llm_s:.3f} "
                f"statuses={json.dumps(status_counts, sort_keys=True)}",
                flush=True,
            )
        except Exception as exc:
            print(
                "EVIDENCE_RETRIEVAL status=FAILED code=matcher_exception "
                f"chunk={chunk_index} error_type={type(exc).__name__}",
                flush=True,
            )
            for item in valid_requirements:
                item["evidence_match"] = {
                    "status": "NOT_CHECKED",
                    "evidence": [],
                    "note": (
                        "Evidence matching failed; the requirement was not "
                        f"checked against the knowledge base: {exc}"
                    ),
                    "retrieval": {
                        "status": "FAILED",
                        "error_code": "matcher_exception",
                        "error_type": type(exc).__name__,
                    },
                }


def load_document_chunks(file_path):
    """Extract pages and chunks; return them with resume checkpoint paths."""
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

    return {
        "document_path": document_path,
        "pages": pages,
        "chunks": chunks,
        "content_hash": content_hash,
        "progress_path": os.path.join(PROGRESS_DIR, f"{path_hash}.json"),
        "output_path": os.path.join(OUTPUT_DIR, f"{path_hash}.json"),
    }


def load_resume_progress(loaded):
    """Checkpointed chunk results when they match the loaded document, else None."""
    progress_path = loaded["progress_path"]
    if not os.path.exists(progress_path):
        return None
    try:
        with open(progress_path, encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, json.JSONDecodeError):
        print("Saved progress is invalid; starting fresh.")
        return None
    if (
        saved.get("document") == loaded["document_path"]
        and saved.get("content_hash") == loaded["content_hash"]
        and saved.get("chunk_count") == len(loaded["chunks"])
        and isinstance(saved.get("completed_chunks"), dict)
    ):
        return saved["completed_chunks"]
    print(
        "Document content or chunk structure changed; "
        "starting fresh."
    )
    return None


def process_tender_document(file_path, resume=True):
    """Extract requirements, retain source metadata, and resume safely."""
    loaded = load_document_chunks(file_path)
    document_path = loaded["document_path"]
    chunks = loaded["chunks"]
    content_hash = loaded["content_hash"]
    progress_path = loaded["progress_path"]
    output_path = loaded["output_path"]

    progress = (load_resume_progress(loaded) if resume else None) or {}

    try:
        matcher = RequirementEvidenceMatcher()
    except Exception as exc:
        matcher = None
        matcher_error = f"Evidence matching unavailable: {exc}"
    else:
        matcher_error = None
    evidence_preflight(matcher, matcher_error)

    def save_progress():
        save_json(progress_path, {
            "document": document_path,
            "content_hash": content_hash,
            "chunk_count": len(chunks),
            "completed_chunks": progress,
        })

    pending = []
    stale_matches = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_key = str(chunk_index)
        if chunk_key not in progress:
            pending.append((chunk_index, chunk))
            continue
        print(f"Skipping completed chunk {chunk_index}/{len(chunks)}")
        stale = [
            item for item in progress[chunk_key]
            if isinstance(item, dict) and _needs_evidence_rematch(item)
        ]
        if stale:
            stale_matches.append((chunk_index, stale))

    max_workers = extract_max_workers()
    active_workers = 1
    executor = None
    futures = {}
    if max_workers > 1 and len(pending) > 1:
        # Only LLM extraction runs in workers. Validation, evidence matching,
        # and checkpointing stay on this thread in original chunk order.
        active_workers = min(max_workers, len(pending))
        executor = ThreadPoolExecutor(
            max_workers=active_workers,
            thread_name_prefix="extract",
        )
        futures = {
            chunk_index: executor.submit(_timed_extract, chunk)
            for chunk_index, chunk in pending
        }
    print(
        f"Requirement extraction workers: {active_workers} "
        f"(configured={max_workers}, pending_chunks={len(pending)})",
        flush=True,
    )

    first_error = None
    try:
        if matcher is not None:
            # Extraction is reused from the checkpoint; only evidence searches
            # that never ran are retried, while new extractions proceed.
            for chunk_index, stale in stale_matches:
                print(
                    f"Re-running evidence matching for {len(stale)} "
                    f"checkpointed requirement(s) in chunk {chunk_index}",
                    flush=True,
                )
                _attach_evidence(chunk_index, stale, matcher, matcher_error)
                save_progress()

        for chunk_index, chunk in pending:
            chunk_key = str(chunk_index)
            future = futures.get(chunk_index)
            if future is not None and future.cancelled():
                # Never started after an earlier failure; resume retries it.
                continue

            print(f"Processing chunk {chunk_index}/{len(chunks)}...")
            print(
                f"Chunk telemetry: index={chunk_index} page={chunk.get('page')} "
                f"source_type={chunk.get('source_type')} chars={len(chunk.get('text') or '')}",
                flush=True,
            )
            try:
                if future is None:
                    raw_result, extraction_seconds = _timed_extract(chunk)
                else:
                    raw_result, extraction_seconds = future.result()
                print(
                    f"Requirement extraction: {extraction_seconds:.3f}s",
                    flush=True,
                )
                valid_requirements = _build_chunk_requirements(
                    chunk_index,
                    chunk,
                    raw_result,
                    extraction_seconds,
                    matcher,
                    matcher_error,
                )
            except Exception as exc:
                if executor is None:
                    raise
                print(f"Chunk {chunk_index} failed: {exc!r}", flush=True)
                if first_error is None:
                    first_error = exc
                    cancelled = sum(
                        1 for pending_future in futures.values()
                        if pending_future.cancel()
                    )
                    print(
                        f"Cancelled {cancelled} queued chunk extraction(s); "
                        "saving chunks already extracted before stopping.",
                        flush=True,
                    )
                continue

            progress[chunk_key] = valid_requirements

            # Progress remains the durable resume checkpoint after every chunk.
            # Final output JSON is written once at the end to avoid rewriting the
            # full requirements list on each chunk.
            save_progress()

            all_requirements = _all_requirements(progress)
            print(f"Saved progress: {len(progress)}/{len(chunks)} chunks")
            print(f"Requirements extracted so far: {len(all_requirements)}")
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    if first_error is not None:
        raise first_error

    all_requirements = _all_requirements(progress)
    _apply_document_structure(all_requirements, chunks)
    save_json(output_path, all_requirements)

    print("\nExtraction completed successfully.")
    print(f"Total chunks: {len(chunks)}")
    print(f"Total requirements: {len(all_requirements)}")
    print(f"Saved results to: {output_path}")

    return all_requirements
