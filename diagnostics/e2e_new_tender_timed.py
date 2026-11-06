"""
Timed E2E on a NEW tender path (fresh progress/output hashes).

Uses production-default OLLAMA_MODEL (qwen3:8b). Does not delete caches,
overwrite baseline reports, upload to production S3, or change the model.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
os.environ.setdefault("OLLAMA_MODEL", "qwen3:8b")

from app.llm import ollama_client
from app.orchestration import service
from app.rag import matcher as matcher_mod
from app.storage.s3_data_locally import generate_local_excel
from app.storage.s3_data_locally import generate_local_word

SOURCE_TENDER = PROJECT_ROOT / "data" / "s3_test" / "tender.pdf"
E2E_DIR = PROJECT_ROOT / "data" / "e2e_new_tender"
E2E_TENDER = E2E_DIR / "representative_new_tender.pdf"
REPORT_DIR = PROJECT_ROOT / "local_data" / "reports" / "e2e_new_tender"
MOCK_S3_DIR = PROJECT_ROOT / "local_data" / "mock_s3" / "e2e_new_tender"
METRICS_PATH = PROJECT_ROOT / "diagnostics" / "e2e_new_tender_metrics.json"


def main():
    if not SOURCE_TENDER.exists():
        raise FileNotFoundError(SOURCE_TENDER)

    E2E_DIR.mkdir(parents=True, exist_ok=True)
    if not E2E_TENDER.exists() or E2E_TENDER.stat().st_size != SOURCE_TENDER.stat().st_size:
        shutil.copy2(SOURCE_TENDER, E2E_TENDER)

    print(f"MODEL={ollama_client.MODEL_NAME}", flush=True)
    print(f"TENDER={E2E_TENDER}", flush=True)

    extract_seconds = 0.0
    match_seconds = 0.0
    extract_calls = 0
    extract_skips = 0
    match_batches = 0
    original_extract = ollama_client.extract_requirements
    original_match_many = matcher_mod.RequirementEvidenceMatcher.match_requirements

    def timed_extract(chunk):
        nonlocal extract_seconds, extract_calls, extract_skips
        started = time.perf_counter()
        result = original_extract(chunk)
        elapsed = time.perf_counter() - started
        # Skip path returns almost instantly without generate.
        if elapsed < 0.05 and result == {"requirements": []}:
            extract_skips += 1
        else:
            extract_calls += 1
            extract_seconds += elapsed
        return result

    def timed_match_many(self, requirements, top_k=3):
        nonlocal match_seconds, match_batches
        started = time.perf_counter()
        result = original_match_many(self, requirements, top_k=top_k)
        match_seconds += time.perf_counter() - started
        match_batches += 1
        return result

    ollama_client.extract_requirements = timed_extract
    service.extract_requirements = timed_extract
    matcher_mod.RequirementEvidenceMatcher.match_requirements = timed_match_many

    wall_started = time.perf_counter()
    analysis_started = time.perf_counter()
    requirements = service.process_tender_document(str(E2E_TENDER), resume=True)
    analysis_seconds = time.perf_counter() - analysis_started

    output_path = None
    for candidate in (PROJECT_ROOT / "chunks" / "requirements" / "output").glob("*.json"):
        # Prefer the file matching this absolute path hash via content peek.
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list) and data and data[0].get("source"):
            # Resolve by mtime after this run.
            pass
        if candidate.stat().st_mtime >= wall_started - 1:
            output_path = candidate

    # Deterministic: recompute path hash the same way as service.
    import hashlib
    path_hash = hashlib.sha256(str(E2E_TENDER.resolve()).encode("utf-8")).hexdigest()
    output_path = PROJECT_ROOT / "chunks" / "requirements" / "output" / f"{path_hash}.json"
    if not output_path.exists():
        raise FileNotFoundError(f"Fresh analysis JSON missing: {output_path}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    excel_out = REPORT_DIR / "tender_analysis_e2e.xlsx"
    word_out = REPORT_DIR / "tender_analysis_e2e.docx"

    excel_started = time.perf_counter()
    generate_local_excel(output_path, output_file=excel_out)
    excel_seconds = time.perf_counter() - excel_started

    word_started = time.perf_counter()
    generate_local_word(output_path, output_file=word_out)
    word_seconds = time.perf_counter() - word_started

    MOCK_S3_DIR.mkdir(parents=True, exist_ok=True)
    s3_started = time.perf_counter()
    # Mock S3 destination only — no production upload.
    for src in (excel_out, word_out, output_path):
        shutil.copy2(src, MOCK_S3_DIR / src.name)
    manifest = {
        "bucket": "mock-rfp-copilot-e2e",
        "prefix": "e2e_new_tender/",
        "objects": [p.name for p in MOCK_S3_DIR.iterdir()],
        "note": "Local mock S3 prep; production upload not performed.",
    }
    (MOCK_S3_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    s3_seconds = time.perf_counter() - s3_started

    wall_seconds = time.perf_counter() - wall_started

    # Validation summary (no fabricated compliance).
    option_hits = {"A": 0, "B": 0, "C": 0}
    mandatory_true = 0
    mandatory_false = 0
    with_evidence = 0
    found = 0
    not_found = 0
    review = 0
    for req in requirements:
        text = (req.get("text") or "").casefold()
        if "option a" in text or "option a/" in text:
            option_hits["A"] += 1
        if "option b" in text:
            option_hits["B"] += 1
        if "option c" in text:
            option_hits["C"] += 1
        if req.get("mandatory") is True:
            mandatory_true += 1
        else:
            mandatory_false += 1
        match = req.get("evidence_match") or {}
        status = match.get("status")
        if status == "FOUND":
            found += 1
        elif status == "NOT_FOUND":
            not_found += 1
        else:
            review += 1
        if match.get("evidence"):
            with_evidence += 1

    from openpyxl import load_workbook
    from docx import Document

    wb = load_workbook(excel_out, read_only=True, data_only=True)
    try:
        excel_req_rows = wb["Requirements"].max_row - 1
    finally:
        wb.close()
    doc = Document(excel_out if False else word_out)
    word_paras = len(doc.paragraphs)

    metrics = {
        "model": ollama_client.MODEL_NAME,
        "tender": str(E2E_TENDER),
        "analysis_json": str(output_path),
        "requirement_count": len(requirements),
        "timings_seconds": {
            "wall_total": round(wall_seconds, 3),
            "analysis_extract_and_match": round(analysis_seconds, 3),
            "extraction_llm": round(extract_seconds, 3),
            "evidence_matching": round(match_seconds, 3),
            "excel_generation": round(excel_seconds, 3),
            "word_generation": round(word_seconds, 3),
            "mock_s3_prep": round(s3_seconds, 3),
        },
        "extract_llm_calls": extract_calls,
        "extract_skips": extract_skips,
        "match_batches": match_batches,
        "validation": {
            "option_mentions": option_hits,
            "mandatory_true": mandatory_true,
            "mandatory_false": mandatory_false,
            "evidence_found": found,
            "evidence_not_found": not_found,
            "evidence_other": review,
            "requirements_with_accepted_evidence": with_evidence,
            "excel_requirement_rows": excel_req_rows,
            "excel_rows_match_json": excel_req_rows == len(requirements),
            "word_paragraphs": word_paras,
            "excel_bytes": excel_out.stat().st_size,
            "word_bytes": word_out.stat().st_size,
            "mock_s3_dir": str(MOCK_S3_DIR),
        },
        "target_8_minutes_met": wall_seconds <= 480,
    }
    METRICS_PATH.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2), flush=True)
    print(f"METRICS_PATH={METRICS_PATH}", flush=True)


if __name__ == "__main__":
    main()
