"""Controlled process_tender_document run for representative_new_tender.pdf.

Does not start consumers, publish RabbitMQ, or write the historical
chunks/requirements/progress checkpoint. Progress/output go under diagnostics/.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))
PDF = ROOT / "data" / "e2e_new_tender" / "representative_new_tender.pdf"
BENCH_DIR = ROOT / "diagnostics" / "benchmark_representative_run"
RESULTS = ROOT / "diagnostics" / "benchmark_representative_tender_results.json"
PREVIOUS = (
    ROOT
    / "chunks"
    / "requirements"
    / "progress"
    / "2e7d281a36243a4c65b1757337c9885fdb065baf864349c48b02218fef756e92.json"
)

os.environ.pop("TENDER_EXTRACT_MAX_WORKERS", None)
os.environ.pop("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", None)
os.environ.pop("OLLAMA_RELEVANCE_MODEL", None)
os.environ.setdefault("OLLAMA_MODEL", "qwen3:8b")


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    return (proc.stdout or proc.stderr or "").strip()


def snapshot_host() -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "vm_swapusage": _run(["sysctl", "vm.swapusage"]),
        "memory_pressure": _run(["memory_pressure"]),
        "ollama_ps": _run(["ollama", "ps"]),
        "ollama_list": _run(["ollama", "list"]),
    }


def competing_workloads() -> list[str]:
    raw = _run(["ps", "-axo", "pid,command"])
    hits = []
    for line in raw.splitlines():
        lower = line.lower()
        if "ollama" in lower and "python -c" not in lower:
            hits.append(line.strip())
        if "app.messaging.consumer" in lower or "messaging/consumer.py" in lower:
            hits.append(line.strip())
    return hits


def warmup_8b() -> dict:
    import requests

    started = time.perf_counter()
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen3:8b",
            "prompt": " ",
            "stream": False,
            "think": False,
            "keep_alive": "60m",
            "options": {"num_predict": 1},
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    return {
        "wall_s": round(time.perf_counter() - started, 3),
        "model": body.get("model") or "qwen3:8b",
        "done_reason": body.get("done_reason"),
    }


def previous_summary() -> dict:
    data = json.loads(PREVIOUS.read_text(encoding="utf-8"))
    extract_by_chunk = []
    rel_calls = cache = 0
    statuses = {}
    reqs = 0
    for items in (data.get("completed_chunks") or {}).values():
        times = [
            (item.get("processing_metrics") or {}).get("requirement_extraction_seconds")
            for item in (items or [])
            if isinstance(item, dict)
        ]
        times = [t for t in times if t is not None]
        if times:
            extract_by_chunk.append(times[0])
        for item in items or []:
            if not isinstance(item, dict):
                continue
            reqs += 1
            em = item.get("evidence_match") or {}
            status = em.get("status")
            statuses[status] = statuses.get(status, 0) + 1
            t = em.get("timings") or {}
            rel_calls += int(t.get("relevance_llm_calls") or 0)
            cache += int(t.get("relevance_cache_hits") or 0)
    extract_sum = sum(extract_by_chunk)
    return {
        "chunk_count": data.get("chunk_count"),
        "completed_chunks": len(data.get("completed_chunks") or {}),
        "requirements": reqs,
        "unique_extract_sum_s": round(extract_sum, 3),
        "unique_extract_mean_s": round(extract_sum / len(extract_by_chunk), 3)
        if extract_by_chunk
        else None,
        "relevance_llm_calls": rel_calls,
        "relevance_cache_hits": cache,
        "statuses": statuses,
    }


def source_completeness(requirements: list) -> dict:
    keys = ("page", "chunk", "source_type", "excerpt", "page_reference_type")
    missing = {key: 0 for key in keys}
    empty_excerpt = 0
    table_without_index = 0
    for item in requirements:
        source = item.get("source") or {}
        for key in keys:
            if source.get(key) in (None, ""):
                missing[key] += 1
        excerpt = source.get("excerpt")
        if not (isinstance(excerpt, str) and excerpt.strip()):
            empty_excerpt += 1
        if source.get("source_type") == "table" and source.get("table_index") is None:
            table_without_index += 1
    return {
        "n": len(requirements),
        "missing": missing,
        "empty_excerpt": empty_excerpt,
        "table_without_table_index": table_without_index,
        "complete": all(v == 0 for v in missing.values()) and empty_excerpt == 0,
    }


def main() -> int:
    competing = competing_workloads()
    host_before = snapshot_host()
    if any("consumer" in line.lower() for line in competing):
        RESULTS.write_text(
            json.dumps({"aborted": True, "reason": "consumer_running", "competing": competing}, indent=2)
            + "\n"
        )
        print("ABORT consumer running")
        return 2

    import app.llm.ollama_client as oc
    import app.orchestration.service as service

    assert service.extract_max_workers() == 1
    assert oc.relevance_batch_max_pairs() == 4
    assert oc.MODEL_NAME == "qwen3:8b"

    print("warmup qwen3:8b", flush=True)
    warmup = warmup_8b()
    host_loaded = snapshot_host()
    print(host_loaded["ollama_ps"], flush=True)
    if "qwen3:8b" not in (host_loaded.get("ollama_ps") or ""):
        print("WARN: ollama ps did not list qwen3:8b after warmup", flush=True)

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    service.PROGRESS_DIR = str(BENCH_DIR / "progress")
    service.OUTPUT_DIR = str(BENCH_DIR / "output")

    generates: list[dict] = []
    original_log = oc._log_generate_metrics

    def logged(label, wall_seconds, body, prompt_chars, num_predict, model=None):
        original_log(label, wall_seconds, body, prompt_chars, num_predict, model=model)
        prompt_eval_duration = body.get("prompt_eval_duration")
        eval_duration = body.get("eval_duration")
        prompt_eval_s = (
            round(prompt_eval_duration / 1e9, 3)
            if isinstance(prompt_eval_duration, (int, float)) and prompt_eval_duration > 0
            else None
        )
        eval_s = (
            round(eval_duration / 1e9, 3)
            if isinstance(eval_duration, (int, float)) and eval_duration > 0
            else None
        )
        accounted = (prompt_eval_s or 0) + (eval_s or 0)
        queue_wait_s = round(max(0.0, wall_seconds - accounted), 3) if accounted else None
        generates.append({
            "label": label,
            "model": model or oc.MODEL_NAME,
            "wall_s": round(wall_seconds, 3),
            "prompt_eval_s": prompt_eval_s,
            "eval_s": eval_s,
            "queue_wait_s": queue_wait_s,
            "eval_count": body.get("eval_count"),
            "done_reason": body.get("done_reason"),
        })

    oc._log_generate_metrics = logged
    started = time.perf_counter()
    try:
        requirements = service.process_tender_document(str(PDF), resume=False)
    finally:
        oc._log_generate_metrics = original_log
    wall = time.perf_counter() - started
    host_after = snapshot_host()

    extract_gens = [g for g in generates if g["label"] == "extract_requirements"]
    rel_gens = [
        g
        for g in generates
        if g["label"] in ("assess_evidence_relevance", "assess_evidence_relevance_batch")
    ]
    statuses = {}
    rel_calls = cache_hits = 0
    for item in requirements:
        em = item.get("evidence_match") or {}
        statuses[em.get("status")] = statuses.get(em.get("status"), 0) + 1
        t = em.get("timings") or {}
        rel_calls += int(t.get("relevance_llm_calls") or 0)
        cache_hits += int(t.get("relevance_cache_hits") or 0)

    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pdf": str(PDF),
        "workers": service.extract_max_workers(),
        "relevance_batch_max_pairs": oc.relevance_batch_max_pairs(),
        "model": oc.MODEL_NAME,
        "warmup": warmup,
        "host_before": host_before,
        "host_loaded": host_loaded,
        "host_after": host_after,
        "competing": competing,
        "total_processing_seconds": round(wall, 3),
        "chunk_count_progress": None,
        "requirements": len(requirements),
        "statuses": statuses,
        "extraction_generate_calls": len(extract_gens),
        "relevance_generate_calls": len(rel_gens),
        "relevance_llm_calls_reported": rel_calls,
        "relevance_cache_hits": cache_hits,
        "relevance_cache_misses": rel_calls,
        "extract_wall_mean_s": round(
            sum(g["wall_s"] or 0 for g in extract_gens) / len(extract_gens), 3
        )
        if extract_gens
        else None,
        "extract_wall_sum_s": round(sum(g["wall_s"] or 0 for g in extract_gens), 3),
        "relevance_wall_mean_s": round(
            sum(g["wall_s"] or 0 for g in rel_gens) / len(rel_gens), 3
        )
        if rel_gens
        else None,
        "relevance_wall_sum_s": round(sum(g["wall_s"] or 0 for g in rel_gens), 3),
        "queue_wait_sum_s": round(
            sum(g.get("queue_wait_s") or 0 for g in generates), 3
        ),
        "queue_wait_extract_mean_s": round(
            sum(g.get("queue_wait_s") or 0 for g in extract_gens) / len(extract_gens), 3
        )
        if extract_gens
        else None,
        "queue_wait_relevance_mean_s": round(
            sum(g.get("queue_wait_s") or 0 for g in rel_gens) / len(rel_gens), 3
        )
        if rel_gens
        else None,
        "prompt_eval_extract_mean_s": round(
            sum(g.get("prompt_eval_s") or 0 for g in extract_gens) / len(extract_gens), 3
        )
        if extract_gens
        else None,
        "eval_extract_mean_s": round(
            sum(g.get("eval_s") or 0 for g in extract_gens) / len(extract_gens), 3
        )
        if extract_gens
        else None,
        "generate_labels": {
            label: sum(1 for g in generates if g["label"] == label)
            for label in sorted({g["label"] for g in generates})
        },
        "source_completeness": source_completeness(requirements),
        "previous_2e7d281a": previous_summary(),
        "generates": generates,
    }
    progress_files = list((BENCH_DIR / "progress").glob("*.json"))
    if progress_files:
        saved = json.loads(progress_files[0].read_text(encoding="utf-8"))
        payload["chunk_count_progress"] = saved.get("chunk_count")
        payload["completed_chunks"] = len(saved.get("completed_chunks") or {})

    RESULTS.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({k: payload[k] for k in (
        "total_processing_seconds",
        "extraction_generate_calls",
        "relevance_generate_calls",
        "relevance_llm_calls_reported",
        "relevance_cache_hits",
        "extract_wall_mean_s",
        "relevance_wall_mean_s",
        "queue_wait_sum_s",
        "queue_wait_extract_mean_s",
        "queue_wait_relevance_mean_s",
        "statuses",
        "requirements",
        "chunk_count_progress",
        "source_completeness",
        "previous_2e7d281a",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
