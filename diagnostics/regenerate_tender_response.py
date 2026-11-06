"""Regenerate a completed response for an existing tender without RabbitMQ.

Never imports the consumer or publisher and never writes
local_data/analysis/actual_response.json. Valid extraction checkpoints are
resumed, so normally only evidence matching for requirements whose evidence
check never finished re-runs, plus at most one tender-summary LLM call.
Prior results are copied (never moved) to a timestamped backup folder first.

Usage:
  python -m diagnostics.regenerate_tender_response --dry-run
  python -m diagnostics.regenerate_tender_response
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from app.orchestration import service
from app.orchestration import tender_summary
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.retriever import TenderRetriever
from app.response.response_builder import build_completed_response

DEFAULT_DOCUMENT = "data/s3_test/tender.pdf"
DEFAULT_REQUEST = "local_data/analysis/actual_response.json"
ANALYSIS_RESPONSE = PROJECT_ROOT / "local_data" / "analysis" / "actual_response.json"
REGENERATED_DIR = PROJECT_ROOT / "local_data" / "analysis" / "regenerated"
BACKUP_ROOT = PROJECT_ROOT / "local_data" / "backups"
# Measured on this machine with qwen3:8b (CPU): evidence matching ~5-11 s per
# requirement, chunk extraction ~55 s wall while Ollama serializes requests.
DEFAULT_SECONDS_PER_REQUIREMENT = 9.0
DEFAULT_SECONDS_PER_CHUNK = 55.0
SUMMARY_CALL_SECONDS = 30.0
RECENT_ACTIVITY_SECONDS = 300
_REQUEST_ID_FIELDS = (
    "organizationId", "workspaceId", "jobId", "analysisId", "correlationId",
    "attempt",
)


def _load_request(path):
    """Request IDs from a saved request, or rebuilt from a saved response."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("eventType") != "RFP_ANALYSIS_COMPLETED":
        return data
    request = {field: data.get(field) for field in _REQUEST_ID_FIELDS}
    request["documents"] = [
        {"documentId": document_id}
        for document_id in data.get("processedDocumentIds") or []
    ]
    # The original inbound tender object is not stored in a response.
    request["tender"] = {}
    return request


def _summary_sidecar(document_path):
    return Path(tender_summary._sidecar_path(document_path))


def backup_results(loaded, stamp):
    """Copy existing results for this tender; returns the backup folder."""
    backup_dir = BACKUP_ROOT / stamp
    sources = [
        ANALYSIS_RESPONSE,
        PROJECT_ROOT / loaded["output_path"],
        PROJECT_ROOT / loaded["progress_path"],
        _summary_sidecar(loaded["document_path"]),
    ]
    copied = []
    for source in sources:
        source = Path(source).resolve()
        if not source.is_file():
            continue
        target = backup_dir / source.relative_to(PROJECT_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target.relative_to(PROJECT_ROOT)))
    return backup_dir, copied


def clear_checkpointed_evidence(loaded):
    """Drop evidence results (not extractions) so resume re-matches them all."""
    path = PROJECT_ROOT / loaded["progress_path"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    cleared = 0
    for items in saved["completed_chunks"].values():
        for item in items:
            if isinstance(item, dict) and item.pop("evidence_match", None) is not None:
                cleared += 1
    service.save_json(str(path), saved)
    return cleared


def estimate(
    loaded, progress, seconds_per_requirement, seconds_per_chunk,
    rematch_all=False,
):
    progress = progress or {}
    checkpointed = [
        item
        for key in progress
        for item in progress[key]
        if isinstance(item, dict)
    ]
    rematch = sum(
        1 for item in checkpointed
        if rematch_all or service._needs_evidence_rematch(item)
    )
    pending_chunks = len(loaded["chunks"]) - len(progress)
    per_chunk = len(checkpointed) / len(progress) if progress else 5.0
    new_requirements = round(pending_chunks * per_chunk)
    seconds = (
        rematch * seconds_per_requirement
        + pending_chunks * seconds_per_chunk
        + new_requirements * seconds_per_requirement
        + SUMMARY_CALL_SECONDS
    )
    return {
        "chunks": len(loaded["chunks"]),
        "checkpointed_chunks": len(progress),
        "pending_chunks": pending_chunks,
        "checkpointed_requirements": len(checkpointed),
        "evidence_rematch_requirements": rematch,
        "estimated_new_requirements": new_requirements,
        "estimated_seconds": round(seconds),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--document", default=DEFAULT_DOCUMENT)
    parser.add_argument(
        "--request", default=DEFAULT_REQUEST,
        help="Saved inbound request or previous completed response (IDs only).",
    )
    parser.add_argument(
        "--relevance-cache",
        help="Relevance cache path; default is the production cache.",
    )
    parser.add_argument(
        "--seconds-per-requirement", type=float,
        default=DEFAULT_SECONDS_PER_REQUIREMENT,
    )
    parser.add_argument(
        "--seconds-per-chunk", type=float, default=DEFAULT_SECONDS_PER_CHUNK,
    )
    parser.add_argument(
        "--rematch-all-evidence", action="store_true",
        help="Re-run evidence matching for every checkpointed requirement "
             "(e.g. after changing CBS_KB_EXCLUDE_SOURCES); extraction is reused.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force", action="store_true",
        help="Run even if the tender checkpoint changed in the last 5 minutes.",
    )
    parser.add_argument(
        "--allow-unavailable-kb", action="store_true",
        help="Proceed when the evidence index is unavailable (NOT_CHECKED).",
    )
    args = parser.parse_args(argv)
    # Checkpoint directories are relative to the project root.
    os.chdir(PROJECT_ROOT)

    kb_retriever = TenderRetriever()
    kb_status = kb_retriever.status()
    print(f"KB preflight: {json.dumps(kb_status, sort_keys=True)}", flush=True)
    print(
        f"KB excluded sources: {json.dumps(kb_retriever.active_exclusions())}",
        flush=True,
    )
    if not kb_status["available"] and not args.allow_unavailable_kb:
        print(
            "Evidence index unavailable; refusing to regenerate. Build it with "
            "python -m app.rag.ingest or pass --allow-unavailable-kb.",
            flush=True,
        )
        return 2

    loaded = service.load_document_chunks(args.document)
    progress = service.load_resume_progress(loaded)
    plan = estimate(
        loaded, progress, args.seconds_per_requirement, args.seconds_per_chunk,
        rematch_all=args.rematch_all_evidence,
    )
    print(f"Plan: {json.dumps(plan, sort_keys=True)}", flush=True)

    progress_path = PROJECT_ROOT / loaded["progress_path"]
    if progress_path.exists():
        idle = time.time() - progress_path.stat().st_mtime
        if idle < RECENT_ACTIVITY_SECONDS and not args.force and not args.dry_run:
            print(
                f"Checkpoint was modified {idle:.0f}s ago; another process (for "
                "example the RabbitMQ consumer) may be processing this tender. "
                "Re-run when it is idle or pass --force.",
                flush=True,
            )
            return 3

    if args.dry_run:
        print("Dry run: no Ollama calls made and no files written.", flush=True)
        return 0

    request = _load_request(args.request)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir, copied = backup_results(loaded, stamp)
    print(f"Backed up {len(copied)} file(s) to {backup_dir}", flush=True)
    if args.rematch_all_evidence and progress:
        cleared = clear_checkpointed_evidence(loaded)
        print(f"Cleared {cleared} checkpointed evidence result(s)", flush=True)

    if args.relevance_cache:
        service.RequirementEvidenceMatcher = functools.partial(
            RequirementEvidenceMatcher, cache_path=args.relevance_cache
        )

    started = time.perf_counter()
    requirements = service.process_tender_document(args.document, resume=True)
    summary = tender_summary.summarize_tender_document(
        args.document, request_tender=request.get("tender")
    )
    response = build_completed_response(
        request_message=request,
        requirements=requirements,
        tender_summary=summary,
    )

    REGENERATED_DIR.mkdir(parents=True, exist_ok=True)
    output = REGENERATED_DIR / f"response_{stamp}.json"
    if output.exists():
        raise FileExistsError(output)
    output.write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    published = response["result"]["requirements"]
    print(f"Wrote {output}", flush=True)
    print(f"Elapsed: {time.perf_counter() - started:.1f}s", flush=True)
    print(f"Requirements: {len(published)}", flush=True)
    print(
        "Evidence statuses: "
        + json.dumps(
            Counter(item["evidenceMatch"]["status"] for item in published),
            sort_keys=True,
        ),
        flush=True,
    )
    print(
        "Retrieval statuses: "
        + json.dumps(
            Counter(
                ((item.get("evidence_match") or {}).get("retrieval") or {}).get(
                    "status", "MISSING"
                )
                for item in requirements
            ),
            sort_keys=True,
        ),
        flush=True,
    )
    print(
        "Summary fields populated: "
        + json.dumps(
            sorted(
                field for field in tender_summary.SUMMARY_FIELDS
                if response["result"][field] is not None
            )
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
